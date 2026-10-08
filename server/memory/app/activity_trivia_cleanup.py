"""Demote the activity narration already stored.

The extraction filter (`activity_facts`) stops new rows; the rows written
before it stay and keep ranking in recall. This finds a namespace's live
extracted semantic rows that narrate tool activity and, through `memory.apply`
(the previewed wide write, which mirrors every column and honours locks), demotes
or labels them. Dry run by default. Never deletes. A key an agent has read BY
NAME in the window (memory.get / update / history on the ledger) is left alone:
retiring a key a live agent reads breaks that agent.

A second classifier, `fact_quality` (`find_junk`), covers every
live non-episodic row whose `fact_quality.junk_class` names a class, plus a
later row that restates an earlier one word for word (its canonical rides the
candidate), labelled `junk:<class>` / `junk:restatement` beside
`fact_quality_junk`."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from server.memory.api.models_apply import ApplyDemote, ApplyLabel, ApplyTarget, MemoryApplyInput
from server.memory.domain.activity_facts import is_activity_narration

LABEL = "activity_narration"
_READ_BY_NAME_OPS = ("memory.get", "memory.update", "memory.history", "memory.get_by_id")

CANDIDATES_SQL = """
SELECT memory_key, value, agent, user_id, session_id
FROM loom.memory FINAL
WHERE org = {org:String} AND namespace = {ns:String}
  AND memory_type = 'semantic' AND source_class = 'extracted'
  AND tombstoned_at IS NULL AND NOT has(tags, {label:String})
LIMIT {lim:UInt32}
"""

READ_BY_NAME_SQL = """
SELECT DISTINCT memory_key
FROM loom.agent_events
WHERE org = {org:String} AND namespace = {ns:String}
  AND operation IN {ops:Array(String)}
  AND memory_key IN {keys:Array(String)}
  AND ts > now() - INTERVAL {days:UInt32} DAY
"""


async def find(
    ch: Any, org: str, namespace: str, *, limit: int = 20000, days: int = 30
) -> dict[str, Any]:
    """{candidates: [...], protected: [...]} — rows that narrate activity, and
    those among them a reader asked for by name recently."""
    rows = await ch.query_all(
        CANDIDATES_SQL, params={"org": org, "ns": namespace, "label": LABEL, "lim": int(limit)}
    )
    hits = [r for r in rows or [] if is_activity_narration(r.get("value"))]
    read = await _read_by_name(ch, org, namespace, hits, days)
    return {
        "scanned": len(rows or []),
        "candidates": [r for r in hits if str(r["memory_key"]) not in read],
        "protected": [r for r in hits if str(r["memory_key"]) in read],
    }


JUNK_LABEL = "fact_quality_junk"

JUNK_CANDIDATES_SQL = """
SELECT memory_key, value, agent, user_id, session_id, memory_type, tags, source_trace
FROM loom.memory FINAL
WHERE org = {org:String} AND namespace = {ns:String}
  AND memory_type NOT IN ('episodic', 'profile')
  AND tombstoned_at IS NULL AND NOT has(tags, {label:String})
ORDER BY created_at, memory_key
LIMIT {lim:UInt32}
"""


def classify_junk(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The junk rows among `rows` (oldest first), each with `class` and, for a
    restatement, the `canonical` key it repeats. Pure."""
    from server.memory.domain.fact_quality import content_words, junk_class

    out: list[dict[str, Any]] = []
    kept: list[tuple[str, frozenset[str]]] = []
    for r in rows:
        cls = junk_class(
            str(r.get("memory_key") or ""),
            r.get("value"),
            tags=r.get("tags"),
            source_trace=r.get("source_trace") or "",
            memory_type=str(r.get("memory_type") or ""),
        )
        if cls:
            out.append({**r, "class": cls})
            continue
        words = content_words(str(r.get("value") or ""))
        canon = next((k for k, w in kept if words and w and (words <= w or w <= words)), None)
        if canon:
            out.append({**r, "class": "restatement", "canonical": canon})
            continue
        kept.append((str(r.get("memory_key") or ""), words))
    return out


async def find_junk(
    ch: Any, org: str, namespace: str, *, limit: int = 20000, days: int = 30
) -> dict[str, Any]:
    """`find` with the `fact_quality` classifier: {scanned, candidates, protected}."""
    rows = await ch.query_all(
        JUNK_CANDIDATES_SQL,
        params={"org": org, "ns": namespace, "label": JUNK_LABEL, "lim": int(limit)},
    )
    hits = classify_junk([dict(r) for r in rows or []])
    read = await _read_by_name(ch, org, namespace, hits, days)
    return {
        "scanned": len(rows or []),
        "candidates": [r for r in hits if str(r["memory_key"]) not in read],
        "protected": [r for r in hits if str(r["memory_key"]) in read],
    }


async def _read_by_name(
    ch: Any, org: str, namespace: str, hits: list[dict[str, Any]], days: int
) -> set[str]:
    keys = sorted({str(r["memory_key"]) for r in hits})
    if not keys:
        return set()
    got = await ch.query_all(
        READ_BY_NAME_SQL,
        params={
            "org": org,
            "ns": namespace,
            "ops": list(_READ_BY_NAME_OPS),
            "keys": keys,
            "days": int(days),
        },
    )
    return {str(r["memory_key"]) for r in got or []}


async def apply(
    org: str,
    namespace: str,
    candidates: list[dict[str, Any]],
    *,
    action: str = "demote",
    execute: bool = False,
    factor: float = 0.25,
    label: str = LABEL,
    reason: str = "activity narration is not a memory",
) -> dict[str, int]:
    """One `memory.apply` per writer identity (it resolves keys at an exact
    identity). `action` demote (confidence × factor) or label (tag only)."""
    from server.memory.app.apply_ops import handle_memory_apply

    groups: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    for r in candidates:  # a `fact_quality` candidate is labelled with its class too
        ident = (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r.get("class", "")))
        groups[ident].append(str(r["memory_key"]))
    totals = {"groups": len(groups), "matched": 0, "applied": 0, "would_apply": 0, "refused": 0}
    for (agent, user_id, session_id, cls), keys in groups.items():
        tags = [label] + ([f"junk:{cls}"] if cls else [])
        for i in range(0, len(keys), 500):
            inp = MemoryApplyInput(
                org=org,
                namespace=namespace,
                agent=agent,
                user_id=user_id,
                session_id=session_id,
                target=ApplyTarget(keys=keys[i : i + 500]),
                action="label" if action == "label" else "demote",
                label=ApplyLabel(tags=tags, mode="add") if action == "label" else None,
                demote=ApplyDemote(confidence_factor=factor) if action != "label" else None,
                reason=reason,
                dry_run=not execute,
            )
            out = await handle_memory_apply(inp)
            for k in ("matched", "applied", "would_apply", "refused"):
                totals[k] += int(getattr(out, k, 0) or 0)
    return totals


def junk_apply_kwargs(fact_quality: bool) -> dict[str, Any]:
    """`apply`'s label and reason for the classifier the CLI picked."""
    if not fact_quality:
        return {}
    return {"label": JUNK_LABEL, "reason": "junk-class row, not a fact"}


def candidate_line(r: dict[str, Any]) -> str:
    """One dry-run line: `[class of canonical] key: value…`."""
    cls = str(r.get("class") or "")
    canon = f" of {r['canonical']}" if r.get("canonical") else ""
    tag = f"[{cls}{canon}] " if cls else ""
    return f"{tag}{r['memory_key']}: {str(r['value'])[:100]}"


def summary_lines(found: dict[str, Any], fact_quality: bool) -> list[str]:
    """The dry-run header: rows scanned, candidates (by class for `fact_quality`), and
    the keys left alone because a reader asked for them by name."""
    kept = found["protected"]
    if not fact_quality:
        return [
            f"scanned {found['scanned']} live extracted rows",
            f"narration: {len(found['candidates'])} (+{len(kept)} read by name, kept)",
        ]
    by: dict[str, int] = defaultdict(int)
    for r in found["candidates"]:
        by[str(r["class"])] += 1
    return [
        f"scanned {found['scanned']} live non-episodic rows",
        f"junk: {len(found['candidates'])} — "
        + ", ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda kv: -kv[1])),
        "read by name in the window, kept: "
        + (", ".join(sorted({str(r["memory_key"]) for r in kept})) or "-"),
    ]
