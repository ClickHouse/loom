"""The record of what Loom retired on its own.

Each automatic path that retires rows (`status='superseded'`) records them once,
after the retirement: one hash-only `memory.auto_supersede` audit row per
`MAX_ENTRIES` rows, naming each row, the row it retired behind and the reason.
`memory.restore` undoes one. See "Automatic retirements" in
docs/features/memory/conflicts-and-refusal.md."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from server.memory.domain.temporal_sql import LIVE_STATUSES

if TYPE_CHECKING:
    from server.memory.store import MemoryRow

log = logging.getLogger(__name__)

EVENT_TYPE = "memory.auto_supersede"

# Why a row retired, one per automatic path.
SAME_KEY_NEWEST_WINS = "same_key_newest_wins"
CONFLICT_RESOLVER = "conflict_resolver"
NEWEST_WINS_CONTRADICTION = "newest_wins_contradiction"
VALUE_UPDATE = "value_update"

# The most retired rows one audit row lists; more are split across several rows.
MAX_ENTRIES = 200


_ENTRY_FIELDS = (
    "memory_id",
    "memory_key",
    "agent",
    "user_id",
    "session_id",
    "superseded_by",
    "superseded_by_key",
    "label",
    "unconfirmed",
)


def retired_entry(row: Mapping[str, Any] | Any, **extra: Any) -> dict[str, str]:
    """The audit entry for one retired row (a dict or a `MemoryRow`): ids, keys,
    identity and the label (the conflict resolver's `update`). Any other field, the
    value first, is dropped."""
    src = dict(row) if isinstance(row, Mapping) else {f: getattr(row, f, "") for f in _ENTRY_FIELDS}
    src.update(extra)
    out: dict[str, str] = {}
    for f in _ENTRY_FIELDS:
        v = src.get(f)
        if v is None or v == "":
            continue
        out[f] = str(v).lower() if isinstance(v, bool) else str(v)
    return out


class RetirementTracker:
    """Which rows a supersession actually retired, for the record.

    Pass `skip`, `written` and `unconfirmed` to `supersede_duplicates` /
    `write_versions_by_ids`. `skip` refuses a row `base` refuses (a lock), a pinned
    row and a row that is no longer live (`LIVE_STATUSES`); `written` hears the rows
    whose retirement the re-read confirmed, `unconfirmed` those it could not tell
    about. `retired(victims)` keeps the victims among both, the second marked
    `unconfirmed`, so a refused, vanished, overtaken or never-written row is never
    recorded. `rows` holds the written rows themselves, for a caller with no victim
    list of its own."""

    def __init__(self, base: Callable[[MemoryRow], bool] | None = None) -> None:
        self._base = base
        self.ids: set[str] = set()
        self.unconfirmed_ids: set[str] = set()
        self.rows: list[Mapping[str, Any]] = []

    def skip(self, row: MemoryRow) -> bool:
        # Only a live row moves to `superseded`; any other is already retired (or
        # tombstoned, rejected, proposed) and is left as it is.
        if row.status not in LIVE_STATUSES:
            return True
        # A pin is a write-time invariant: nothing automatic retires a pinned row.
        if row.pinned:
            return True
        return self._base is not None and self._base(row)

    def written(self, rows: Iterable[Mapping[str, Any]]) -> None:
        rows = list(rows)
        self.rows.extend(rows)
        self.ids.update(str(r["memory_id"]) for r in rows)

    async def unconfirmed(self, rows: list[dict[str, Any]]) -> None:
        """Rows the re-read could not confirm: recorded as such. Recall keeps its own
        copy; `loom reconcile-status` mirrors a divergence it finds."""
        self.rows.extend(rows)
        self.unconfirmed_ids.update(str(r["memory_id"]) for r in rows)

    def retired(self, victims: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """The victims whose retirement took effect or may have: those the re-read
        could not confirm carry `unconfirmed=True`, and no result counts them."""
        out = []
        for v in victims:
            mid = str(v.get("memory_id") or "")
            if mid in self.ids:
                out.append(dict(v))
            elif mid in self.unconfirmed_ids:
                out.append({**v, "unconfirmed": True})
        return out


def _request_id() -> str:
    from server.core.logging_config import request_id_var

    return request_id_var.get() or ""


async def record_auto_supersessions(
    *,
    org: str,
    namespace: str,
    retired: Sequence[Mapping[str, Any]],
    reason: str,
    request_id: str | None = None,
) -> bool:
    """`memory.auto_supersede` audit rows for the rows one path just retired, one per
    `MAX_ENTRIES` of them. `retired`: dicts with `memory_id`, `memory_key`, `agent`,
    `user_id`, `session_id`, `superseded_by` and optionally `label` /
    `superseded_by_key`. Nothing to record writes nothing. Never raises; False when a
    row was not written."""
    left = await unrecorded(
        org=org, namespace=namespace, retired=retired, reason=reason, request_id=request_id
    )
    return not left


async def unrecorded(
    *,
    org: str,
    namespace: str,
    retired: Sequence[Mapping[str, Any]],
    reason: str,
    request_id: str | None = None,
) -> list[dict[str, Any]]:
    """`record_auto_supersessions`, answering the entries whose audit row was not
    written ([] when every one was). Never raises."""
    from server.memory.app.provenance import writer_actor
    from server.observability import audit as _audit

    entries = [retired_entry(r) for r in retired]
    actor = writer_actor(fallback="process:loom") or "process:loom"
    rid = _request_id() if request_id is None else request_id
    left: list[dict[str, Any]] = []
    for start in range(0, len(entries), MAX_ENTRIES):
        chunk = entries[start : start + MAX_ENTRIES]
        try:
            written = await _audit.emit(
                EVENT_TYPE,
                org=org,
                namespace=namespace,
                actor=actor,
                subject={"reason": reason, "count": len(chunk), "retired": chunk},
                request_id=rid,
            )
        except Exception as e:  # noqa: BLE001 — the retirement stands; its record is owed
            log.warning("auto_resolution: audit emit failed (non-fatal): %s", e)
            written = None
        if not written:
            left += chunk
    return left


async def settle_retirements(
    *,
    org: str,
    namespace: str,
    retired: Sequence[Mapping[str, Any]],
    reason: str,
    follows: list[tuple[str, str]],
    agent: str = "",
    intent: str = "",
) -> None:
    """The record of rows a point write just retired (`record_or_owe`), and their
    (loser, winner) pairs added to `follows`, which the caller follows once its own
    relations are projected (a follow before them writes the successor fact they then
    write again). Never raises."""
    follows.extend((str(r["memory_id"]), str(r["superseded_by"])) for r in retired)
    await record_or_owe(
        org=org, namespace=namespace, retired=retired, reason=reason, agent=agent, intent=intent
    )


async def owe_retirements(
    *, org: str, namespace: str, victims: Sequence[Mapping[str, Any]], reason: str, agent: str
) -> str:
    """A FOLLOW_OWED intent for rows about to be retired, written before the supersede as
    the conflict resolver writes one: a process stopped before their record leaves it for
    the repair, which acts only on a row still retired behind its winner. Its id, or ''
    when it could not be written (the record after the supersede is owed as before)."""
    from server.memory.app.projection_failures import FOLLOW_OWED, record_projection_failure

    if not victims:
        return ""
    pairs = [[str(v["memory_id"]), str(v["superseded_by"])] for v in victims]
    try:
        return await record_projection_failure(
            org=org,
            namespace=namespace,
            agent=agent,
            projection=FOLLOW_OWED,
            source_operation=reason,
            source_resource_id=f"{reason}:{pairs[0][1]}",
            error=f"retirement to record: {len(pairs)} row(s)",
            payload={"pairs": pairs, "retired": [dict(v) for v in victims], "reason": reason},
        )
    except Exception as e:  # noqa: BLE001 — the write goes on; its record is owed after it
        log.warning("auto_resolution: retirement intent not written: %s", e)
        return ""


async def record_or_owe(
    *,
    org: str,
    namespace: str,
    retired: Sequence[Mapping[str, Any]],
    reason: str,
    agent: str = "",
    intent: str = "",
) -> None:
    """`record_auto_supersessions`; the entries whose record failed are owed to the
    projection repair as a FOLLOW_OWED intent (`agent`: the winner's writer) with their
    reason, which the repair writes once. `intent`: the one `owe_retirements` wrote
    before the supersede, resolved once this is done; the record names it, which is how
    a replay finds it. Never raises."""
    left = await unrecorded(
        org=org, namespace=namespace, retired=retired, reason=reason, request_id=intent or None
    )
    if left:
        await _owe(org=org, namespace=namespace, left=left, reason=reason, agent=agent)
    if intent:
        from server.memory.app.projection_failures import mark_resolved

        try:
            await mark_resolved(org, [intent])
        except Exception as e:  # noqa: BLE001 — a replay of recorded rows writes nothing new
            log.warning("auto_resolution: retirement intent left for the repair: %s", e)


async def _owe(
    *, org: str, namespace: str, left: list[dict[str, Any]], reason: str, agent: str
) -> None:
    from types import SimpleNamespace

    from server.memory.app.projection_errors import record_input_projection_failure
    from server.memory.app.projection_failures import FOLLOW_OWED

    pairs = [[str(r["memory_id"]), str(r["superseded_by"])] for r in left]
    await record_input_projection_failure(
        SimpleNamespace(org=org, namespace=namespace, agent=agent),
        projection=FOLLOW_OWED,
        error=f"retirement left for the repair to record: {len(pairs)} row(s)",
        payload={"pairs": pairs, "retired": left, "reason": reason},
        source_operation=reason,
        source_resource_id=f"{reason}:{pairs[0][1]}",
    )


def follow_beside_response(
    *, org: str, namespace: str, pairs: list[tuple[str, str]], op: str, agent: str = ""
) -> None:
    """The triple plane's follow of rows a point write retired, beside the response
    (`after_write.schedule`) once the write's relations are in; what it could not follow
    is left to the projection repair (FOLLOW_ROWS). Never raises."""
    if not pairs:
        return
    try:
        from server.memory.app import after_write

        follow = _follow(org, namespace, agent, list(pairs), op)
        after_write.schedule(follow, name=f"{op}.follow")
    except Exception as e:  # noqa: BLE001 — the rows are retired; the repair follows them
        log.warning("%s: scheduling the triples follow failed: %s", op, e)


async def _follow(
    org: str, namespace: str, agent: str, pairs: list[tuple[str, str]], op: str
) -> None:
    from types import SimpleNamespace

    from server.core.ch import get_client
    from server.memory.app.entity_projections import follow_row_supersessions
    from server.memory.app.projection_errors import record_input_projection_failure
    from server.memory.app.projection_failures import FOLLOW_ROWS

    scope = SimpleNamespace(org=org, namespace=namespace, agent=agent)
    try:
        left = (await follow_row_supersessions(scope, pairs, ch=get_client())).unfollowed
    except Exception as e:  # noqa: BLE001 — the rows are retired; the repair follows them
        log.warning("%s: triples follow failed (recorded for repair): %s", op, e)
        left = pairs
    if left:
        await record_input_projection_failure(
            scope,
            projection=FOLLOW_ROWS,
            error=f"supersession pairs left for the repair to follow: {len(left)}",
            payload={"pairs": [list(p) for p in left]},
            source_operation=op,
            source_resource_id=f"{op}:{left[0][1]}",
        )
