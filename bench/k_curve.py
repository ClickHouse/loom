"""How many memories should an agent read? Gold-evidence recall and context
size by top_k, LLM-free: a small K loses evidence, a large one adds context
without adding recall, so context size is reported beside recall.

For each LoCoMo multi-hop and temporal question (the two categories where
the evidence spans sessions), run `memory.search` once PER k — not one wide
search truncated, because dedup, reranking and the fusion window depend on
top_k — and score the hits' `session_id`s against the question's cited
evidence sessions (`locomo_qa.evidence_sessions_cited`, session granularity):

  gold-recall@k   share of the gold evidence sessions present in the top k
  all-gold@k      share of questions whose EVERY gold session is in the top k
                  (the multi-hop reader needs the whole set, not one of it)
  tokens@k        median context handed to a reader at k, from the excerpts
                  (~4 chars/token; `context_tokens_est` when the server sends it)
  inst-recall@k   share of the gold ANSWER's instances (a set answer split on
                  commas / "and": "clarinet and violin") whose words appear in
                  some hit's text in the top k — LLM-free, on questions with
                  two or more instances (`n_set`); all-inst@k = every one did.
                  Tells retrieval misses from reader misses on set questions.

No LLM: the number is what retrieval puts in front of the reader, so the curve is stable
across reader models and cheap to rerun after a ranking change. `--reader ask` runs the
same scoring over `memory.ask` with `allow_synthesis: false`: the hits are the ask
path's own `results` — the widened pool after recency and injection — so gold-recall@k
there is gold-in-window@k for the synthesizer, still with no model call. `--ask-json`
merges fields into every ask body (an arm), e.g. `{"deep_first": false}`; `--agent`
names the identity the namespaces were ingested under (`kcurve`), which the ask path's
private scope filters by. Ingests its own namespaces
(prefix `kcurve`) so it does not depend on another run's lifecycle; `--reuse` skips
ingest.

Usage:
  LOOM_API_KEY=... python bench/k_curve.py --dataset bench/datasets/locomo10.json \\
      --base-url http://localhost:7777 --org <org> --limit-conversations 5 \\
      --ks 3,6,10,15,30 --out bench/k_curve.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from bench.evidence_alignment import _post, ingest_conv
from bench.locomo_qa import (
    CATEGORY_NAMES,
    evidence_sessions_cited,
    hit_session_numbers,
)

CATEGORIES_DEFAULT = (1, 2)  # multi_hop, temporal


@dataclass
class QuestionAtK:
    category: str
    k: int
    gold_sessions: int
    gold_recall: float
    all_gold: bool
    tokens: int
    hits: int
    latency_ms: int = 0
    instances: int = 0  # gold answer instances (0 = not a set answer)
    instances_found: int = 0


@dataclass
class KPoint:
    category: str
    k: int
    n: int
    gold_recall_mean: float
    all_gold_share: float
    tokens_median: int
    tokens_p90: int
    hits_mean: float
    latency_p50_ms: int = 0
    latency_p90_ms: int = 0
    n_set: int = 0
    inst_recall_mean: float = 0.0
    all_inst_share: float = 0.0
    rows: list[QuestionAtK] = field(default_factory=list, repr=False)


_COUNT_WORDS = {
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
}
_STOP = {
    "the",
    "and",
    "with",
    "from",
    "for",
    "his",
    "her",
    "their",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "at",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def gold_instances(answer: Any) -> list[str]:
    """The instances of a set answer: split on commas, semicolons, quotes and
    " and "; counts ("2", "two") and fragments under three characters are
    not instances. A single-valued answer yields one instance; callers treat
    fewer than two as "not a set question"."""
    g = str(answer or "")
    for sep in ('"', "'", ";", " and "):
        g = g.replace(sep, ",")
    out: list[str] = []
    for part in g.replace(".", " ").split(","):
        part = " ".join(_norm(part).split())
        if len(part) < 3 or re.fullmatch(r"\d+", part) or part in _COUNT_WORDS:
            continue
        out.append(part)
    return out


def instance_in(instance: str, text: str) -> bool:
    """An instance is in a text when it appears verbatim or when each of its
    content words has a prefix match (five characters, so "mountains" meets
    "mountain") — a lexical test, deliberately looser than exact and tighter
    than any one word."""
    t = _norm(text)
    if instance in t:
        return True
    toks = [w for w in instance.split() if len(w) >= 3 and w not in _STOP]
    if not toks:
        return False
    words = t.split()

    def _hit(tok: str) -> bool:
        pre = tok[: max(4, len(tok) - 2)] if len(tok) > 4 else tok
        return any(w.startswith(pre) for w in words)

    return all(_hit(tok) for tok in toks)


def instances_found(instances: list[str], hits: list[dict[str, Any]]) -> int:
    texts = [str(h.get("content_excerpt") or "") for h in hits]
    return sum(1 for i in instances if any(instance_in(i, t) for t in texts))


def tokens_of(hits: list[dict[str, Any]], resp: dict[str, Any]) -> int:
    """Context the reader would get: the server's estimate when present, else
    the excerpts at ~4 chars/token (the board's convention)."""
    est = resp.get("context_tokens_est")
    if isinstance(est, int | float) and est > 0:
        return int(est)
    return sum(len(str(h.get("content_excerpt") or "")) for h in hits) // 4


def score(
    hits: list[dict[str, Any]],
    namespace: str,
    gold: set[int],
    k: int,
    category: str,
    resp: dict[str, Any] | None = None,
    answer: Any = None,
) -> QuestionAtK:
    sess = hit_session_numbers(hits, namespace)[:k]
    seen = set(sess)
    covered = len(gold & seen)
    inst = gold_instances(answer) if answer is not None else []
    if len(inst) < 2:
        inst = []
    return QuestionAtK(
        category=category,
        k=k,
        gold_sessions=len(gold),
        gold_recall=covered / len(gold) if gold else 0.0,
        all_gold=bool(gold) and covered == len(gold),
        tokens=tokens_of(hits[:k], resp or {}),
        hits=len(hits[:k]),
        instances=len(inst),
        instances_found=instances_found(inst, hits[:k]) if inst else 0,
    )


def request_body(a: Any, namespace: str, question: str, k: int) -> dict[str, Any]:
    """The request for one (question, k): a `memory.search` body, or under
    `--reader ask` a retrieval-only `memory.ask` body (`allow_synthesis`
    false, the ingest identity as `agent`) with `--ask-json` merged last so an
    arm can override any field."""
    body: dict[str, Any] = {
        "org": a.org,
        "namespace": namespace,
        "query": question,
        "top_k": k,
        "namespace_scope": "this",
    }
    if getattr(a, "reader", "search") == "ask":
        body["allow_synthesis"] = False
        if getattr(a, "agent", ""):
            body["agent"] = a.agent
        body.update(getattr(a, "ask_json", None) or {})
    else:
        body.update(getattr(a, "search_json", None) or {})
    return body


def _p(xs: list[int], q: float) -> int:
    """Nearest-rank percentile: the value at ceil(q * n), 1-indexed."""
    if not xs:
        return 0
    s = sorted(xs)
    idx = math.ceil(q * len(s)) - 1
    return int(s[min(len(s) - 1, max(0, idx))])


def summarise(rows: list[QuestionAtK]) -> list[KPoint]:
    out: list[KPoint] = []
    keys = sorted({(r.category, r.k) for r in rows}, key=lambda t: (t[0], t[1]))
    for cat, k in keys:
        rs = [r for r in rows if r.category == cat and r.k == k]
        toks = [r.tokens for r in rs]
        lats = [r.latency_ms for r in rs if r.latency_ms > 0]
        sets = [r for r in rs if r.instances >= 2]
        out.append(
            KPoint(
                category=cat,
                k=k,
                n=len(rs),
                gold_recall_mean=round(statistics.fmean(r.gold_recall for r in rs), 3),
                all_gold_share=round(sum(1 for r in rs if r.all_gold) / len(rs), 3),
                tokens_median=int(statistics.median(toks)) if toks else 0,
                tokens_p90=_p(toks, 0.9),
                hits_mean=round(statistics.fmean(r.hits for r in rs), 1),
                latency_p50_ms=int(statistics.median(lats)) if lats else 0,
                latency_p90_ms=_p(lats, 0.9),
                n_set=len(sets),
                inst_recall_mean=(
                    round(statistics.fmean(r.instances_found / r.instances for r in sets), 3)
                    if sets
                    else 0.0
                ),
                all_inst_share=(
                    round(sum(1 for r in sets if r.instances_found == r.instances) / len(sets), 3)
                    if sets
                    else 0.0
                ),
                rows=rs,
            )
        )
    return out


def render(points: list[KPoint]) -> str:
    lines = [
        "| category | k | n | gold-recall@k | all-gold@k | tokens median | tokens p90 | hits | "
        "latency p50 | p90 | n_set | inst-recall@k | all-inst@k |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for p in points:
        lines.append(
            f"| {p.category} | {p.k} | {p.n} | {p.gold_recall_mean:.3f} | {p.all_gold_share:.3f} | "
            f"{p.tokens_median} | {p.tokens_p90} | {p.hits_mean} | "
            f"{p.latency_p50_ms} | {p.latency_p90_ms} | "
            f"{p.n_set} | {p.inst_recall_mean:.3f} | {p.all_inst_share:.3f} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Write path: report the write-path cost before attributing a gain to the architecture.
# One `cost.summary` call per namespace of the run; a failure prints a note and never
# fails the bench.
# ---------------------------------------------------------------------------


@dataclass
class WritePathRow:
    namespace: str
    ops: int
    tokens: int
    embed_tokens: int
    cost_usd: float


async def write_path_cost(
    cl: Any, base_url: str, tok: str, org: str, namespaces: list[str], *, days: int = 1
) -> list[WritePathRow]:
    """`memory.set_from_messages` spend per namespace over the last `days`
    days (UTC), read through `cost.summary` with the bench's own token — the
    same ledger the acceptance records quote. Fail-open: an error yields []."""
    from datetime import UTC, datetime, timedelta

    today = datetime.now(UTC).date()
    out: list[WritePathRow] = []
    for ns in namespaces:
        body = {
            "org": org,
            "namespace": ns,
            "from_date": (today - timedelta(days=days)).isoformat(),
            "to_date": today.isoformat(),
            "group_by": ["namespace"],
            "operation": "memory.set_from_messages",
            "include_cache": False,
        }
        try:
            resp = await _post(cl, base_url + "/v1/cost.summary", body, tok)
        except Exception as e:  # noqa: BLE001 — a readout, not the bench
            print(f"  write-path cost unavailable for {ns}: {e}", file=sys.stderr)
            continue
        if "_err" in resp:
            print(f"  write-path cost unavailable for {ns}: {resp['_err']}", file=sys.stderr)
            continue
        rows = list(resp.get("rows") or [])
        if not rows:
            out.append(WritePathRow(ns, 0, 0, 0, 0.0))
            continue
        r = rows[0]
        out.append(
            WritePathRow(
                ns,
                int(r.get("ops") or 0),
                int(r.get("tokens") or 0),
                int(r.get("embed_tokens") or 0),
                float(r.get("cost_usd") or 0.0),
            )
        )
    return out


def render_write_path(rows: list[WritePathRow], *, days: int = 1) -> str:
    """The write side of the arm: `set_from_messages` calls, LLM tokens,
    embedding tokens and dollars per namespace, then the total — so a recall
    delta is never read without the ingest it cost."""
    if not rows:
        return ""
    lines = [
        "",
        f"write path (memory.set_from_messages, last {days} day(s), from cost.summary):",
        "| namespace | ingests | llm tokens | embed tokens | usd |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r.namespace} | {r.ops} | {r.tokens} | {r.embed_tokens} | {r.cost_usd:.3f} |"
        )
    lines.append(
        f"| total | {sum(r.ops for r in rows)} | {sum(r.tokens for r in rows)} | "
        f"{sum(r.embed_tokens for r in rows)} | {sum(r.cost_usd for r in rows):.3f} |"
    )
    return "\n".join(lines)


async def run(a: argparse.Namespace) -> list[KPoint]:
    tok = a.token
    data = json.load(open(a.dataset))[: a.limit_conversations]
    ks = [int(x) for x in a.ks.split(",") if x.strip()]
    cats = {int(x) for x in a.categories.split(",") if x.strip()}
    sem = asyncio.Semaphore(a.concurrency)
    ns_of = {i: f"{a.namespace_prefix}-{i}" for i in range(len(data))}
    rows: list[QuestionAtK] = []

    async with httpx.AsyncClient() as cl:
        if not a.reuse:
            for i, conv in enumerate(data):
                await ingest_conv(
                    cl, a.base_url, tok, sem, ns_of[i], conv, org=a.org, agent="kcurve"
                )
                print(f"  ingested conv {i} -> {ns_of[i]}", file=sys.stderr, flush=True)
            if a.settle_seconds:
                await asyncio.sleep(a.settle_seconds)

        async def one(q: dict[str, Any], ns: str, k: int) -> None:
            gold = evidence_sessions_cited(q)
            if not gold:
                return
            body = request_body(a, ns, str(q.get("question") or ""), k)
            path = "/v1/memory.ask" if a.reader == "ask" else "/v1/memory.search"
            async with sem:
                t0 = time.monotonic()
                resp = await _post(cl, a.base_url + path, body, tok)
                elapsed_ms = int((time.monotonic() - t0) * 1000)
            hits = list(resp.get("results") or [])
            if "_err" in resp:
                return
            row = score(
                hits,
                ns,
                gold,
                k,
                CATEGORY_NAMES.get(int(q.get("category", 0)), "other"),
                resp,
                answer=q.get("answer"),
            )
            row.latency_ms = elapsed_ms
            rows.append(row)

        tasks = [
            one(q, ns_of[i], k)
            for i, conv in enumerate(data)
            for q in conv.get("qa", [])
            if int(q.get("category", 0)) in cats
            for k in ks
        ]
        await asyncio.gather(*tasks)
        a.write_path_rows = (
            []
            if getattr(a, "no_cost", False)
            else await write_path_cost(cl, a.base_url, tok, a.org, list(ns_of.values()))
        )
    return summarise(rows)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--dataset", default="bench/datasets/locomo10.json")
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--org", default="dev")
    p.add_argument("--namespace-prefix", default="kcurve")
    p.add_argument("--limit-conversations", type=int, default=5)
    p.add_argument(
        "--categories",
        default=",".join(str(c) for c in CATEGORIES_DEFAULT),
        help="LoCoMo category ids (1 multi_hop, 2 temporal, 3 open_domain, 4 single_hop)",
    )
    p.add_argument("--ks", default="3,6,10,15,30")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--settle-seconds", type=int, default=20, help="wait after ingest for embeddings"
    )
    p.add_argument("--reuse", action="store_true", help="skip ingest; reuse existing namespaces")
    p.add_argument(
        "--search-json", type=json.loads, default=None, help="JSON merged into every search body"
    )
    p.add_argument(
        "--reader",
        choices=["search", "ask"],
        default="search",
        help="search = memory.search hits (default); ask = retrieval-only memory.ask, "
        "whose results are the synthesis pool's order (gold-in-window@k)",
    )
    p.add_argument(
        "--ask-json", type=json.loads, default=None, help="JSON merged into every ask body"
    )
    p.add_argument(
        "--agent",
        default="kcurve",
        help="identity for --reader ask (the ingest agent; the ask path filters by it)",
    )
    p.add_argument("--out", default="")
    p.add_argument(
        "--no-cost",
        action="store_true",
        help="skip the write-path readout (one cost.summary call per namespace)",
    )
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if not a.token:
        print("need --token or LOOM_API_KEY", file=sys.stderr)
        return 2
    points = asyncio.run(run(a))
    if a.out:
        with open(a.out, "w") as f:
            json.dump(
                [{**{k: v for k, v in asdict(pt).items() if k != "rows"}} for pt in points],
                f,
                indent=2,
            )
    cost_rows = list(getattr(a, "write_path_rows", []) or [])
    if a.out and cost_rows:
        with open(a.out + ".cost.json", "w") as f:
            json.dump([asdict(r) for r in cost_rows], f, indent=2)
    print(
        json.dumps(
            [{k: v for k, v in asdict(pt).items() if k != "rows"} for pt in points], indent=2
        )
        if a.json
        else render(points) + render_write_path(cost_rows)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
