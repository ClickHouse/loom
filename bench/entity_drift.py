"""Entity drift: how far an entity's summary moved between consecutive
versions, per namespace.

The distance between an entity's semantic embedding in consecutive sessions
is a known signal of false-memory build-up. Loom does not train, but it keeps every version of
`loom.entity_summaries` with its embedding, so the same quantity is a
measurement: cosine distance between consecutive versions of one entity's
summary. A spike after an ingest is the moment supersession or the conflict
judge should look at that entity; a namespace whose p90 drift climbs week
over week is accumulating contradictions. The `divergent_entities` gauge
only counts; this puts a number and a name on each move.

Run:
    python bench/entity_drift.py --org <org> [--namespace-prefix <prefix>] --top 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from dataclasses import asdict, dataclass
from typing import Any


def cosine_distance(a: list[float], b: list[float]) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return None
    return round(1.0 - dot / (na * nb), 6)


@dataclass
class Drift:
    namespace: str
    entity: str
    versions: int
    max_step: float
    last_step: float
    total_path: float
    fact_count_last: int


def drift_series(rows: list[dict[str, Any]]) -> list[Drift]:
    """rows: entity_summaries versions with namespace, entity, version,
    summary_embedding, fact_count. Groups by (namespace, entity), orders by
    version, and measures each consecutive step."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((str(r["namespace"]), str(r["entity"])), []).append(r)
    out: list[Drift] = []
    for (ns, ent), vs in groups.items():
        vs.sort(key=lambda r: int(r.get("version") or 0))
        steps: list[float] = []
        for prev, cur in zip(vs, vs[1:], strict=False):
            d = cosine_distance(
                list(prev.get("summary_embedding") or []), list(cur.get("summary_embedding") or [])
            )
            if d is not None:
                steps.append(d)
        if not steps:
            continue
        out.append(
            Drift(
                namespace=ns,
                entity=ent,
                versions=len(vs),
                max_step=max(steps),
                last_step=steps[-1],
                total_path=round(sum(steps), 6),
                fact_count_last=int(vs[-1].get("fact_count") or 0),
            )
        )
    out.sort(key=lambda d: -d.max_step)
    return out


def summarise(drifts: list[Drift]) -> dict[str, Any]:
    steps = sorted(d.last_step for d in drifts)
    n = len(steps)
    q = lambda p: steps[min(n - 1, int(p * n))] if n else None  # noqa: E731
    return {
        "entities_with_history": n,
        "last_step_p50": q(0.5),
        "last_step_p90": q(0.9),
        "max_step_over_0_1": sum(1 for d in drifts if d.max_step > 0.1),
    }


def render(drifts: list[Drift], top: int) -> str:
    s = summarise(drifts)
    lines = [
        f"entity drift: {s['entities_with_history']} entities with more than one summary version; "
        f"last-step cosine distance p50 {s['last_step_p50']} p90 {s['last_step_p90']}; "
        f"{s['max_step_over_0_1']} moved more than 0.1 in one step",
        "",
        "| namespace | entity | versions | max step | last step | path | facts |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in drifts[:top]:
        lines.append(
            f"| {d.namespace} | {d.entity} | {d.versions} | {d.max_step:.3f} | "
            f"{d.last_step:.3f} | {d.total_path:.3f} | {d.fact_count_last} |"
        )
    return "\n".join(lines)


async def fetch(org: str, namespace_prefix: str, days: int, ch: Any) -> list[dict[str, Any]]:
    where = ["org = {org:String}"]
    params: dict[str, Any] = {"org": org}
    if namespace_prefix:
        where.append("startsWith(namespace, {pfx:String})")
        params["pfx"] = namespace_prefix
    if days:
        where.append("updated_at > now() - INTERVAL {days:UInt32} DAY")
        params["days"] = int(days)
    # All versions (no FINAL): the history is the point.
    return [
        dict(r)
        for r in await ch.query_all(
            f"""
        SELECT namespace, entity, version, summary_embedding, fact_count
        FROM loom.entity_summaries
        WHERE {" AND ".join(where)}
          AND (namespace, entity) IN (
              SELECT namespace, entity FROM loom.entity_summaries
              WHERE {" AND ".join(where)} GROUP BY namespace, entity HAVING count() > 1)
        ORDER BY namespace, entity, version
        """,
            params=params,
        )
    ]


async def main_async(a: argparse.Namespace) -> int:
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    try:
        drifts = drift_series(await fetch(a.org, a.namespace_prefix, a.days, ch))
    finally:
        await ch.disconnect()
    print(
        json.dumps(
            {"summary": summarise(drifts), "top": [asdict(d) for d in drifts[: a.top]]}, indent=2
        )
        if a.json
        else render(drifts, a.top)
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--org", required=True)
    p.add_argument("--namespace-prefix", default="")
    p.add_argument("--days", type=int, default=0, help="0 = all history")
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--json", action="store_true")
    return asyncio.run(main_async(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
