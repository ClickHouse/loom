"""Sensitivity backfill and sweep (the tier is `memory.domain.sensitivity`).

`resolve_sensitivity` runs on two write paths — `memory.set` and the
extractor's entries. Rows that arrive by `observe.record`, `copy_to`, the
imports, or were written before the column existed carry `sensitivity = ''`
whatever they hold, so a row with an `sk-`-shaped key or an e-mail address can
sit unlabelled, embedded and returned by every search leg.

The sweep is the detector, run late:

  1. a coarse ClickHouse `match()` narrows live unlabelled rows to the
     shapes the detector could label;
  2. `detect_sensitivity` — the write path's own regexes, in Python —
     decides the level exactly, so a swept row is labelled as it would have
     been on write;
  3. `apply` writes ONE new `loom.memory` version per labelled row
     (`build_memory_version_row` carries every column; never `ALTER UPDATE`)
     with the level and its `sensitivity:<level>` tag. A `high` row's
     embedding rows and entity vectors are re-emitted with a tombstone — the
     sensitivity invariant, "a high row has no embedding row". A `medium`
     row's embedding rows are re-emitted with the tag, which is the only
     place the tier rides on the table search reads.

Read-only by construction until `apply`; `scan` is what `--dry-run` prints.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.sensitivity import HIGH, MEDIUM, detect_sensitivity, sensitivity_tags

# RE2, for ClickHouse `match()`: a superset of the shapes in
# `memory/domain/sensitivity.py`'s high and medium patterns. Anything this misses
# the detector would miss too; anything it over-selects the detector drops.
COARSE_SHAPE_RE = (
    r"sk-|pk-lf-|AKIA|gh[opusr]_|github_pat_|xox[abprs]-|PRIVATE KEY|eyJ|AIza"
    r"|(?i:password|passwd|pwd|api[_-]?key|secret|access[_-]?token|auth[_-]?token"
    r"|bearer|client[_-]?secret|private[_-]?key)"
    r"|@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"|[0-9]{3}[\s.()-]*[0-9]{3}[\s.-]*[0-9]{4}"
)

# Two probe shapes reported beside the detector's levels, so a dry run says how
# many credential- and e-mail-shaped rows it found.
_PROBE_CREDENTIAL = r"sk-[A-Za-z0-9_-]{20,}"
_PROBE_EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"


@dataclass
class SweepCandidate:
    memory_id: str
    org: str
    namespace: str
    agent: str
    user_id: str
    session_id: str
    memory_key: str
    level: str  # 'high' | 'medium'
    shapes: tuple[str, ...]  # 'credential' | 'email' | 'phone' | 'other'


@dataclass
class SweepReport:
    scanned: int = 0  # candidate rows the coarse filter returned
    candidates: list[SweepCandidate] = field(default_factory=list)
    truncated: bool = False

    @property
    def by_level(self) -> Counter[str]:
        return Counter(c.level for c in self.candidates)

    @property
    def by_shape(self) -> Counter[str]:
        return Counter(s for c in self.candidates for s in c.shapes)

    def by_namespace(self) -> dict[tuple[str, str], Counter[str]]:
        out: dict[tuple[str, str], Counter[str]] = {}
        for c in self.candidates:
            out.setdefault((c.org, c.namespace), Counter())[c.level] += 1
        return out


def _shapes(text: str) -> tuple[str, ...]:
    import re

    shapes: list[str] = []
    if re.search(_PROBE_CREDENTIAL, text):
        shapes.append("credential")
    if re.search(_PROBE_EMAIL, text):
        shapes.append("email")
    from server.memory.domain.sensitivity import _MEDIUM_PATTERNS

    if _MEDIUM_PATTERNS[1].search(text):
        shapes.append("phone")
    return tuple(shapes) or ("other",)


def scan_sql(*, org: str | None, namespace: str | None, since_days: int | None) -> str:
    scope = ""
    if org:
        scope += " AND org = {org:String}"
    if namespace:
        scope += " AND namespace = {ns:String}"
    if since_days:
        scope += " AND created_at > now() - INTERVAL {days:UInt32} DAY"
    return f"""
        SELECT memory_id, org, namespace, agent, user_id, session_id, memory_key,
               if(length(toString(value_json)) > 2, toString(value_json), value) AS value
        FROM loom.memory FINAL
        WHERE tombstoned_at IS NULL AND sensitivity = ''
          AND (expires_at IS NULL OR expires_at > now())
          AND match(value, {{shape:String}})
          {scope}
        ORDER BY created_at, memory_key
        LIMIT {{lim:UInt32}}
    """


async def scan(
    *,
    org: str | None = None,
    namespace: str | None = None,
    since_days: int | None = None,
    limit: int = 20000,
    only_shapes: Iterable[str] | None = None,
    ch: _CHClient | None = None,
) -> SweepReport:
    """Live unlabelled rows the detector would label, exactly as the write
    path would have. Read-only. `only_shapes` keeps candidates carrying at
    least one of the named probe shapes (`credential`, `email`, `phone`,
    `other`) — the phone shape is a ten-digit run and the noisiest, so an
    operator can apply the others first."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"shape": COARSE_SHAPE_RE, "lim": int(limit)}
    if org:
        params["org"] = org
    if namespace:
        params["ns"] = namespace
    if since_days:
        params["days"] = int(since_days)
    rows = await ch.query_all(
        scan_sql(org=org, namespace=namespace, since_days=since_days), params=params
    )
    report = SweepReport(scanned=len(rows), truncated=len(rows) >= int(limit))
    keep = {str(x).strip().lower() for x in (only_shapes or ()) if str(x).strip()}
    for r in rows:
        text = str(r.get("value") or "")
        level = detect_sensitivity(text)
        if level not in (HIGH, MEDIUM):
            continue
        shapes = _shapes(text)
        if keep and not keep.intersection(shapes):
            continue
        report.candidates.append(
            SweepCandidate(
                memory_id=str(r["memory_id"]),
                org=str(r["org"]),
                namespace=str(r["namespace"]),
                agent=str(r.get("agent") or "-"),
                user_id=str(r.get("user_id") or "-"),
                session_id=str(r.get("session_id") or "-"),
                memory_key=str(r["memory_key"]),
                level=level,
                shapes=shapes,
            )
        )
    return report


@dataclass
class ApplyResult:
    relabelled: int = 0
    high: int = 0
    medium: int = 0
    embeddings_tombstoned: int = 0  # high rows' embedding rows
    entity_vectors_tombstoned: int = 0  # high rows' entity vectors
    embeddings_tagged: int = 0  # medium rows' embedding rows re-emitted with the tag
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "relabelled": self.relabelled,
            "high": self.high,
            "medium": self.medium,
            "embeddings_tombstoned": self.embeddings_tombstoned,
            "entity_vectors_tombstoned": self.entity_vectors_tombstoned,
            "embeddings_tagged": self.embeddings_tagged,
            "errors": len(self.errors),
        }


async def _tag_embeddings(c: SweepCandidate, *, ch: _CHClient) -> int:
    """Re-emit a medium row's live embedding rows with the `sensitivity:`
    tag (the embeddings table has no sensitivity column; the ranking reads
    the tag)."""
    from server.memory.store import (
        EMBEDDING_PERSISTED_COLS,
        build_embedding_row,
        insert_embedding_rows,
        live_embedding_rows_for_key,
        reemit_version,
    )

    rows = await live_embedding_rows_for_key(
        ch,
        org=c.org,
        namespace=c.namespace,
        agent=c.agent,
        user_id=c.user_id,
        session_id=c.session_id,
        memory_key=c.memory_key,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        tags = list(r.get("tags") or [])
        extra = sensitivity_tags(c.level, tags)
        if not extra:
            continue
        out.append(
            build_embedding_row(
                **(
                    {col: val for col, val in r.items() if col in EMBEDDING_PERSISTED_COLS}
                    | {"tags": [*tags, *extra], "version": reemit_version(r)}
                )
            )
        )
    if out:
        await insert_embedding_rows(out, ch=ch)
    return len(out)


async def apply(candidates: list[SweepCandidate], *, ch: _CHClient | None = None) -> ApplyResult:
    """Label each candidate as the write path would have: one new memory
    version, then the tier's mirror on the tables search reads."""
    import server.memory.store as memory_repo

    ch = ch or _default_client()
    result = ApplyResult()
    now = naive_utc_now()
    for c in candidates:
        try:

            def _relabel(existing: Any, _c: SweepCandidate = c) -> dict[str, Any]:
                tags = list(getattr(existing, "tags", None) or [])
                return {
                    "sensitivity": _c.level,
                    "tags": [*tags, *sensitivity_tags(_c.level, tags)],
                    "updated_at": now,
                }

            n = await memory_repo.write_versions_by_ids(c.org, [c.memory_id], _relabel, ch=ch)
            if not n:
                continue  # gone between scan and apply
            result.relabelled += n
            if c.level == HIGH:
                result.high += 1
                result.embeddings_tombstoned += await memory_repo.tombstone_embeddings_for_key(
                    c.org,
                    c.namespace,
                    c.agent,
                    c.user_id,
                    c.session_id,
                    c.memory_key,
                    ts=now,
                    ch=ch,
                )
                result.entity_vectors_tombstoned += (
                    await memory_repo.tombstone_entity_vectors_for_keys(
                        [(c.org, c.namespace, c.agent, c.user_id, c.session_id, c.memory_key)],
                        ts=now,
                        ch=ch,
                    )
                )
            else:
                result.medium += 1
                result.embeddings_tagged += await _tag_embeddings(c, ch=ch)
        except Exception as e:  # noqa: BLE001 — one row's failure never stops the sweep
            result.errors.append(f"{c.namespace}/{c.memory_key}: {e}")
    return result


# The sensitivity invariant ("a high row has no embedding row"), as a count (SQL shared with
# the state probe's `loom.integrity.high_with_embedding` gauge).
from server.memory.store.integrity import HIGH_WITH_EMBEDDING_SQL  # noqa: E402


async def high_with_embedding_count(
    *, org: str | None = None, namespace: str | None = None, ch: _CHClient | None = None
) -> int:
    ch = ch or _default_client()
    where = ""
    params: dict[str, Any] = {}
    if org:
        where += " AND org = {org:String}"
        params["org"] = org
    if namespace:
        where += " AND namespace = {ns:String}"
        params["ns"] = namespace
    row = await ch.query_one(
        f"SELECT count() AS n FROM ({HIGH_WITH_EMBEDDING_SQL}) WHERE 1 {where}", params=params
    )
    return int((row or {}).get("n") or 0)


__all__ = [
    "COARSE_SHAPE_RE",
    "HIGH_WITH_EMBEDDING_SQL",
    "ApplyResult",
    "SweepCandidate",
    "SweepReport",
    "apply",
    "high_with_embedding_count",
    "scan",
    "scan_sql",
]
