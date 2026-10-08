"""A labelled relevance set from what agents actually relied on.

Every ranking lever in loom has sat default-off for the same reason: the A/B
that judged it had 12, 24 or 60 items, and twelve items cannot separate an
MRR of 0.713 from 0.709. Loom already writes the labels a bigger set needs:

  * `loom.search_trace_log` — the query (when the namespace consented to
    capture), the keys returned and the order they came back in;
  * `loom.memory_usage_events` with `event_type IN ('used', 'used_cited')` (an
    agent's label, or memory.ask's cited-key credit) — the memories an
    answer relied on. `memory.ask` credits every memory it cited; an agent's
    `run.complete` credits `memories_used`; `memory.outcome` credits by hand.

Joined, a trace whose returned keys were later relied on inside a short
window is a (query, gold_keys) pair labelled by use, not by a judge. This
script writes those pairs in the shape `bench/retrieval_recall.py` consumes
(one dataset per namespace), with the instrument recorded alongside,
so every rank term can be swept against thousands of
pairs with a bootstrap interval before a default moves.

    python bench/relied_on_set.py --org <org> --days 7 \\
        --out-dir bench/datasets/relied_on

The join is by time, not by id: `search_trace_log.run_id` is usually empty and
`memory_usage_events` has no trace id, while `memory.ask` credits its citations seconds
after its own search. `--window-seconds` (120) bounds the pairing; `--min-relied` (1) is
how many returned keys must have been relied on for a trace to become a question.

Only namespaces that consented to capture (`capture_generation_io`) carry
query text; the rest log a blank query and are skipped (usually most of
them — turn it on for bench namespaces). Reads ClickHouse
directly through `server.core.ch` (the server's own `.env`), read-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@dataclass
class Pair:
    query: str
    returned_keys: list[str]
    relied_keys: list[str]
    ts: datetime
    agent: str = ""
    shape: str = ""  # search_trace_log.shape_sha256 of the labelling retrieval
    # rank (1-based) at which each relied key was SHOWN by the labelling
    # retrieval. A label is a click on a ranked list: it carries the list's
    # position bias, and the sweep weights it by 1/propensity(rank) to undo
    # that.
    shown_ranks: dict[str, int] = field(default_factory=dict)


@dataclass
class NamespaceSet:
    org: str
    namespace: str
    pairs: list[Pair] = field(default_factory=list)


def pair_traces(
    traces: list[dict[str, Any]],
    used: list[dict[str, Any]],
    *,
    window_seconds: int = 120,
    min_relied: int = 1,
) -> list[Pair]:
    """Pure core. `traces`: rows with ts, query, returned_keys, agent.
    `used`: rows with ts, memory_key (already resolved from memory_id).
    A trace pairs with every `used` key whose event lands in
    [trace.ts, trace.ts + window]; the trace becomes a pair when at least
    `min_relied` of its returned keys were relied on. Keys relied on but not
    returned are dropped — they label a different retrieval, not this one.
    A blank query never becomes a pair. Later traces take precedence for the
    same used event only in the sense that both may claim it; a used event
    is evidence for every retrieval that could have produced it."""
    used_sorted = sorted(
        ((u["ts"], str(u["memory_key"])) for u in used if u.get("memory_key")),
        key=lambda x: x[0],
    )
    out: list[Pair] = []
    win = timedelta(seconds=max(0, int(window_seconds)))
    import bisect

    ts_index = [t for t, _ in used_sorted]
    for t in traces:
        q = str(t.get("query") or "").strip()
        if not q:
            continue
        returned = [str(k) for k in (t.get("returned_keys") or []) if k]
        if not returned:
            continue
        ts = t["ts"]
        lo = bisect.bisect_left(ts_index, ts)
        hi = bisect.bisect_right(ts_index, ts + win)
        relied_set = {k for _, k in used_sorted[lo:hi]}
        relied = [k for k in returned if k in relied_set]  # keep returned order
        if len(relied) < max(1, int(min_relied)):
            continue
        out.append(
            Pair(
                query=q,
                returned_keys=returned,
                relied_keys=relied,
                ts=ts,
                agent=str(t.get("agent") or ""),
                shape=str(t.get("shape") or ""),
                shown_ranks={k: returned.index(k) + 1 for k in relied},
            )
        )
    return out


def dedupe_pairs(pairs: list[Pair]) -> list[Pair]:
    """One question per distinct query text: the union of relied keys across
    its traces (the same question asked twice with different windows is one
    question with more gold), keeping the earliest ts."""
    by_q: dict[str, Pair] = {}
    for p in sorted(pairs, key=lambda p: p.ts):
        cur = by_q.get(p.query)
        if cur is None:
            by_q[p.query] = Pair(
                p.query,
                list(p.returned_keys),
                list(p.relied_keys),
                p.ts,
                p.agent,
                p.shape,
                dict(p.shown_ranks),
            )
        else:
            for k in p.relied_keys:
                if k not in cur.relied_keys:
                    cur.relied_keys.append(k)
                r = p.shown_ranks.get(k)
                if r and (k not in cur.shown_ranks or r < cur.shown_ranks[k]):
                    cur.shown_ranks[k] = r  # the best position it was ever shown at
    return list(by_q.values())


def propensity_by_rank(pairs: list[Pair]) -> dict[str, float]:
    """Empirical examination propensity per shown rank: of everything the
    labelling retrievals showed at rank r, the share that was relied on. Pure
    counting over the UN-deduplicated pairs (every labelled trace counts once);
    ranks never shown are absent. A typical reading falls from ~0.6 at rank 1
    to ~0.3 at rank 15 — the click-model curve."""
    shown: dict[int, int] = {}
    relied: dict[int, int] = {}
    for p in pairs:
        rk = set(p.relied_keys)
        for i, k in enumerate(p.returned_keys, start=1):
            shown[i] = shown.get(i, 0) + 1
            relied[i] = relied.get(i, 0) + (1 if k in rk else 0)
    return {str(r): round(relied[r] / shown[r], 4) for r in sorted(shown) if shown[r]}


def to_dataset(
    ns_set: NamespaceSet,
    *,
    instrument: dict[str, Any],
    window_seconds: int,
    days: int,
    propensity: dict[str, float] | None = None,
) -> dict[str, Any]:
    """The `retrieval_recall.py` dataset shape, plus provenance fields."""
    qs = [
        {
            "query": p.query,
            "gold_keys": p.relied_keys,
            "note": f"relied-on {len(p.relied_keys)} of {len(p.returned_keys)} returned; "
            f"agent={p.agent or '-'}; ts={p.ts.isoformat()}",
            # The retrieval shape (search_mode, recipe, scope…) that produced the
            # label: gold is biased toward it, so a sweep over modes reads this.
            "shape": p.shape,
            # 1-based rank each gold key was shown at by the labelling retrieval.
            "gold_shown_ranks": {k: p.shown_ranks[k] for k in p.relied_keys if k in p.shown_ranks},
        }
        for p in ns_set.pairs
    ]
    return {
        "_about": "Relevance set labelled by USE, not by a judge: each gold key was "
        "returned by memory.search and then relied on (a `used` usage event "
        "within the pairing window). Built by bench/relied_on_set.py.",
        "_caveat": "Gold is what an answer cited or an agent reported relying on — "
        "a lower bound on relevance (a relevant key that was never cited is "
        "not gold) and biased toward what the current ranking surfaced. Use it "
        "for ORDERING deltas between arms on the same set, not as an absolute.",
        "_built": {
            "at": datetime.now(UTC).isoformat(),
            "days": days,
            "window_seconds": window_seconds,
            **instrument,
        },
        "org": ns_set.org,
        "namespace": ns_set.namespace,
        # How many distinct retrieval shapes labelled this set. One shape means
        # every gold key came from the same search_mode/recipe: a sweep over
        # modes on such a set measures agreement with that shape, not quality.
        "_label_shapes": sorted({p.shape for p in ns_set.pairs if p.shape}),
        # Examination propensity per shown rank over every labelled trace of
        # this namespace. The sweep weights a gold key by
        # 1/propensity(rank shown) so a key cited from rank 14 counts more than
        # one cited from rank 1 — the labelling list's position bias, undone.
        "_propensity_by_rank": propensity or {},
        "questions": qs,
    }


async def _fetch(
    org: str, namespaces: list[str] | None, days: int, ch: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    ns_clause = " AND namespace IN {nss:Array(String)}" if namespaces else ""
    e_ns_clause = " AND e.namespace IN {nss:Array(String)}" if namespaces else ""
    params: dict[str, Any] = {"org": org, "days": int(days)}
    if namespaces:
        params["nss"] = namespaces
    traces = await ch.query_all(
        f"""
        SELECT org, namespace, agent, query, returned_keys, ts, embedding_model,
               toString(shape_sha256) AS shape
        FROM loom.search_trace_log
        WHERE org = {{org:String}} AND ts > now() - INTERVAL {{days:UInt32}} DAY
          AND query != '' AND result_count > 0{ns_clause}
        ORDER BY ts
        """,
        params=params,
    )
    # `used` events carry memory_id; resolve to the key on the memory table.
    # `AS mid`, never `AS memory_id`.
    used = await ch.query_all(
        f"""
        SELECT e.namespace AS namespace, e.ts AS ts, m.memory_key AS memory_key
        FROM loom.memory_usage_events AS e
        INNER JOIN (
            SELECT toString(memory_id) AS mid, org, namespace, memory_key
            FROM loom.memory FINAL WHERE org = {{org:String}}
        ) AS m ON m.mid = toString(e.memory_id) AND m.org = e.org AND m.namespace = e.namespace
        WHERE e.org = {{org:String}} AND e.event_type IN ('used', 'used_cited')
          AND e.ts > now() - INTERVAL {{days:UInt32}} DAY{e_ns_clause}
        """,
        params=params,
    )
    health = await ch.query_one("SELECT version() AS ch_version")
    instrument = {
        "ch_version": str((health or {}).get("ch_version") or ""),
        "embedding_models": sorted({str(t.get("embedding_model") or "") for t in traces} - {""}),
    }
    return [dict(t) for t in traces], [dict(u) for u in used], instrument


async def main_async(args: argparse.Namespace) -> int:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    namespaces = [n for n in (args.namespace or "").split(",") if n.strip()] or None
    traces, used, instrument = await _fetch(args.org, namespaces, args.days, ch)
    by_ns_t: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_ns_u: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in traces:
        by_ns_t[str(t["namespace"])].append(t)
    for u in used:
        by_ns_u[str(u["namespace"])].append(u)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"relied-on set: org={args.org} days={args.days} window={args.window_seconds}s "
        f"traces_with_query={len(traces)} used_events={len(used)} namespaces={len(by_ns_t)}"
    )
    total = 0
    for ns, ts_rows in sorted(by_ns_t.items(), key=lambda kv: -len(kv[1])):
        raw_pairs = pair_traces(
            ts_rows,
            by_ns_u.get(ns, []),
            window_seconds=args.window_seconds,
            min_relied=args.min_relied,
        )
        pairs = dedupe_pairs(raw_pairs)
        if len(pairs) < args.min_questions:
            print(
                f"  {ns:<40} traces={len(ts_rows):>6} questions={len(pairs):>5}  "
                "(below --min-questions, skipped)"
            )
            continue
        ds = to_dataset(
            NamespaceSet(args.org, ns, pairs),
            instrument=instrument,
            window_seconds=args.window_seconds,
            days=args.days,
            propensity=propensity_by_rank(raw_pairs),
        )
        path = out_dir / f"{args.org}__{ns}.json"
        path.write_text(json.dumps(ds, indent=1, default=str))
        total += len(pairs)
        avg_gold = sum(len(p.relied_keys) for p in pairs) / len(pairs)
        print(
            f"  {ns:<40} traces={len(ts_rows):>6} questions={len(pairs):>5} "
            f"avg_gold={avg_gold:.2f}  -> {path.name}"
        )
    print(f"wrote {total} questions under {out_dir}")
    return 0 if total else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--org", required=True)
    p.add_argument(
        "--namespace",
        default="",
        help="Comma-separated; default every consented namespace in the org.",
    )
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--window-seconds", type=int, default=120)
    p.add_argument("--min-relied", type=int, default=1)
    p.add_argument(
        "--min-questions",
        type=int,
        default=20,
        help="Skip namespaces with fewer questions than this (too small to sweep).",
    )
    p.add_argument("--out-dir", default="bench/datasets/relied_on")
    args = p.parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
