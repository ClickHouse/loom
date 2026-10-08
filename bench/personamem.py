"""PersonaMem-v2 against loom: does the standing profile help an answer?

PersonaMem-v2 (HF `bowen-upenn/PersonaMem-v2`) is 200 personas × 25 questions in the
text split. Each persona has a long user–chatbot history (32k or 128k tokens) in which
preferences are revealed IMPLICITLY — a seasonal allergy mentioned while asking for an
email edit — and each question is a fresh user query with one personalised answer and
three plausible answers that ignore or contradict the persona. Scoring is four-way
multiple choice, which is why published numbers sit near 50%.

A consolidated user profile read next to the retrieved episodes can beat
either alone. loom has both surfaces: `memory.search` over the
extracted facts, and the `memory_type='profile'` sections the
profile-consolidation worker folds and `profile.render` serves (and
`memory.ask` pins). This harness runs the three arms with a
FIXED reader so the only thing that differs is what memory hands it:

    memories          top-K facts for the query, this user
    memories+profile  the same facts with the rendered profile ahead of them
    profile           the rendered profile alone

Pipeline per persona: one throwaway namespace (erased at the end,
registered with `consolidation_enabled` so the profile worker on the
target server folds this user), the chat history ingested through
`memory.set_from_messages` in windows of `--messages-per-session` messages
with a synthetic observation_date per window, a wait for the profile fold
to land (`profile.render` polled until a section exists), then every
question under every arm. The reader is `gpt-4.1-mini` at temperature 0 and
reads ONLY the context block plus the four options; it answers one letter.

Run (server: LOOM_PROFILE_CONSOLIDATION_ENABLED=true with a short interval
and a low min_memories, e.g. an isolated loom):

    LOOM_API_KEY=... OPENAI_API_KEY=... python bench/personamem.py \\
        --base-url http://127.0.0.1:7814 --limit-personas 10 \\
        --questions-per-persona 10 --out bench/personamem.csv

Data: `--dataset-dir` (default bench/datasets/personamem) is filled on first
use from the HF repo — the text benchmark CSV and the chat histories the
selected personas need. ~$0.30 of gpt-4o-mini extraction per 32k persona.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

try:
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
    from bench.locomo_qa import _chat, _post
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
    from locomo_qa import _chat, _post  # type: ignore[no-redef]

HF_BASE = "https://huggingface.co/datasets/bowen-upenn/PersonaMem-v2/resolve/main/"
BENCH_CSV = "benchmark/text/benchmark.csv"
ARMS: tuple[str, ...] = ("memories", "memories+profile", "profile")
LETTERS = "ABCD"
_DEFAULT_READER_MODEL = os.environ.get("BENCH_READER_MODEL", "gpt-4.1-mini")

_READER_SYSTEM = (
    "You are the user's personal assistant choosing the reply that best fits THIS user. "
    "You will see a CONTEXT block (what the assistant remembers about the user) and the "
    "user's new message, then four candidate replies A-D. Exactly one candidate is "
    "personalised to this user's stated preferences, situation or constraints; the others "
    "fit a different user or ignore what is known. Use ONLY the context. If the context "
    "says nothing relevant, pick the most generally appropriate reply. Answer with the "
    "single letter and nothing else."
)


# ---------------------------------------------------------------- pure parts


def sessionize(messages: list[dict[str, Any]], per_session: int) -> list[list[dict[str, str]]]:
    """Windows of `per_session` non-system messages, each starting on a user
    turn where possible (an odd cut is nudged forward one message so a window
    never opens on the assistant's half of an exchange)."""
    turns = [
        {"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
        for m in messages
        if m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip()
    ]
    if not turns:
        return []
    n = max(2, int(per_session))
    out: list[list[dict[str, str]]] = []
    i = 0
    while i < len(turns):
        j = min(len(turns), i + n)
        if j < len(turns) and turns[j]["role"] == "assistant":
            j += 1
        out.append(turns[i:j])
        i = j
    return out


def observation_dates(n: int, start: date = date(2025, 1, 6)) -> list[str]:
    """One synthetic date per window, a day apart, so the store has an order."""
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def option_seed(seed: int, persona_id: str, q_index: int) -> int:
    """Shuffle seed for one question: run seed, persona and question index
    together. Seeding by question index alone would give every persona the
    same permutation at index i and skew the correct letter's distribution,
    so a reader that always answered one letter would score above chance."""
    h = hashlib.sha1(f"{seed}:{persona_id}:{q_index}".encode()).hexdigest()
    return int(h[:8], 16)


def options_for(correct: str, incorrect: list[str], *, seed: int) -> tuple[list[str], str]:
    """The four candidates in a seeded shuffle and the letter of the correct one."""
    opts = [correct] + [str(x) for x in incorrect][:3]
    rng = random.Random(seed)
    rng.shuffle(opts)
    return opts, LETTERS[opts.index(correct)]


_LETTER_RE = re.compile(r"\b([ABCD])\b")


def parse_letter(reply: str) -> str:
    """The first standalone A-D in the reader's reply; '' when none."""
    m = _LETTER_RE.search((reply or "").strip().upper())
    return m.group(1) if m else ""


def reader_prompt(context: str, query: str, options: list[str]) -> str:
    lines = [
        "CONTEXT (what the assistant remembers about this user):",
        context.strip() or "(nothing)",
        "",
        f"USER'S NEW MESSAGE:\n{query.strip()}",
        "",
        "CANDIDATE REPLIES:",
    ]
    for letter, opt in zip(LETTERS, options, strict=False):
        lines.append(f"{letter}. {opt.strip()}")
    lines.append("")
    lines.append("Which candidate is best personalised for this user? Answer A, B, C or D.")
    return "\n".join(lines)


def memories_block(hits: list[dict[str, Any]]) -> str:
    rows = []
    for h in hits:
        when = str(h.get("valid_at") or h.get("temporal_anchor") or "")[:10] or "undated"
        whose = " (about someone other than the user)" if h.get("about_other") else ""
        text = h.get("body") or h.get("content_full") or h.get("content_excerpt", "")
        rows.append(f"[{when}] [{h.get('memory_key', '')}]{whose} {text}")
    return "\n".join(rows)


def context_for(arm: str, memories: str, profile: str) -> str:
    if arm == "memories":
        return memories
    if arm == "profile":
        return profile
    if arm == "memories+profile":
        return f"STANDING PROFILE:\n{profile}\n\nMEMORIES:\n{memories}" if profile else memories
    raise ValueError(f"unknown arm {arm!r}")


def parse_query(raw: str) -> str:
    """`user_query` is a dict rendered as a Python literal in the CSV."""
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            import ast

            obj = ast.literal_eval(text)
            if isinstance(obj, dict):
                return str(obj.get("content") or "")
        except Exception:  # noqa: BLE001 — fall through to the raw text
            pass
    return text


def parse_incorrect(raw: str) -> list[str]:
    text = (raw or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, list):
            return [str(x) for x in obj]
    except Exception:  # noqa: BLE001
        pass
    try:
        import ast

        obj = ast.literal_eval(text)
        if isinstance(obj, list):
            return [str(x) for x in obj]
    except Exception:  # noqa: BLE001
        pass
    return []


@dataclass
class QuestionResult:
    persona_id: str
    namespace: str
    question_index: int
    scenario: str
    pref_type: str
    who: str
    updated: bool
    query: str
    correct_letter: str
    arm: str
    picked: str
    correct: bool
    context_chars: int
    hits: int
    profile_chars: int
    reply: str = ""
    error: str = ""
    # The memory keys the reader saw, in rank order — the retrieval coverage
    # reading a re-run over the same namespaces compares against.
    hit_keys: str = ""


@dataclass
class PersonaBuild:
    persona_id: str
    namespace: str
    user_id: str
    sessions: int = 0
    messages: int = 0
    written: int = 0
    profile_labels: list[str] = field(default_factory=list)
    profile_wait_s: float = 0.0
    error: str = ""


def aggregate(results: list[QuestionResult]) -> dict[str, Any]:
    """Accuracy per arm overall and by scenario / pref_type / updated / who,
    plus the pairwise flip counts between arms — the published columns with
    the discordance a delta needs to be read against."""
    # A reply that names no letter is a wrong pick, not a skipped question: dropping
    # it would raise the accuracy of an arm whose reader rambles.
    judged = [r for r in results if not r.error]
    out: dict[str, Any] = {"questions": len({(r.persona_id, r.question_index) for r in results})}
    by_arm: dict[str, dict[str, Any]] = {}
    for arm in sorted({r.arm for r in judged}):
        rows = [r for r in judged if r.arm == arm]
        acc = lambda rs: round(100.0 * sum(r.correct for r in rs) / len(rs), 2) if rs else None  # noqa: E731
        slices: dict[str, dict[str, Any]] = {}
        for key in ("scenario", "pref_type", "who"):
            groups: dict[str, list[QuestionResult]] = defaultdict(list)
            for r in rows:
                groups[getattr(r, key)].append(r)
            slices[key] = {k: {"n": len(v), "accuracy": acc(v)} for k, v in sorted(groups.items())}
        upd = [r for r in rows if r.updated]
        by_arm[arm] = {
            "n": len(rows),
            "accuracy": acc(rows),
            "updated": {"n": len(upd), "accuracy": acc(upd)},
            "by": slices,
        }
    out["arms"] = by_arm
    pairs: dict[str, dict[str, int]] = {}
    arms = sorted(by_arm)
    idx = {(r.persona_id, r.question_index, r.arm): r for r in judged}
    for i, a in enumerate(arms):
        for b in arms[i + 1 :]:
            a_only = b_only = both = 0
            for key in {(r.persona_id, r.question_index) for r in judged}:
                ra, rb = idx.get((*key, a)), idx.get((*key, b))
                if ra is None or rb is None:
                    continue
                both += 1
                a_only += ra.correct and not rb.correct
                b_only += rb.correct and not ra.correct
            pairs[f"{a} vs {b}"] = {"paired": both, f"{a}_only": a_only, f"{b}_only": b_only}
    out["pairs"] = pairs
    out["errors"] = sum(1 for r in results if r.error)
    out["unparsed"] = sum(1 for r in judged if not r.picked)
    return out


# ---------------------------------------------------------------- data


def ensure_file(dataset_dir: Path, rel: str) -> Path:
    dest = dataset_dir / rel
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = HF_BASE + rel
    with httpx.Client(follow_redirects=True, timeout=300.0) as c:
        r = c.get(url)
        r.raise_for_status()
        dest.write_bytes(r.content)
    return dest


def load_benchmark(dataset_dir: Path) -> list[dict[str, str]]:
    csv.field_size_limit(10**9)
    path = ensure_file(dataset_dir, BENCH_CSV)
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def select_personas(rows: list[dict[str, str]], *, limit: int, seed: int) -> list[str]:
    ids = sorted({r["persona_id"] for r in rows}, key=lambda x: int(x))
    if limit and limit < len(ids):
        rng = random.Random(seed)
        ids = sorted(rng.sample(ids, limit), key=lambda x: int(x))
    return ids


# ---------------------------------------------------------------- pipeline


async def ingest_persona(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    *,
    org: str,
    namespace: str,
    user_id: str,
    agent: str,
    messages: list[dict[str, Any]],
    per_session: int,
    concurrency: int,
) -> PersonaBuild:
    build = PersonaBuild(persona_id="", namespace=namespace, user_id=user_id)
    sessions = sessionize(messages, per_session)
    dates = observation_dates(len(sessions))
    build.sessions = len(sessions)
    build.messages = sum(len(s) for s in sessions)
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(i: int, turns: list[dict[str, str]]) -> int:
        async with sem:
            body = {
                "org": org,
                "namespace": namespace,
                "agent": agent,
                "user_id": user_id,
                "session_id": f"{namespace}:s{i}",
                "observation_date": dates[i],
                "messages": turns,
            }
            try:
                out = await _post(client, base_url + "/v1/memory.set_from_messages", body, token)
            except Exception as e:  # noqa: BLE001 — one window never sinks the persona
                build.error = f"{type(e).__name__}: {str(e)[:120]}"
                return 0
            return len(out.get("written") or [])

    counts = await asyncio.gather(*(_one(i, s) for i, s in enumerate(sessions)))
    build.written = sum(counts)
    return build


async def wait_for_profile(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    *,
    org: str,
    namespace: str,
    user_id: str,
    agent: str,
    timeout_s: float,
) -> tuple[str, list[str], float]:
    """Poll profile.render until a section exists or the timeout passes.
    Returns (markdown, labels, seconds waited)."""
    t0 = time.perf_counter()
    text, labels = "", []
    while True:
        try:
            out = await _post(
                client,
                base_url + "/v1/profile.render",
                {
                    "org": org,
                    "namespace": namespace,
                    "agent": agent,
                    "user_id": user_id,
                    "format": "markdown",
                    "read_scope": "own_user",
                },
                token,
            )
            text = str(out.get("rendered") or "")
            labels = list(out.get("labels") or [])
        except Exception:  # noqa: BLE001 — a failed poll is a retry
            pass
        if labels or time.perf_counter() - t0 >= timeout_s:
            return text, labels, round(time.perf_counter() - t0, 1)
        await asyncio.sleep(5.0)


async def answer_question(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    *,
    org: str,
    namespace: str,
    user_id: str,
    agent: str,
    row: dict[str, str],
    q_index: int,
    arms: list[str],
    profile_text: str,
    top_k: int,
    reader_model: str,
    api_key: str,
    usage: dict[str, int],
    seed: int,
    search_mode: str = "",
    bodies: str = "",
) -> list[QuestionResult]:
    query = parse_query(row.get("user_query", ""))
    correct = str(row.get("correct_answer") or "")
    options, letter = options_for(
        correct, parse_incorrect(row.get("incorrect_answers", "")), seed=seed
    )
    base = dict(
        persona_id=row["persona_id"],
        namespace=namespace,
        question_index=q_index,
        scenario=row.get("conversation_scenario", ""),
        pref_type=row.get("pref_type", ""),
        who=row.get("who", ""),
        updated=str(row.get("updated", "")).strip().lower() == "true",
        query=query,
        correct_letter=letter,
    )
    hits: list[dict[str, Any]] = []
    base["hit_keys"] = ""
    search_err = ""
    if any(a != "profile" for a in arms):
        try:
            res = await _post(
                client,
                base_url + "/v1/memory.search",
                {
                    "org": org,
                    "namespace": namespace,
                    "agent": agent,
                    "user_id": user_id,
                    "query": query,
                    "top_k": top_k,
                    "namespace_scope": "this",
                    **({"search_mode": search_mode} if search_mode else {}),
                    **({"bodies": bodies} if bodies else {}),
                },
                token,
            )
            hits = list(res.get("results") or [])
            base["hit_keys"] = "|".join(str(h.get("memory_key") or "") for h in hits)
        except Exception as e:  # noqa: BLE001
            search_err = f"{type(e).__name__}: {str(e)[:120]}"
    mem_block = memories_block(hits)
    out: list[QuestionResult] = []
    for arm in arms:
        ctx = context_for(arm, mem_block, profile_text)
        reply, err, picked = "", search_err if arm != "profile" else "", ""
        if not err:
            try:
                reply = await _chat(
                    client,
                    reader_model,
                    reader_prompt(ctx, query, options),
                    api_key,
                    max_tokens=5,
                    usage=usage,
                    system=_READER_SYSTEM,
                )
                picked = parse_letter(reply)
                if not picked:
                    err = "no letter in reply"
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {str(e)[:120]}"
        out.append(
            QuestionResult(
                **base,
                arm=arm,
                picked=picked,
                correct=bool(picked) and picked == letter,
                context_chars=len(ctx),
                hits=len(hits),
                profile_chars=len(profile_text),
                reply=reply[:20],
                error=err,
            )
        )
    return out


async def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dataset-dir", default="bench/datasets/personamem")
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--org", default="dev")
    p.add_argument("--agent", default="personamem-bench")
    p.add_argument("--context", choices=["32k", "128k"], default="32k")
    p.add_argument("--limit-personas", type=int, default=10)
    p.add_argument("--questions-per-persona", type=int, default=0, help="0 = all (25)")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--arms", default=",".join(ARMS))
    p.add_argument("--messages-per-session", type=int, default=10)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--wait-profile-seconds", type=float, default=180.0)
    p.add_argument("--reader-model", default=_DEFAULT_READER_MODEL)
    p.add_argument("--ingest-concurrency", type=int, default=3)
    p.add_argument("--qa-concurrency", type=int, default=4)
    p.add_argument("--out", default="bench/personamem.csv")
    p.add_argument("--keep-namespaces", action="store_true")
    p.add_argument(
        "--namespaces-json",
        default="",
        help="path to a {persona_id: namespace} map from an earlier --keep-namespaces run; "
        "personas found in it are not ingested again — the same rows, a new question pass "
        "(retrieval and reader A/Bs without re-extraction)",
    )
    p.add_argument(
        "--bodies",
        default="",
        help="memory.search `bodies` for the memories arms: unit = the whole session in written "
        "order within the budget, source = the linked evidence passage; "
        "server default when empty",
    )
    p.add_argument(
        "--search-mode",
        default="",
        help="memory.search search_mode override for the memories arms (server default when empty)",
    )
    args = p.parse_args()

    api_key = os.environ.get("BENCH_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("OPENAI_API_KEY (or BENCH_OPENAI_API_KEY) is required for the reader")
        return 2
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ARMS:
            print(f"unknown arm {a!r}; choose from {ARMS}")
            return 2

    dataset_dir = Path(args.dataset_dir)
    rows = load_benchmark(dataset_dir)
    personas = select_personas(rows, limit=args.limit_personas, seed=args.seed)
    by_persona: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        if r["persona_id"] in set(personas):
            by_persona[r["persona_id"]].append(r)

    usage = {"tin": 0, "tout": 0, "calls": 0}
    results: list[QuestionResult] = []
    builds: list[PersonaBuild] = []
    link_col = "chat_history_32k_link" if args.context == "32k" else "chat_history_128k_link"

    reuse_map: dict[str, str] = {}
    if args.namespaces_json:
        reuse_map = {
            str(k): str(v) for k, v in json.loads(Path(args.namespaces_json).read_text()).items()
        }
    namespaces_out: dict[str, str] = {}
    # Namespaces this run mints are erased on exit, after a failure or Ctrl-C
    # too; ones reused through --namespaces-json were never remembered and stay.
    async with (
        httpx.AsyncClient() as client,
        erase_on_exit(
            lambda path, body: _post(client, args.base_url + path, body, args.token),
            org=args.org,
            keep=args.keep_namespaces,
        ),
    ):
        for pid in personas:
            prow = by_persona[pid]
            qs = prow[: args.questions_per_persona] if args.questions_per_persona else prow
            user_id = f"persona-{pid}"
            reused_ns = reuse_map.get(str(pid), "")
            if reused_ns:
                namespace = reused_ns
                build = PersonaBuild(persona_id=pid, namespace=namespace, user_id=user_id)
                profile_text, labels, waited = await wait_for_profile(
                    client,
                    args.base_url,
                    args.token,
                    org=args.org,
                    namespace=namespace,
                    user_id=user_id,
                    agent=args.agent,
                    timeout_s=min(args.wait_profile_seconds, 5.0),
                )
                build.profile_labels = labels
                build.profile_wait_s = waited
                builds.append(build)
                namespaces_out[str(pid)] = namespace
                print(
                    f"[persona {pid}] reusing {namespace}; profile "
                    f"{'✓ ' + ','.join(labels) if labels else 'none'}",
                    flush=True,
                )
            else:
                namespace = f"bench-pmem-{pid}-{uuid.uuid4().hex[:6]}"
                namespaces_out[str(pid)] = namespace
            if not reused_ns:
                remember_bench_namespace(args.org, namespace)
                await register_bench_namespace(
                    lambda path, body: _post(client, args.base_url + path, body, args.token),
                    org=args.org,
                    namespace=namespace,
                    description=f"PersonaMem-v2 persona {pid} ({args.context})",
                    # The profile worker on the target server folds only opted-in
                    # namespaces; the fold is the surface under measurement.
                    extra={"consolidation_enabled": True},
                    keep=args.keep_namespaces,
                )
                hist_path = ensure_file(dataset_dir, prow[0][link_col])
                history = json.loads(hist_path.read_text(encoding="utf-8"))
                messages = list(history.get("chat_history") or [])
                build = await ingest_persona(
                    client,
                    args.base_url,
                    args.token,
                    org=args.org,
                    namespace=namespace,
                    user_id=user_id,
                    agent=args.agent,
                    messages=messages,
                    per_session=args.messages_per_session,
                    concurrency=args.ingest_concurrency,
                )
                build.persona_id = pid
                profile_text, labels, waited = await wait_for_profile(
                    client,
                    args.base_url,
                    args.token,
                    org=args.org,
                    namespace=namespace,
                    user_id=user_id,
                    agent=args.agent,
                    timeout_s=args.wait_profile_seconds,
                )
                build.profile_labels = labels
                build.profile_wait_s = waited
                builds.append(build)
                print(
                    f"[persona {pid}] {build.sessions} sessions / {build.messages} messages → "
                    f"{build.written} memories; profile "
                    f"{'✓ ' + ','.join(labels) if labels else 'none'} "
                    f"after {waited}s{(' ERR ' + build.error) if build.error else ''}",
                    flush=True,
                )
            qa_sem = asyncio.Semaphore(max(1, args.qa_concurrency))

            async def _guarded(
                i: int,
                row: dict[str, str],
                _ns: str = namespace,
                _uid: str = user_id,
                _prof: str = profile_text,
                _sem: asyncio.Semaphore = qa_sem,
                _pid: str = pid,
            ) -> list[QuestionResult]:
                async with _sem:
                    return await answer_question(
                        client,
                        args.base_url,
                        args.token,
                        org=args.org,
                        namespace=_ns,
                        user_id=_uid,
                        agent=args.agent,
                        row=row,
                        q_index=i,
                        arms=arms,
                        profile_text=_prof,
                        top_k=args.top_k,
                        reader_model=args.reader_model,
                        api_key=api_key,
                        usage=usage,
                        seed=option_seed(args.seed, _pid, i),
                        search_mode=args.search_mode,
                        bodies=args.bodies,
                    )

            per_q = await asyncio.gather(*(_guarded(i, r) for i, r in enumerate(qs)))
            for chunk in per_q:
                results.extend(chunk)
            agg = aggregate([r for r in results if r.persona_id == pid])
            print(
                f"[persona {pid}] "
                + " ".join(f"{a}={v['accuracy']}" for a, v in agg["arms"].items()),
                flush=True,
            )

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            fields = list(asdict(results[0]).keys()) if results else ["empty"]
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in results:
                w.writerow(asdict(r))
        summary = {
            "personas": len(personas),
            "context": args.context,
            "reader_model": args.reader_model,
            "top_k": args.top_k,
            "messages_per_session": args.messages_per_session,
            "profiles_folded": sum(1 for b in builds if b.profile_labels),
            "memories_written": sum(b.written for b in builds),
            "reader_tokens": usage,
            **aggregate(results),
        }
        out.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2))
        out.with_suffix(".namespaces.json").write_text(json.dumps(namespaces_out, indent=2))
        print(json.dumps({k: v for k, v in summary.items() if k != "arms"}, indent=2))
        for arm, v in summary["arms"].items():
            print(f"  {arm:18} n={v['n']} accuracy={v['accuracy']} updated={v['updated']}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
