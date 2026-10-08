"""LongMemEval failure-mode diagnostic.

For each failed item (selected from a results CSV by its judge_label),
re-run end-to-end and dump:
- Number of memories extracted per session
- Top-K retrieved excerpts
- Synthesized answer
- Gold answer

The judge verdict itself comes from the results CSV (that's the
--only-wrong filter); this tool re-traces the pipeline, it does not
re-judge. Use it to identify *systematic* failure modes (e.g. "the fact
was never extracted" vs "it was extracted but search ranked it 35th").

Each item is also labelled with a failure STAGE (bench/failure_taxonomy.py
classify_answer_failure): retrieval_ranking (no gold-session memory in the
top-k), reader (the gold text was in an excerpt and the answer was still
wrong), compression_loss (the gold text is in the raw source turns linked to
a retrieved memory but in none of its excerpts — the extractor compressed it
away: the over-compression case), not_verbatim (a
computed or paraphrased gold the substring probe cannot see). The evidence
read goes through server.core.ch (LOOM_CH_* in .env, run from the repo root) and
is skipped with a note when no ClickHouse is reachable. The report opens
with the stage histogram — the retrieval-vs-compression split.

Run:
    OPENAI_API_KEY=... python bench/lme_diagnose.py \
        --csv bench/longmemeval.csv \
        --dataset bench/datasets/longmemeval_oracle.json \
        --limit 10 --only-wrong --out bench/lme_diagnosis.md
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# The main bench's date parser: LongMemEval dates are '2023/04/10 (Mon) 17:50'.
from bench._http import apost_json
from bench.bench_namespaces import erase_on_exit, remember_bench_namespace
from bench.longmemeval import _to_iso_date

# Any OpenAI-compatible endpoint (gateway, proxy, vLLM) — mirrors
# bench/longmemeval.py so a diagnosis run hits the same LLM backend.
_OPENAI_CHAT_URL = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip(
    "/"
) + "/chat/completions"


async def _post(client, url, body, token=""):
    return await apost_json(client, url, body, token=token, timeout_s=180.0)


_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    """Lowercase, collapse non-alphanumerics — the main bench's substring probe."""
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def gold_in(gold: str, texts: list[str]) -> bool:
    """Is the normalised gold answer a substring of any normalised text?"""
    g = _norm(gold)
    return bool(g) and any(g in _norm(t) for t in texts)


async def linked_evidence_texts(
    org: str, namespace: str, memory_keys: list[str], *, ch=None, limit: int = 400
) -> list[str] | None:
    """Raw source turns linked to `memory_keys` (memory_belief_sources →
    memory_evidence). None when ClickHouse is not reachable, so the caller
    can tell "no evidence" from "could not look"."""
    if not memory_keys:
        return []
    try:
        if ch is None:
            from server.core.ch import get_client

            ch = get_client()
            # The process-level client is lazy; a bench process that never
            # served a request has not connected it (owners_precision.py does
            # the same before its first read).
            if hasattr(ch, "connect"):
                await ch.connect()
        rows = await ch.query_all(
            """
            SELECT DISTINCT e.content AS content
            FROM loom.memory_belief_sources AS b FINAL
            INNER JOIN loom.memory_evidence AS e FINAL
                ON e.evidence_id = b.evidence_id
               AND e.org = b.org AND e.namespace = b.namespace
            WHERE b.org = {org:String} AND b.namespace = {ns:String}
              AND b.memory_key IN {keys:Array(String)}
              AND b.tombstoned_at IS NULL AND e.tombstoned_at IS NULL
            LIMIT {lim:UInt32}
            """,
            params={"org": org, "ns": namespace, "keys": list(memory_keys), "lim": limit},
        )
    except Exception as e:  # noqa: BLE001 — diagnostic, never fatal
        print(f"  evidence read skipped: {e}", file=sys.stderr)
        return None
    return [str(r.get("content") or "") for r in rows]


def summarize_stages(stages: list[str]) -> list[tuple[str, int, float]]:
    """(stage, count, share%) rows, most frequent first."""
    counts: dict[str, int] = {}
    for st in stages:
        counts[st] = counts.get(st, 0) + 1
    n = len(stages) or 1
    return sorted(
        ((st, c, round(c / n * 100.0, 1)) for st, c in counts.items()),
        key=lambda t: (-t[1], t[0]),
    )


async def _diagnose_one(
    client,
    base_url,
    item,
    openai_key,
    synth_model=None,
    *,
    judge_label: str = "WRONG",
    read_evidence: bool = True,
    token: str = "",
    org: str = "dev",
):
    # This model only SYNTHESIZES a diagnostic
    # answer — the judge verdict comes from the results CSV. Pinned like the
    # main bench's reader; deliberately not LOOM_LLM_MODEL (the variable
    # under test must not steer the diagnosis instrument).
    synth_model = synth_model or os.environ.get("BENCH_READER_MODEL") or "gpt-4o-mini"
    namespace = f"lme-diag-{uuid.uuid4().hex[:8]}"
    remember_bench_namespace(org, namespace)  # erased when the run exits
    identity = {
        "org": org,
        "namespace": namespace,
        "agent": "lme-diag",
        "user_id": "-",
    }
    sessions = item.get("haystack_sessions", []) or []
    dates = item.get("haystack_dates", []) or []
    session_ids = item.get("haystack_session_ids", []) or []
    key_to_sessions: dict[str, set[str]] = {}

    # Ingest per session (mirroring main bench)
    fact_count_per_session = []
    for i, session in enumerate(sessions):
        if not session:
            fact_count_per_session.append(0)
            continue
        date = _to_iso_date(dates[i]) if i < len(dates) else ""
        sid = str(session_ids[i]) if i < len(session_ids) else f"s{i}"
        # One set_from_messages per SESSION, as bench/longmemeval.py does: the
        # extractor sees the whole session, and one call per session stays
        # under the server's write rate limit (per-pair ingest 429'd).
        session_facts = 0
        body = {
            **identity,
            "session_id": sid,
            "messages": session[:400],
            "max_tokens": 4096,
        }
        if date:
            body["observation_date"] = date
        try:
            resp = await _post(client, base_url + "/v1/memory.set_from_messages", body, token)
            written = resp.get("written", []) or []
            session_facts += len(written)
            for w in written:
                key = str((w or {}).get("memory_key") or "")
                if key:
                    key_to_sessions.setdefault(key, set()).add(sid)
        except Exception as e:  # noqa: BLE001
            print(f"  ingest err: {e}", file=sys.stderr)
        fact_count_per_session.append(session_facts)

    # Search
    resp = await _post(
        client,
        base_url + "/v1/memory.search",
        {
            **identity,
            "query": item["question"],
            "top_k": 30,
            "search_mode": "bm25_hybrid",
            "alpha": 0.5,
            "rerank": "builtin:openai",
            "include_top_n_unmatched": 20,
            # Pinned as the main bench pins it: under the server default a token
            # that reads other namespaces fans the search out over them.
            "namespace_scope": "this",
        },
        token,
    )
    hits = resp.get("results", [])
    excerpts = [h.get("content_excerpt", "") for h in hits]
    hit_keys = [str(h.get("memory_key") or "") for h in hits if h.get("memory_key")]

    # Stage classification (bench/failure_taxonomy.classify_answer_failure).
    from bench.failure_taxonomy import classify_answer_failure

    gold = str(item.get("answer", ""))
    answer_sessions = {str(x) for x in (item.get("answer_session_ids") or []) if x}
    retrieved_sessions: set[str] = set()
    for key in hit_keys:
        retrieved_sessions |= key_to_sessions.get(key, set())
    recalled = (
        bool(answer_sessions & retrieved_sessions) if answer_sessions else gold_in(gold, excerpts)
    )
    gold_in_excerpt = gold_in(gold, excerpts)
    evidence_texts = (
        await linked_evidence_texts(identity["org"], namespace, hit_keys) if read_evidence else None
    )
    gold_in_evidence = gold_in(gold, evidence_texts or [])
    stage = classify_answer_failure(
        judged_correct=(judge_label == "CORRECT"),
        recalled=recalled,
        gold_in_excerpt=gold_in_excerpt,
        gold_in_evidence=gold_in_evidence,
    )
    if evidence_texts is None and stage == "not_verbatim":
        stage = "not_verbatim_or_unchecked"

    # Synthesize an answer
    synth_body = {
        "model": synth_model,
        "temperature": 0.0,
        "messages": [
            {
                "role": "system",
                "content": "Answer the question using only the retrieved memories. "
                "Prefer most recent on conflict. Concise — under 6 words. "
                "If memories don't contain enough info, say 'I don't know.'",
            },
            {
                "role": "user",
                "content": f"Question: {item['question']}\n\nMemories:\n"
                + "\n".join(f"  [{i + 1}] {e[:400]}" for i, e in enumerate(excerpts)),
            },
        ],
    }
    synth_r = await client.post(
        _OPENAI_CHAT_URL,
        headers={
            "Authorization": f"Bearer {openai_key}",
            "Content-Type": "application/json",
        },
        json=synth_body,
        timeout=30.0,
    )
    generated = synth_r.json()["choices"][0]["message"]["content"]

    return {
        "question_id": item.get("question_id", ""),
        "category": item.get("question_type", ""),
        "question": item["question"],
        "gold": item["answer"],
        "n_sessions": len(sessions),
        "facts_per_session": fact_count_per_session,
        "total_facts": sum(fact_count_per_session),
        "n_retrieved": len(excerpts),
        "top_5_excerpts": excerpts[:5],
        "generated_answer": generated.strip(),
        "stage": stage,
        "recalled": recalled,
        "gold_in_excerpt": gold_in_excerpt,
        "gold_in_evidence": gold_in_evidence,
        "evidence_turns_read": len(evidence_texts or []),
    }


async def main_async(args):
    # Read failed items from results CSV
    with open(args.csv) as f:
        reader = csv.DictReader(f)
        results_by_id = {row["question_id"]: row for row in reader}

    failed_ids = [
        qid
        for qid, row in results_by_id.items()
        if not args.only_wrong or row.get("judge_label") == "WRONG"
    ][: args.limit]

    if not failed_ids:
        print("no failed items found", file=sys.stderr)
        return 1

    # Load full dataset
    with open(args.dataset) as f:
        dataset = json.load(f)
    by_id = {item["question_id"]: item for item in dataset}

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key:
        print("OPENAI_API_KEY required for synthesis", file=sys.stderr)
        return 2

    out_lines: list[str] = ["# LongMemEval failure diagnostic\n"]
    out_lines.append(f"Re-traced {len(failed_ids)} items end-to-end.\n")
    stages: list[str] = []

    # Every re-traced item's namespace is erased when the run exits, after a failure too.
    async with (
        httpx.AsyncClient() as client,
        erase_on_exit(lambda p, b: _post(client, args.base_url + p, b, args.token), org=args.org),
    ):
        for i, qid in enumerate(failed_ids):
            item = by_id.get(qid)
            if not item:
                continue
            print(
                f"diag {i + 1}/{len(failed_ids)}: {qid} [{item.get('question_type', '')}]",
                flush=True,
            )
            try:
                trace = await _diagnose_one(
                    client,
                    args.base_url,
                    item,
                    openai_key,
                    judge_label=str(results_by_id[qid].get("judge_label") or "WRONG"),
                    read_evidence=not args.no_evidence,
                    token=args.token,
                    org=args.org,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  err: {e}", file=sys.stderr)
                continue
            stages.append(trace["stage"])
            out_lines.append(f"\n## {trace['question_id']} [{trace['category']}]\n")
            out_lines.append(
                f"**Stage:** `{trace['stage']}` (recalled={trace['recalled']}, "
                f"gold_in_excerpt={trace['gold_in_excerpt']}, "
                f"gold_in_evidence={trace['gold_in_evidence']}, "
                f"evidence_turns_read={trace['evidence_turns_read']})\n"
            )
            out_lines.append(f"**Question:** {trace['question']}\n")
            out_lines.append(f"**Gold:** {trace['gold']!r}\n")
            out_lines.append(f"**Generated:** {trace['generated_answer']!r}\n")
            out_lines.append(
                f"**Pipeline:** {trace['n_sessions']} sessions → "
                f"{trace['total_facts']} facts extracted ({trace['facts_per_session']}) → "
                f"{trace['n_retrieved']} retrieved\n",
            )
            out_lines.append("**Top-5 retrieved:**\n")
            for j, ex in enumerate(trace["top_5_excerpts"]):
                out_lines.append(f"  {j + 1}. {ex[:250]}\n")

    # Stage histogram first: the retrieval-vs-compression-vs-reader split.
    hist = summarize_stages(stages)
    header = ["\n## Failure stages\n", "| stage | items | share |", "|---|---|---|"]
    header += [f"| {st} | {c} | {pct}% |" for st, c, pct in hist]
    out_lines[2:2] = header
    Path(args.out).write_text("\n".join(out_lines))
    for st, c, pct in hist:
        print(f"  {st:<28} {c:>4}  {pct:>5}%")
    print(f"wrote {args.out}")
    if len(stages) < len(failed_ids):
        # An item that errored or is missing from the dataset leaves the histogram incomplete.
        print(f"{len(failed_ids) - len(stages)} item(s) not diagnosed; see above", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--dataset", default="bench/datasets/longmemeval_oracle.json")
    parser.add_argument("--base-url", default="http://localhost:7777")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--only-wrong", action="store_true")
    parser.add_argument("--out", default="bench/lme_diagnosis.md")
    parser.add_argument(
        "--org",
        default="dev",
        help="Identity org for the throwaway namespaces (default 'dev', the "
        "main bench's; the token must cover it).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("LOOM_API_KEY", ""),
        help="Bearer JWT for the loom server (default: $LOOM_API_KEY). The main "
        "bench takes the same flag; without it every call 401s on a server "
        "with LOOM_AUTH_REQUIRED=true.",
    )
    parser.add_argument(
        "--no-evidence",
        action="store_true",
        help="Skip the ClickHouse read of the raw turns behind retrieved "
        "memories (compression_loss cannot be told from not_verbatim).",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
