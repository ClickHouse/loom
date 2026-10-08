"""MemBench through loom: participation + observation
scenarios, factual + reflective levels, multiple-choice accuracy, evidence
recall, read/write seconds, and a capacity curve under injected noise.

Why this bench exists beside LongMemEval and the judged corpora: it is the
only public set that asks, in one harness, the four things loom did not
measure — whether memory holds what the AGENT said ("what movies have you
recommended me?"), whether it holds what can only be INFERRED over many
facts ("which flavour do I prefer?"), how accuracy falls as stored tokens
grow, and what a read and a write cost in seconds. Its hardest types
(aggregative, comparative) are entity-attribute questions, the shape loom's typed records exist for.

Data: the released archive (Google Drive, see the benchmark's repository
import-myself/Membench), unpacked so that `--data-dir` holds
`Categorical data/{FirstAgent,ThirdAgent}/*.json` and `NoiseData/*.json`.
Nothing here is committed: the corpus is third-party.

Per trajectory: a fresh namespace; sessions fed in TIME ORDER through
memory.set_from_messages with the session date as observation_date (the
LongMemEval harness's shape); the question through memory.ask with the
four options appended and a letter-only answer contract; accuracy by
letter against `ground_truth`; evidence recall by mapping the returned
memory keys to the sessions they were extracted from and comparing with
the item's `target_step_id`. `--noise K` inserts K noise sessions from the
released pools between real ones (≈ 1k tokens each), so
runs at K = 0, 10, 30, 90 draw accuracy against stored tokens.

    python bench/membench.py --data-dir /path/Membenchdata \\
        --scenario observation --types Single-hop,Comparative,Aggregative \\
        --limit 20 --base-url $LOOM_BASE_URL --token $TOK --org dev \\
        --out bench/membench.json
    python bench/membench.py --dry --data-dir /path/Membenchdata --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import time
import uuid

try:
    from bench._gate import (
        add_gate_arguments,
        agreement,
        flip_rate,
        gate_from_args,
        identical_share,
        majority_label,
        measurement_fields,
    )
    from bench._http import apost_json
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from _gate import (  # type: ignore[no-redef]
        add_gate_arguments,
        agreement,
        flip_rate,
        gate_from_args,
        identical_share,
        majority_label,
        measurement_fields,
    )
    from _http import apost_json  # type: ignore[no-redef]
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---- data model ------------------------------------------------------------

SCENARIO_DIR = {"observation": "ThirdAgent", "participation": "FirstAgent"}
REFLECTIVE_TYPES = {"Preference", "Emotion"}
_TIME_RE = re.compile(r"'?(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}))?'?")
_LETTER_RE = re.compile(r"(?<![A-Za-z])([ABCD])(?![A-Za-z])")


@dataclass
class Session:
    sid: str
    date: str  # ISO date for observation_date ("" when unknown)
    messages: list[dict[str, str]]  # {"role", "content"}
    is_noise: bool = False
    # original (session_index, mid) pairs → position, for evidence mapping
    step_ids: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class Item:
    scenario: str
    qtype: str
    subset: str  # roles | events | hybrid | movie | food | book | ""
    tid: int
    question: str
    choices: dict[str, str]
    ground_truth: str
    question_date: str
    evidence: list[tuple[int, int]]  # (session_index, mid)
    sessions: list[Session]

    index: int = 0  # position within the file: tids repeat across files/subsets

    @property
    def qid(self) -> str:
        return f"{self.scenario[:3]}-{self.qtype}-{self.subset or 'all'}-{self.tid}-{self.index}"


@dataclass
class ItemResult:
    qid: str
    scenario: str
    qtype: str
    reflective: bool
    letter: str
    ground_truth: str
    correct: bool
    abstained: bool
    evidence_sessions: int
    evidence_recalled: int
    stored_chars: int
    noise_sessions: int
    write_seconds: list[float]
    read_seconds: float
    tokens_in: int
    answer: str = ""
    error: str = ""
    abstain_reason: str = ""
    degraded: str = ""
    evidence_precision: float = 0.0
    # The namespace the item was ingested into (or reused from --namespace-json).
    namespace: str = ""
    # --repeats N: per-run CORRECT/WRONG labels, agreement with
    # the majority written to `correct`, and the retrieval pin (share of runs
    # whose returned keys equalled the first run's).
    repeat_labels: str = ""
    repeat_agreement: float = 1.0
    repeat_window_identical: float = 1.0


# ---- pure helpers ----------------------------------------------------------


def parse_iso_date(t: str) -> str:
    """MemBench times look like "'2024-10-01 08:00' Tuesday" → 2024-10-01."""
    m = _TIME_RE.search(t or "")
    return m.group(1) if m else ""


def parse_iso_datetime(raw: str) -> str:
    """`'2024-10-01 08:10' Tuesday` → `2024-10-01T08:10:00`; '' when absent."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", raw or "")
    return f"{m.group(1)}T{m.group(2)}:00" if m else ""


def render_choice(v: Any) -> str:
    if isinstance(v, list):
        return "; ".join(str(x) for x in v)
    return str(v)


QUERY_CAP = 1900  # memory.ask accepts 2000 chars; leave room for the contract line


STATEMENT_SEMANTICS = (
    " Count every stated instance separately: repeated or updated statements "
    "about the same item each count once, and names that differ are different items."
)


def build_query(
    question: str, choices: dict[str, str], question_date: str, *, aggregation: str = "entities"
) -> str:
    """The multiple-choice contract: the question, the options, and an
    instruction to answer with the letter alone. The date the question is
    asked rides along so relative expressions in the memories resolve.
    Long options (post-processing items quote whole reviews) are elided
    evenly so the whole query stays under memory.ask's 2000-char limit.

    `aggregation="statements"` states MemBench's counting convention in the
    question: the generator (DialogueGeneration/aggregative.py) asks an LLM
    "is this statement a yes?" per user message and sums the yeses, so a
    repeated or updated statement about one event counts again and
    "Aqua Fest" / "AquaFest" are two. Loom's default is one entity, latest
    value; with the convention stated the asker gets what they asked for."""
    date = f"Current date: {question_date}\n" if question_date else ""
    # The convention rides on the contract line, not on the question: the
    # intent grammar reads the first line and abstains past 180 chars, and a
    # longer stem would switch count mode off for the very questions it serves.
    convention = STATEMENT_SEMANTICS if aggregation == "statements" else ""
    head = (
        f"{date}{question}\n\nChoose exactly one option and reply with its letter only."
        f"{convention}\n"
    )
    rendered = {k: render_choice(v) for k, v in sorted(choices.items())}
    budget = QUERY_CAP - len(head) - sum(len(k) + 3 for k in rendered)
    per = max(40, budget // max(1, len(rendered)))
    opts = "\n".join(
        f"{k}) {v if len(v) <= per else v[: per - 1] + '…'}" for k, v in rendered.items()
    )
    return head + opts


def parse_letter(answer: str) -> str:
    """The first standalone A–D in the answer; '' when none (abstained or
    prose the contract did not constrain)."""
    text = (answer or "").strip()
    m = re.match(r"^\s*\(?([ABCD])[\)\.:\s]", text + " ")
    if m:
        return m.group(1)
    m2 = _LETTER_RE.search(text)
    return m2.group(1) if m2 else ""


def evidence_recall(
    evidence: list[tuple[int, int]],
    step_to_session: dict[tuple[int, int], str],
    returned_keys: list[str],
    key_to_sessions: dict[str, set[str]],
) -> tuple[int, int]:
    """(evidence sessions, evidence sessions covered by a returned key)."""
    needed = {step_to_session[s] for s in evidence if s in step_to_session}
    got: set[str] = set()
    for k in returned_keys:
        got |= key_to_sessions.get(k, set())
    return len(needed), len(needed & got)


def evidence_precision(
    evidence: list[tuple[int, int]],
    step_to_session: dict[tuple[int, int], str],
    returned_keys: list[str],
    key_to_sessions: dict[str, set[str]],
) -> float:
    """Share of returned memory keys that came from an evidence session —
    the retrieval-cleanness number that answer accuracy masks: a system can recall every evidence
    session and still hand the reader ten unrelated rows for each one.
    Keys the harness cannot attribute to a session (chunks, summaries) are
    left out of the denominator; 0.0 when nothing is attributable."""
    needed = {step_to_session[s] for s in evidence if s in step_to_session}
    attributable = [k for k in returned_keys if key_to_sessions.get(k)]
    if not attributable:
        return 0.0
    clean = sum(1 for k in attributable if key_to_sessions[k] & needed)
    return round(clean / len(attributable), 3)


def _load_categorical(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """(subset, trajectory) pairs; files are either {subset: [traj]} or [traj]."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [("", t) for t in raw]
    out: list[tuple[str, dict[str, Any]]] = []
    for subset, trajs in raw.items():
        for t in trajs if isinstance(trajs, list) else list(trajs.values()):
            out.append((str(subset), t))
    return out


def sessions_of(traj: dict[str, Any], scenario: str) -> list[Session]:
    """Normalise the three released shapes into role/content sessions:
    observation-factual = one flat list of {message}; observation-reflective =
    list of lists of {message}; participation = list of lists of {user,
    assistant}. Each real message keeps its (session_index, mid) so the
    item's `target_step_id` maps to a session for evidence recall."""
    ml = traj.get("message_list") or []
    groups: list[list[dict[str, Any]]]
    if ml and isinstance(ml[0], dict):
        groups = [ml]  # flat: one session per trajectory
        flat = True
    else:
        groups = [g for g in ml if isinstance(g, list)]
        flat = False
    out: list[Session] = []
    for si, group in enumerate(groups):
        msgs: list[dict[str, str]] = []
        steps: list[tuple[int, int]] = []
        for m in group:
            # Two id spellings: `mid` (per session) or `sid` (global message
            # index, used by the participation factual files). Evidence pairs
            # are (message id, session index) in either case.
            mid = int(m.get("mid", m.get("sid", len(steps))))
            # Every released message carries its own clock ("'2024-10-01
            # 08:10' Tuesday"); the Emotion questions ask about a moment
            # inside the session, so the turn's time rides along.
            stamp = parse_iso_datetime(str(m.get("time") or ""))
            extra = {"timestamp": stamp} if stamp else {}
            if scenario == "participation":
                user = m.get("user") or m.get("user_message")
                assistant = m.get("assistant") or m.get("assistant_message")
                if user:
                    msgs.append({"role": "user", "content": str(user), **extra})
                if assistant:
                    msgs.append({"role": "assistant", "content": str(assistant), **extra})
            else:
                msgs.append({"role": "user", "content": str(m.get("message") or ""), **extra})
            # flat factual lists key evidence by mid alone → session 0
            steps.append((0 if flat else si, mid))
        date = parse_iso_date(str(group[0].get("time") or "")) if group else ""
        out.append(Session(sid=f"s{si}", date=date, messages=msgs, step_ids=steps))
    return out


def load_items(
    data_dir: Path, scenario: str, types: list[str], limit: int, seed: int
) -> list[Item]:
    items: list[Item] = []
    base = data_dir / "Categorical data" / SCENARIO_DIR[scenario]
    for qtype in types:
        # Seeded per type so the sample for a type does not depend on which
        # other types were requested or their order: a --types Aggregative
        # rerun measures the same items as the full run did.
        rng = random.Random(f"{seed}:{qtype.lower()}")
        path = next(iter(base.glob(f"{qtype}.json")), None) or next(
            iter(p for p in base.glob("*.json") if p.stem.lower() == qtype.lower()), None
        )
        if path is None:
            print(f"warning: no file for type {qtype!r} under {base}", file=sys.stderr)
            continue
        trajs = _load_categorical(path)
        rng.shuffle(trajs)
        for index, (subset, t) in enumerate(trajs[: limit or len(trajs)]):
            qa = t.get("QA") or {}
            ev_raw = qa.get("target_step_id") or []
            evidence: list[tuple[int, int]] = []
            for e in ev_raw:
                if isinstance(e, list) and len(e) == 2:
                    evidence.append((int(e[1]), int(e[0])))  # released order: (mid, session)
                elif isinstance(e, int):
                    evidence.append((0, e))
            items.append(
                Item(
                    scenario=scenario,
                    qtype=path.stem,
                    subset=subset,
                    tid=int(t.get("tid") or t.get("gid") or 0),
                    question=str(qa.get("question") or ""),
                    choices=dict(qa.get("choices") or {}),
                    ground_truth=str(qa.get("ground_truth") or ""),
                    question_date=parse_iso_date(str(qa.get("time") or "")),
                    evidence=evidence,
                    sessions=sessions_of(t, scenario),
                    index=index,
                )
            )
    return items


def load_noise(data_dir: Path, scenario: str) -> list[Session]:
    """The released noise pools: news-shaped sessions with no facts about the
    user. ThirdNoise items hold {message} lists, FirstNoise {user, assistant}."""
    path = (
        data_dir
        / "NoiseData"
        / ("FirstNoise.json" if scenario == "participation" else "ThirdNoise.json")
    )
    if not path.exists():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: list[Session] = []
    for n in raw:
        msgs: list[dict[str, str]] = []
        for m in n.get("noise_message") or []:
            if m.get("message"):
                msgs.append({"role": "user", "content": str(m["message"])})
            if m.get("user"):
                msgs.append({"role": "user", "content": str(m["user"])})
            if m.get("assistant"):
                msgs.append({"role": "assistant", "content": str(m["assistant"])})
        if msgs:
            out.append(
                Session(sid=f"n{n.get('nid', len(out))}", date="", messages=msgs, is_noise=True)
            )
    return out


def interleave_noise(
    sessions: list[Session], noise: list[Session], k: int, rng: random.Random
) -> list[Session]:
    """Insert k noise sessions at random positions between the real ones,
    dating them by the neighbouring real session so time order holds."""
    if k <= 0 or not noise:
        return list(sessions)
    picks = rng.sample(noise, min(k, len(noise)))
    out = list(sessions)
    for p in picks:
        pos = rng.randint(0, len(out))
        date = next((s.date for s in out[pos:] if s.date and not s.is_noise), "") or next(
            (s.date for s in reversed(out[:pos]) if s.date and not s.is_noise), ""
        )
        out.insert(pos, Session(sid=p.sid, date=date, messages=p.messages, is_noise=True))
    return out


def stored_chars(sessions: list[Session]) -> int:
    return sum(len(m["content"]) for s in sessions for m in s.messages)


# ---- live run ----------------------------------------------------------------


async def _post(
    client: Any, path: str, body: dict[str, Any], token: str, retries: int = 3
) -> dict[str, Any]:
    out: dict[str, Any] = await apost_json(
        client, path, body, token=token, timeout_s=180.0, attempts=retries
    )
    return out


async def run_item(
    client: Any,
    a: argparse.Namespace,
    item: Item,
    noise: list[Session],
    sem: asyncio.Semaphore,
    reuse: dict[str, str] | None = None,
    keep: bool = False,
) -> ItemResult:
    rng = random.Random(f"{a.seed}:{item.qid}")
    sessions = interleave_noise(item.sessions, noise, a.noise, rng)
    res = ItemResult(
        qid=item.qid,
        scenario=item.scenario,
        qtype=item.qtype,
        reflective=item.qtype in REFLECTIVE_TYPES,
        letter="",
        ground_truth=item.ground_truth,
        correct=False,
        abstained=False,
        evidence_sessions=0,
        evidence_recalled=0,
        stored_chars=stored_chars(sessions),
        noise_sessions=sum(1 for s in sessions if s.is_noise),
        write_seconds=[],
        read_seconds=0.0,
        tokens_in=0,
    )
    # --namespace-json: an item whose namespace the map names is asked, not
    # ingested — the paired arm of an A/B on the read path, one ingest for both
    # arms (evidence recall is not computable without the write log).
    reused = str((reuse or {}).get(item.qid) or "")
    ns = reused or f"mb-{uuid.uuid4().hex[:10]}"
    res.namespace = ns
    if not reused:
        remember_bench_namespace(a.org, ns)  # erased when the run ends unless kept
        await register_bench_namespace(
            lambda p, b: _post(client, p, b, a.token),
            org=a.org,
            namespace=ns,
            description=f"MemBench item {item.qid}",
            keep=keep,
        )
    ident = {"org": a.org, "namespace": ns, "agent": "membench", "user_id": "-"}
    key_to_sessions: dict[str, set[str]] = {}
    step_to_session = {step: s.sid for s in sessions for step in s.step_ids}
    try:
        async with sem:
            for s in [] if reused else sessions:  # time order matters: updates supersede
                body: dict[str, Any] = {
                    **ident,
                    "messages": s.messages[:400],
                    "session_id": s.sid,
                    "max_tokens": 4096,
                    **({"judge_extraction": True} if getattr(a, "judge_extraction", False) else {}),
                }
                if s.date:
                    body["observation_date"] = s.date
                t0 = time.perf_counter()
                resp = await _post(client, "/v1/memory.set_from_messages", body, a.token)
                res.write_seconds.append(time.perf_counter() - t0)
                res.tokens_in += int(resp.get("tokens_in") or 0)
                for w in resp.get("written") or []:
                    k = str(w.get("memory_key") or "")
                    if k:
                        key_to_sessions.setdefault(k, set()).add(s.sid)
            query = build_query(
                item.question,
                item.choices,
                item.question_date,
                aggregation=getattr(a, "aggregation", "entities")
                if item.qtype == "Aggregative"
                else "entities",
            )
            body = {
                **ident,
                "agent": "*",
                "query": query,
                "top_k": a.top_k,
                "max_answer_chars": 200,
                "consumer": "membench",
                # The item's namespace only: the default namespace_scope
                # "all_mine" fans a wildcard bench token out over EVERY
                # namespace on the service, and another bench's facts
                # outranked this item's (measured: a MemoryAgentBench
                # namespace's rows at the top of a MemBench ask).
                "namespace_scope": "this",
                # An evaluation read credits nothing unless --credit.
                **measurement_fields("ask", credit=bool(getattr(a, "credit", False))),
                **(
                    {"world_knowledge": a.world_knowledge}
                    if getattr(a, "world_knowledge", "")
                    else {}
                ),
                **(json.loads(a.ask_json) if getattr(a, "ask_json", "") else {}),
            }
            # --repeats N: the same ask N times; the item keeps
            # the MAJORITY verdict, every run's label and the window pin.
            runs: list[tuple[dict[str, Any], float]] = []
            for _rep in range(max(1, int(getattr(a, "repeats", 1) or 1))):
                t0 = time.perf_counter()
                ask_n = await _post(client, "/v1/memory.ask", body, a.token)
                runs.append((ask_n, time.perf_counter() - t0))
            ask, res.read_seconds = runs[0]
        res.abstained = bool(ask.get("abstain")) or ask.get("source") == "abstained"
        res.answer = str(ask.get("answer") or "")[:300]
        res.abstain_reason = str(ask.get("abstain_reason") or "")[:120]
        res.degraded = ",".join(sorted(str(d) for d in (ask.get("degraded") or [])))[:120]
        res.letter = parse_letter(res.answer)
        run_labels = [
            "CORRECT"
            if (parse_letter(str(r.get("answer") or "")) or "") == item.ground_truth
            else "WRONG"
            for r, _ in runs
        ]
        res.correct = majority_label(run_labels) == "CORRECT"
        if len(runs) > 1:
            res.repeat_labels = "|".join(run_labels)
            res.repeat_agreement = agreement(run_labels)
            res.repeat_window_identical = identical_share(
                [
                    tuple(str(h.get("memory_key") or "") for h in (r.get("results") or []))
                    for r, _ in runs
                ]
            )
        returned = [str(h.get("memory_key") or "") for h in (ask.get("results") or [])[: a.top_k]]
        res.evidence_sessions, res.evidence_recalled = evidence_recall(
            item.evidence, step_to_session, returned, key_to_sessions
        )
        res.evidence_precision = evidence_precision(
            item.evidence, step_to_session, returned, key_to_sessions
        )
    except Exception as e:  # noqa: BLE001 — record and keep the run going
        res.error = f"{type(e).__name__}: {str(e)[:200]}"
    return res


# ---- summary -----------------------------------------------------------------


def _p50(xs: list[float]) -> float:
    return round(statistics.median(xs), 3) if xs else 0.0


def summarize(results: list[ItemResult]) -> dict[str, Any]:
    ok = [r for r in results if not r.error]
    by: dict[str, list[ItemResult]] = {}
    for r in ok:
        by.setdefault(f"{r.scenario}/{r.qtype}", []).append(r)

    def block(rows: list[ItemResult]) -> dict[str, Any]:
        ev = [(r.evidence_sessions, r.evidence_recalled) for r in rows if r.evidence_sessions]
        return {
            "n": len(rows),
            "accuracy": round(sum(r.correct for r in rows) / len(rows), 3) if rows else None,
            "abstained": sum(r.abstained for r in rows),
            "no_letter": sum(1 for r in rows if not r.letter and not r.abstained),
            "evidence_recall": round(sum(g for _, g in ev) / sum(n for n, _ in ev), 3)
            if ev
            else None,
            "evidence_precision": round(
                sum(r.evidence_precision for r in rows if r.evidence_sessions)
                / max(1, sum(1 for r in rows if r.evidence_sessions)),
                3,
            )
            if ev
            else None,
            "write_s_p50": _p50([w for r in rows for w in r.write_seconds]),
            "read_s_p50": _p50([r.read_seconds for r in rows]),
            "stored_chars_mean": int(sum(r.stored_chars for r in rows) / len(rows)) if rows else 0,
            "noise_sessions_mean": round(sum(r.noise_sessions for r in rows) / len(rows), 1)
            if rows
            else 0,
        }

    summary: dict[str, Any] = {
        "n": len(results),
        "errors": len(results) - len(ok),
        "by_type": {k: block(v) for k, v in sorted(by.items())},
        # Per-item flip rate over --repeats labels (None = single run).
        "flip_rate": flip_rate(
            [lb for lb in str(r.repeat_labels or "").split("|") if lb] for r in ok
        ),
        "window_identical_rate": (
            round(
                sum(r.repeat_window_identical >= 1.0 for r in ok if r.repeat_labels)
                / max(1, sum(1 for r in ok if r.repeat_labels)),
                4,
            )
            if any(r.repeat_labels for r in ok)
            else None
        ),
    }
    for level, pred in (
        ("factual", lambda r: not r.reflective),
        ("reflective", lambda r: r.reflective),
    ):
        rows = [r for r in ok if pred(r)]
        if rows:
            summary[level] = block(rows)
    for scen in ("observation", "participation"):
        rows = [r for r in ok if r.scenario == scen]
        if rows:
            summary[scen] = block(rows)
    return summary


def print_summary(s: dict[str, Any]) -> None:
    print("\n=== membench ===")
    print(f"items: {s['n']}  errors(excluded): {s['errors']}   (multiple choice: chance = 0.25)")
    if s.get("flip_rate") is not None:
        print(
            f"repeats: per-item flip rate {100 * s['flip_rate']:.1f}%  "
            f"window identical across repeats {s.get('window_identical_rate')}"
        )
    head = f"  {'type':<36}{'n':>4}{'acc':>7}{'ev_rec':>8}{'abst':>6}"
    print(head + f"{'wt_s':>7}{'rt_s':>7}{'chars':>9}{'noise':>6}")
    for k, b in s["by_type"].items():
        acc = f"{b['accuracy']:.3f}" if b["accuracy"] is not None else "-"
        ev = f"{b['evidence_recall']:.3f}" if b["evidence_recall"] is not None else "-"
        print(
            f"  {k:<36}{b['n']:>4}{acc:>7}{ev:>8}{b['abstained']:>6}{b['write_s_p50']:>7.2f}"
            f"{b['read_s_p50']:>7.2f}{b['stored_chars_mean']:>9}{b['noise_sessions_mean']:>6}"
        )
    for k in ("factual", "reflective", "observation", "participation"):
        if k in s:
            b = s[k]
            print(
                f"  {k:<14} n={b['n']:<4} accuracy={b['accuracy']}"
                f"  evidence_recall={b['evidence_recall']}"
                f"  write_s_p50={b['write_s_p50']}  read_s_p50={b['read_s_p50']}"
            )


def compare_command(a: argparse.Namespace) -> int:
    """Paired gate of two result files."""
    rows_a = json.loads(Path(a.compare[0]).read_text()).get("rows") or []
    rows_b = json.loads(Path(a.compare[1]).read_text()).get("rows") or []

    def _labels(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in rows:
            if r.get("error"):
                continue
            reps = [x for x in str(r.get("repeat_labels") or "").split("|") if x]
            out[str(r["qid"])] = reps or ["CORRECT" if r.get("correct") else "WRONG"]
        return out

    g = gate_from_args(
        a,
        _labels(rows_a),
        _labels(rows_b),
        clusters={str(r["qid"]): str(r.get("qtype", "")) for r in rows_b},
    )
    print("\n".join(g.lines()))
    if a.out:
        Path(a.out).write_text(json.dumps(g.as_dict(), indent=1))
    return 0


# ---- cli ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data-dir", required=True, help="unpacked Membenchdata directory")
    ap.add_argument("--scenario", choices=["observation", "participation"], default="observation")
    ap.add_argument(
        "--types",
        default="Single-hop,Multi-hop,Comparative,Aggregative,knowledge_updating,post_processing,Preference",
        help="comma list of categorical file stems (case-insensitive)",
    )
    ap.add_argument("--limit", type=int, default=10, help="items per type")
    ap.add_argument(
        "--noise", type=int, default=0, help="noise sessions inserted per item (~1k tokens each)"
    )
    ap.add_argument("--top-k", type=int, default=15)
    ap.add_argument(
        "--aggregation",
        choices=["entities", "statements"],
        default="entities",
        help="counting convention stated in Aggregative questions: loom's default (one "
        "entity, latest value) or MemBench's (every statement counts; see build_query)",
    )
    ap.add_argument(
        "--world-knowledge",
        choices=["", "forbid", "allow"],
        default="",
        help="memory.ask world_knowledge mode sent with every question ('' = the server's "
        "default). Post_processing items match a stored city to a description of it, "
        "which the transcript never states — 'allow' lets the reader bridge that step.",
    )
    ap.add_argument(
        "--judge-extraction",
        action="store_true",
        help="ask memory.set_from_messages to grade each extracted fact against the transcript "
        "(judge_extraction) — measures the write-side attribution errors seen on Post_processing",
    )
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--base-url", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--org", default="dev")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="")
    ap.add_argument(
        "--ask-json",
        default="",
        help="JSON merged into every memory.ask body (e.g. '{\"deep_first\": false}' for "
        "one arm of a read-path A/B on one server; the shape of locomo_qa.py's flag)",
    )
    ap.add_argument(
        "--namespace-json",
        default="",
        help="qid → namespace map: written after an ingesting run, and when the file "
        "exists its items are asked without ingest (the paired arm of a read-path A/B)",
    )
    ap.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="Do not erase this run's mb-* namespaces at the end (implied by "
        "--namespace-json, whose map a later run reuses)",
    )
    ap.add_argument(
        "--dry", action="store_true", help="load + normalise only; oracle answers; no server"
    )
    ap.add_argument(
        "--compare",
        nargs=2,
        metavar=("A.json", "B.json"),
        help="no server: paired gate of two --out files (rows pair by qid, clusters = "
        "qtype) — delta, 95%% clustered bootstrap CI over majority labels, flip rate, "
        "verdict",
    )
    add_gate_arguments(ap)  # --repeats / --gate* / --credit / --judge-calibration
    a = ap.parse_args(argv)
    if a.compare:
        return compare_command(a)

    data_dir = Path(a.data_dir)
    types = [t.strip() for t in a.types.split(",") if t.strip()]
    items = load_items(data_dir, a.scenario, types, a.limit, a.seed)
    kinds = ", ".join(sorted({i.qtype for i in items}))
    print(f"loaded {len(items)} items ({a.scenario}; types {kinds})")
    if not items:
        return 2
    noise = load_noise(data_dir, a.scenario) if a.noise else []

    if a.dry:
        rng = random.Random(a.seed)
        results = []
        for it in items:
            sessions = interleave_noise(it.sessions, noise, a.noise, rng)
            step_to_session = {step: s.sid for s in sessions for step in s.step_ids}
            needed = {step_to_session[e] for e in it.evidence if e in step_to_session}
            results.append(
                ItemResult(
                    qid=it.qid,
                    scenario=it.scenario,
                    qtype=it.qtype,
                    reflective=it.qtype in REFLECTIVE_TYPES,
                    letter=it.ground_truth,
                    ground_truth=it.ground_truth,
                    correct=True,
                    abstained=False,
                    evidence_sessions=len(needed),
                    evidence_recalled=len(needed),
                    stored_chars=stored_chars(sessions),
                    noise_sessions=sum(s.is_noise for s in sessions),
                    write_seconds=[0.0],
                    read_seconds=0.0,
                    tokens_in=0,
                )
            )
        s = summarize(results)
        print_summary(s)
        unmapped = sum(
            1
            for it in items
            if it.evidence
            and not any(
                e in {st for sess in it.sessions for st in sess.step_ids} for e in it.evidence
            )
        )
        assert s["errors"] == 0 and s["factual"]["accuracy"] == 1.0 if "factual" in s else True
        print(f"--dry self-check passed; evidence ids unmapped to a session: {unmapped}")
        return 0

    if not a.base_url:
        print("--base-url required for a live run", file=sys.stderr)
        return 2
    import httpx

    reuse: dict[str, str] = {}
    ns_path = Path(a.namespace_json) if getattr(a, "namespace_json", "") else None
    if ns_path is not None and ns_path.exists():
        reuse = {
            str(k): str(v)
            for k, v in (json.loads(ns_path.read_text(encoding="utf-8")) or {}).items()
        }
        print(f"reusing {len(reuse)} namespaces from {ns_path} (ask only, no ingest)")

    # A run that writes a namespace map is ingesting for a later ask-only run:
    # its namespaces must outlive it.
    keep = bool(a.keep_namespaces or ns_path is not None)

    async def go() -> list[ItemResult]:
        sem = asyncio.Semaphore(a.concurrency)
        async with httpx.AsyncClient(base_url=a.base_url) as client:

            async def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
                return await _post(client, path, body, a.token)

            async with erase_on_exit(post, org=a.org, keep=keep):
                return await asyncio.gather(
                    *(run_item(client, a, it, noise, sem, reuse, keep) for it in items)
                )

    results = asyncio.run(go())
    s = summarize(results)
    s["namespaces"] = {r.qid: r.namespace for r in results if r.namespace}
    if ns_path is not None and not ns_path.exists():
        ns_path.write_text(json.dumps(s["namespaces"], indent=1), encoding="utf-8")
        print(f"wrote {len(s['namespaces'])} namespaces to {ns_path}")
    s["config"] = {
        "scenario": a.scenario,
        "types": types,
        "limit": a.limit,
        "noise": a.noise,
        "top_k": a.top_k,
        "seed": a.seed,
        "aggregation": getattr(a, "aggregation", "entities"),
        "world_knowledge": getattr(a, "world_knowledge", ""),
        "judge_extraction": bool(getattr(a, "judge_extraction", False)),
    }
    print_summary(s)
    for r in results:
        if r.error:
            print(f"  error {r.qid}: {r.error}", file=sys.stderr)
    if a.out:
        Path(a.out).write_text(
            json.dumps({"summary": s, "rows": [asdict(r) for r in results]}, indent=1)
        )
        print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
