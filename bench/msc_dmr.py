"""MSC deep-memory retrieval against loom — the summary-versus-raw contrast.

The task takes Multi-Session Chat (five sessions between two personas, about a dozen
turns each), writes a "memory challenge" question for a sixth session that can only be
answered from the earlier chat logs — not from the persona summaries — and grades the
answer with a generous LLM judge (CORRECT if it touches the gold topic) plus ROUGE-L
recall. A reader that sees only a lossy recursive summary of the five sessions scores
far below one that can search the raw history. It is the one public bench with a
built-in summary-only arm, which is the control LoCoMo lacks: it measures what a
raw-turn read recovers over digests.

Data: the authors' released self-instruct file `msc_self_instruct.jsonl`
(500 chats; Apache-2.0) — the Hugging Face dataset `MSC-Self-Instruct`.
Each row carries `previous_dialogs` (four sessions with `time_back`), `dialog`
(the fifth), and `self_instruct` = {B: question, A: gold answer}.

Pipeline per chat (one throwaway namespace): the five sessions are ingested
in date order with `memory.set_from_messages` — both speakers are humans, so
every turn is a `user` message prefixed with the speaker and
carries a per-message timestamp derived from `time_back`. Then the
question is answered under each arm and judged:

  ask            memory.ask — loom's own answer path.
  search         memory.search top_k → external reader (the retriever-reader
                 baseline).
  digest         memory.search, keep only `source="summary"` hits → reader.
                 The summary-only baseline: digests only.
  search_recall  memory.search hits + memory.recall(question, match=any,
                 relevance) raw turns → reader: the summary/fact pool
                 plus a raw-turn leg.
  search_reader  memory.search(render="reader", top_k=15): the server's own
                 reader block and instructions handed to the
                 external reader — what an MCP agent would get if loom_search
                 returned them. Measures how much of the search→reader gap
                 against `ask` is the block, not the machinery behind ask.

`--reuse prior.jsonl` maps each chat's `initial_data_id` to the namespace an
earlier run ingested it into and skips ingest for those chats, so an arm can
be added to a finished run without paying the 150 extraction calls again. The
earlier run must have used `--keep-namespaces`; otherwise its namespaces were
erased when it exited.

Reader and judge are pinned independently of the server's model
(BENCH_READER_MODEL, default gpt-4o-mini; BENCH_JUDGE_MODEL, default
gpt-4.1). The judge prompt is the task's published one, verbatim.

    LOOM_API_KEY=... OPENAI_API_KEY=... python bench/msc_dmr.py \\
        --data msc_self_instruct.jsonl --n 30 --base-url http://localhost:7777 \\
        --arms ask,search,digest,search_recall --out msc_dmr.jsonl

Cost at --n 30: 150 extraction calls on the server's LLM, ~90 reader and
~120 judge calls. The full 500 is ~17x that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/x.py` puts bench/ itself on sys.path
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )

ANCHOR = datetime(2024, 1, 15, 18, 0)

# The task's published judge prompt, verbatim; wrapped only at spaces.
JUDGE_PROMPT = "\n".join(
    [
        "Your task is to label an answer to a question as 'CORRECT' or 'WRONG'.",
        (
            "You will be given the following data: (1) a question (posed by one user to "
            "another user), (2) a 'gold' (ground truth) answer, (3) a generated answer "
            "which you will score as CORRECT/WRONG."
        ),
        (
            "The point of the question is to ask about something one user should know about "
            "the other user based on their prior conversations."
        ),
        (
            "The gold answer will usually be a concise and short answer that includes the "
            "referenced topic, for example:"
        ),
        "Question: Do you remember what I got the last time I went to Hawaii?",
        "Gold answer: A shell necklace",
        (
            "The generated answer might be much longer, but you should be generous with "
            "your grading - as long as it touches on the same topic as the gold answer, it "
            "should be counted as CORRECT."
        ),
        "For example, the following answers would be considered CORRECT:",
        (
            "Generated answer (CORRECT): Oh yeah, that was so fun! I got so much stuff "
            "there, including that shell necklace."
        ),
        (
            "Generated answer (CORRECT): I got a ton of stuff... that surfboard, the mug, "
            "the necklace, those coasters too.."
        ),
        "Generated answer (CORRECT): That cute necklace",
        "The following answers would be considered WRONG:",
        (
            "Generated answer (WRONG): Oh yeah, that was so fun! I got so much stuff there, "
            "including that mug."
        ),
        (
            "Generated answer (WRONG): I got a ton of stuff... that surfboard, the mug, "
            "those coasters too.."
        ),
        ("Generated answer (WRONG): I'm sorry, I don't remember what you're talking about."),
        "Now it's time for the real question:",
        "Question: {question}",
        "Gold answer: {gold}",
        "Generated answer: {generated}",
        (
            "First, provide a short (one sentence) explanation of your reasoning, then "
            "finish with CORRECT or WRONG. Do NOT include both CORRECT and WRONG in your "
            "response, or it will break the evaluation script."
        ),
    ]
)

READER_PROMPT = (
    "You are answering a question one person asked the other about their earlier "
    "conversations. Use ONLY the memories below. If they do not contain the answer, "
    "say you don't remember. Be concise.\n\nMemories:\n{memories}\n\nQuestion: {question}\nAnswer:"
)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_TIME_BACK_RE = re.compile(r"(\d+)\s*(day|hour|week|month)s?", re.I)


def parse_time_back(text: str) -> timedelta:
    """'7 days 8 hours ago' → timedelta(days=7, hours=8); unparseable → 0."""
    days = 0.0
    for n, unit in _TIME_BACK_RE.findall(text or ""):
        v = int(n)
        u = unit.lower()
        days += v if u == "day" else v / 24 if u == "hour" else v * 7 if u == "week" else v * 30
    return timedelta(days=days)


def sessions_of(row: dict[str, Any], anchor: datetime = ANCHOR) -> list[dict[str, Any]]:
    """The chat's sessions oldest-first, each with a date and its turns."""
    out: list[dict[str, Any]] = []
    for prev in row.get("previous_dialogs") or []:
        out.append(
            {
                "date": anchor - parse_time_back(str(prev.get("time_back") or "")),
                "turns": list(prev.get("dialog") or []),
            }
        )
    out.append({"date": anchor, "turns": list(row.get("dialog") or [])})
    out.sort(key=lambda s: s["date"])
    return out


def turn_to_message(turn: dict[str, Any], when: datetime) -> dict[str, Any]:
    speaker = str(turn.get("id") or "Speaker").strip()
    text = str(turn.get("text") or "").strip()
    return {
        "role": "user",
        "content": f"{speaker}: {text}",
        "name": speaker,
        "timestamp": when.isoformat(),
    }


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (s or "").lower())


def rouge_l_recall(prediction: str, gold: str) -> float:
    """LCS(prediction, gold) / len(gold) over lowercase alphanumeric tokens —
    the task's metric for verbose answers against short golds."""
    p, g = _tokens(prediction), _tokens(gold)
    if not g:
        return 0.0
    prev = [0] * (len(g) + 1)
    for pt in p:
        cur = [0] * (len(g) + 1)
        for j, gt in enumerate(g, 1):
            cur[j] = prev[j - 1] + 1 if pt == gt else max(prev[j], cur[j - 1])
        prev = cur
    return prev[-1] / len(g)


def parse_verdict(text: str) -> int | None:
    t = (text or "").upper()
    has_c, has_w = "CORRECT" in t.replace("INCORRECT", ""), "WRONG" in t or "INCORRECT" in t
    if has_c and not has_w:
        return 1
    if has_w and not has_c:
        return 0
    # Both or neither: take the last word the judge wrote, as the reference
    # script would.
    last = re.findall(r"CORRECT|WRONG", t)
    return None if not last else (1 if last[-1] == "CORRECT" else 0)


def load_rows(path: Path, n: int, seed: int) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.open() if line.strip()]
    rows = [
        r
        for r in rows
        if (r.get("self_instruct") or {}).get("B") and (r.get("self_instruct") or {}).get("A")
    ]
    rng = random.Random(seed)
    rng.shuffle(rows)
    return rows[: max(0, n)] if n > 0 else rows


# ---------------------------------------------------------------------------
# Wire
# ---------------------------------------------------------------------------


# Retry posture: a 429 is the per-token limiter and honours Retry-After; a 5xx
# is the server or its store — a table restart (TABLE_IS_BEING_RESTARTED after a Keeper
# session expiry) can outlast a 30-second backoff. Eight attempts with a 45-second cap
# wait about three minutes in total, which a rolling table restart fits inside; a longer
# outage still fails the task, and the run reports it rather than hanging.
MAX_ATTEMPTS = 8
BACKOFF_CAP_S = 45.0


def backoff_seconds(attempt: int, status: int, retry_after: float = 0.0) -> float:
    """Seconds to wait after `attempt` (0-based) failed with `status`."""
    wait = min(1.0 * (2**attempt), BACKOFF_CAP_S)
    return max(wait, retry_after) if status == 429 else wait


async def _post(client: Any, url: str, body: dict[str, Any], token: str = "") -> dict[str, Any]:
    import httpx

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    last: Any = None
    for attempt in range(MAX_ATTEMPTS):
        r = await client.post(url, json=body, headers=headers, timeout=300.0)
        if r.status_code < 500 and r.status_code != 429:
            r.raise_for_status()
            return r.json()
        last = r
        try:
            retry_after = float(r.headers.get("retry-after") or 0)
        except ValueError:
            retry_after = 0.0
        await asyncio.sleep(backoff_seconds(attempt, r.status_code, retry_after))
    if last is not None:
        last.raise_for_status()
    raise httpx.HTTPError("unreachable")


async def _chat(client: Any, model: str, api_key: str, prompt: str, system: str = "") -> str:
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    r = await client.post(
        base + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": model, "temperature": 0.0, "messages": messages},
        timeout=90.0,
    )
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


async def ingest(
    client: Any,
    row: dict[str, Any],
    *,
    base_url: str,
    token: str,
    identity: dict[str, Any],
    ingest_json: dict[str, Any],
) -> int:
    t0 = time.perf_counter()
    for i, sess in enumerate(sessions_of(row)):
        when = sess["date"]
        messages = [
            turn_to_message(t, when + timedelta(minutes=j)) for j, t in enumerate(sess["turns"])
        ]
        if not messages:
            continue
        await _post(
            client,
            base_url + "/v1/memory.set_from_messages",
            {
                **identity,
                "session_id": f"{identity['namespace']}:s{i + 1}",
                "messages": messages,
                "observation_date": when.date().isoformat(),
                **ingest_json,
            },
            token,
        )
    return int((time.perf_counter() - t0) * 1000)


def _mem_lines(hits: list[dict[str, Any]]) -> list[str]:
    out = []
    for h in hits:
        date = str(h.get("valid_at") or h.get("created_at") or "")[:10]
        out.append(f"- [{date}] {h.get('content_excerpt', '')}")
    return out


async def run_one(
    client: Any,
    row: dict[str, Any],
    *,
    base_url: str,
    token: str,
    org: str,
    arms: list[str],
    top_k: int,
    reader_model: str,
    judge_model: str,
    api_key: str,
    ingest_json: dict[str, Any],
    ask_json: dict[str, Any],
    reuse: dict[str, str] | None = None,
    keep: bool = False,
) -> dict[str, Any]:
    chat_id = str((row.get("metadata") or {}).get("initial_data_id") or "")
    ns = (reuse or {}).get(chat_id, "")
    reused = bool(ns)
    if not ns:
        ns = f"bench-mscdmr-{uuid.uuid4().hex[:10]}"
        remember_bench_namespace(org, ns)
        await register_bench_namespace(
            lambda p, b: _post(client, base_url + p, b, token),
            org=org,
            namespace=ns,
            description=f"MSC chat {chat_id}",
            keep=keep,
        )
    identity = {"org": org, "namespace": ns, "agent": "msc-dmr-bench", "user_id": "-"}
    question = str(row["self_instruct"]["B"]).strip()
    gold = str(row["self_instruct"]["A"]).strip()
    out: dict[str, Any] = {
        "id": chat_id or ns,
        "namespace": ns,
        "question": question,
        "gold": gold,
        "reused": reused,
        "ingest_ms": 0
        if reused
        else await ingest(
            client, row, base_url=base_url, token=token, identity=identity, ingest_json=ingest_json
        ),
    }

    async def _judge(answer: str) -> int | None:
        if not api_key:
            return None
        text = await _chat(
            client,
            judge_model,
            api_key,
            JUDGE_PROMPT.format(question=question, gold=gold, generated=answer or "(no answer)"),
        )
        return parse_verdict(text)

    async def _record(arm: str, answer: str, ms: int, extra: dict[str, Any] | None = None) -> None:
        out[f"{arm}_answer"] = answer[:400]
        out[f"{arm}_ms"] = ms
        out[f"{arm}_rouge_l_r"] = round(rouge_l_recall(answer, gold), 4)
        v = await _judge(answer)
        if v is not None:
            out[f"{arm}_correct"] = v
        if extra:
            out.update({f"{arm}_{k}": v for k, v in extra.items()})

    search_body = {**identity, "query": question, "top_k": top_k, "namespace_scope": "this"}
    hits: list[dict[str, Any]] | None = None

    async def _hits() -> list[dict[str, Any]]:
        nonlocal hits
        if hits is None:
            resp = await _post(client, base_url + "/v1/memory.search", search_body, token)
            hits = list(resp.get("results") or [])
        return hits

    if "ask" in arms:
        t0 = time.perf_counter()
        resp = await _post(
            client,
            base_url + "/v1/memory.ask",
            {
                **identity,
                "query": question,
                "top_k": top_k,
                "namespace_scope": "this",
                **ask_json,
            },
            token,
        )
        await _record(
            "ask",
            str(resp.get("answer") or ""),
            int((time.perf_counter() - t0) * 1000),
            {"abstain": int(bool(resp.get("abstain"))), "sources": len(resp.get("sources") or [])},
        )
    if "search" in arms and api_key:
        t0 = time.perf_counter()
        hs = await _hits()
        ans = await _chat(
            client,
            reader_model,
            api_key,
            READER_PROMPT.format(memories="\n".join(_mem_lines(hs)) or "(none)", question=question),
        )
        await _record("search", ans, int((time.perf_counter() - t0) * 1000), {"hits": len(hs)})
    if "digest" in arms and api_key:
        t0 = time.perf_counter()
        resp = await _post(
            client, base_url + "/v1/memory.search", {**search_body, "top_k": 40}, token
        )
        digests = [h for h in resp.get("results") or [] if str(h.get("source") or "") == "summary"]
        ans = await _chat(
            client,
            reader_model,
            api_key,
            READER_PROMPT.format(
                memories="\n".join(_mem_lines(digests)) or "(none)", question=question
            ),
        )
        await _record("digest", ans, int((time.perf_counter() - t0) * 1000), {"hits": len(digests)})
    if "search_reader" in arms and api_key:
        t0 = time.perf_counter()
        resp = await _post(
            client,
            base_url + "/v1/memory.search",
            {**search_body, "top_k": max(top_k, 15), "render": "reader"},
            token,
        )
        block = str(resp.get("reader_block") or "")
        instr = str(resp.get("reader_instructions") or "")
        ans = await _chat(
            client,
            reader_model,
            api_key,
            f"{block or '(no memories)'}\n\nQuestion: {question}",
            system=instr,
        )
        await _record(
            "search_reader",
            ans,
            int((time.perf_counter() - t0) * 1000),
            {"hits": len(resp.get("results") or []), "block_chars": len(block)},
        )
    if "search_block" in arms and api_key:
        # The block alone, read with the generic prompt: isolates the block's
        # value (dates, ask order, whole excerpts) from the instructions'.
        t0 = time.perf_counter()
        resp = await _post(
            client,
            base_url + "/v1/memory.search",
            {**search_body, "top_k": max(top_k, 15), "render": "reader"},
            token,
        )
        block = str(resp.get("reader_block") or "")
        ans = await _chat(
            client,
            reader_model,
            api_key,
            READER_PROMPT.format(memories=block or "(none)", question=question),
        )
        await _record(
            "search_block",
            ans,
            int((time.perf_counter() - t0) * 1000),
            {"hits": len(resp.get("results") or []), "block_chars": len(block)},
        )
    if "search_recall" in arms and api_key:
        t0 = time.perf_counter()
        hs = await _hits()
        turns: list[dict[str, Any]] = []
        try:
            rr = await _post(
                client,
                base_url + "/v1/memory.recall",
                {
                    **identity,
                    "query": question,
                    "match": "any",
                    "order": "relevance",
                    "top_k": 8,
                    "max_chars": 400,
                },
                token,
            )
            turns = list(rr.get("turns") or [])
        except Exception as e:  # noqa: BLE001 — the op may be absent on an older server
            out["search_recall_error"] = str(e)[:120]
        lines = _mem_lines(hs) + [
            f"- [{str(t.get('observed_at') or '')[:10]}] (said) {t.get('content', '')}"
            for t in turns
        ]
        ans = await _chat(
            client,
            reader_model,
            api_key,
            READER_PROMPT.format(memories="\n".join(lines) or "(none)", question=question),
        )
        await _record(
            "search_recall",
            ans,
            int((time.perf_counter() - t0) * 1000),
            {"hits": len(hs), "turns": len(turns)},
        )
    return out


def summarize(rows: list[dict[str, Any]], arms: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"n": len(rows)}
    for arm in arms:
        c = [r[f"{arm}_correct"] for r in rows if f"{arm}_correct" in r]
        rl = [r[f"{arm}_rouge_l_r"] for r in rows if f"{arm}_rouge_l_r" in r]
        ms = sorted(r[f"{arm}_ms"] for r in rows if f"{arm}_ms" in r)
        cell: dict[str, Any] = {}
        if c:
            cell["accuracy"] = round(sum(c) / len(c), 4)
            cell["judged"] = len(c)
        if rl:
            cell["rouge_l_r"] = round(sum(rl) / len(rl), 4)
        if ms:
            cell["ms_p50"] = ms[len(ms) // 2]
        if cell:
            out[arm] = cell
    if rows:
        out["ingest_ms_p50"] = sorted(r["ingest_ms"] for r in rows)[len(rows) // 2]
    return out


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    rows = load_rows(Path(args.data), args.n, args.seed)
    if not rows:
        print("no rows loaded", file=sys.stderr)
        return 1
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and any(a != "ask" for a in arms):
        print("OPENAI_API_KEY is required for the reader/judge arms", file=sys.stderr)
        return 2
    ingest_json = json.loads(args.ingest_json) if args.ingest_json else {}
    ask_json = json.loads(args.ask_json) if args.ask_json else {}
    reuse: dict[str, str] = {}
    if args.reuse:
        for line in Path(args.reuse).open():
            if line.strip():
                prior = json.loads(line)
                if prior.get("id") and prior.get("namespace"):
                    reuse[str(prior["id"])] = str(prior["namespace"])
    results: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=300.0) as client:

        async def _one(row: dict[str, Any]) -> None:
            async with sem:
                try:
                    results.append(
                        await run_one(
                            client,
                            row,
                            base_url=args.base_url.rstrip("/"),
                            token=args.token,
                            org=args.org,
                            arms=arms,
                            top_k=args.top_k,
                            reader_model=args.reader_model,
                            judge_model=args.judge_model,
                            api_key=api_key,
                            ingest_json=ingest_json,
                            ask_json=ask_json,
                            reuse=reuse,
                            keep=args.keep_namespaces,
                        )
                    )
                except Exception as e:  # noqa: BLE001 — one bad chat must not sink the run
                    print(f"row failed: {e}", file=sys.stderr)

        # Namespaces this run minted are erased on exit, after a failure or
        # Ctrl-C too; reused ones were never remembered and stay.
        async with erase_on_exit(
            lambda p, b: _post(client, args.base_url.rstrip("/") + p, b, args.token),
            org=args.org,
            keep=args.keep_namespaces,
        ):
            await asyncio.gather(*[_one(r) for r in rows])
        summary = summarize(results, arms)
        summary.update(
            {
                "arms": arms,
                "reader_model": args.reader_model,
                "judge_model": args.judge_model,
                "top_k": args.top_k,
            }
        )
        print(json.dumps(summary, indent=2))
        if args.out:
            with open(args.out, "w") as f:
                for r in results:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--data", required=True, help="msc_self_instruct.jsonl")
    ap.add_argument("--n", type=int, default=30, help="chats to sample (0 = all 500)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default=os.environ.get("LOOM_ORG", "dev"))
    ap.add_argument("--arms", default="ask,search,digest,search_recall")
    ap.add_argument(
        "--reuse",
        default="",
        help="per-chat JSONL of an earlier run; its namespaces are reused and ingest skipped",
    )
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--reader-model", default=os.environ.get("BENCH_READER_MODEL", "gpt-4o-mini"))
    ap.add_argument("--judge-model", default=os.environ.get("BENCH_JUDGE_MODEL", "gpt-4.1"))
    ap.add_argument("--ingest-json", default="", help="extra set_from_messages fields")
    ap.add_argument("--ask-json", default="", help="extra memory.ask fields")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--out", default="", help="per-chat JSONL")
    ap.add_argument("--keep-namespaces", action="store_true")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
