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

from server.memory.app.projection_failures import queue_status_mirror
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
        """Rows the re-read could not confirm: recorded, and queued for the repair to
        mirror their head onto recall, before the write goes on."""
        self.rows.extend(rows)
        self.unconfirmed_ids.update(str(r["memory_id"]) for r in rows)
        await queue_status_mirror(rows)

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
    try:
        if not retired:
            return True
        from server.memory.app.provenance import writer_actor
        from server.observability import audit as _audit

        entries = [retired_entry(r) for r in retired]
        actor = writer_actor(fallback="process:loom") or "process:loom"
        rid = _request_id() if request_id is None else request_id
        written = True
        for start in range(0, len(entries), MAX_ENTRIES):
            chunk = entries[start : start + MAX_ENTRIES]
            written = (
                bool(
                    await _audit.emit(
                        EVENT_TYPE,
                        org=org,
                        namespace=namespace,
                        actor=actor,
                        subject={"reason": reason, "count": len(chunk), "retired": chunk},
                        request_id=rid,
                    )
                )
                and written
            )
        return written
    except Exception as e:  # noqa: BLE001 — the retirement stands; its record is best effort
        log.warning("auto_resolution: audit emit failed (non-fatal): %s", e)
        return False


async def settle_retirements(
    *, org: str, namespace: str, retired: Sequence[Mapping[str, Any]], reason: str
) -> None:
    """The record and the graph follow of rows a point write just retired: the record
    (`record_auto_supersessions`) and the triple plane's follow
    (`entity_projections.follow_row_supersessions`), as a bulk write's. What fails is
    owed to the projection repair: a record as a FOLLOW_OWED intent with its entries and
    reason (its replay follows too), a follow alone as FOLLOW_ROWS. Never raises."""
    pairs = [(str(r["memory_id"]), str(r["superseded_by"])) for r in retired]
    if not pairs:
        return
    from types import SimpleNamespace

    from server.core.ch import get_client
    from server.memory.app.entity_projections import follow_row_supersessions
    from server.memory.app.projection_errors import record_input_projection_failure
    from server.memory.app.projection_failures import FOLLOW_OWED, FOLLOW_ROWS

    recorded = await record_auto_supersessions(
        org=org, namespace=namespace, retired=retired, reason=reason
    )
    scope = SimpleNamespace(org=org, namespace=namespace, agent="")
    try:
        left = (await follow_row_supersessions(scope, pairs, ch=get_client())).unfollowed
    except Exception as e:  # noqa: BLE001 — the rows are retired; the repair follows them
        log.warning("%s: triples follow failed (recorded for repair): %s", reason, e)
        left = pairs
    if recorded and not left:
        return
    payload: dict[str, Any]
    if recorded:
        owed, payload = FOLLOW_ROWS, {"pairs": [list(p) for p in left]}
    else:
        entries = [retired_entry(r) for r in retired]
        owed = FOLLOW_OWED
        payload = {"pairs": [list(p) for p in pairs], "retired": entries, "reason": reason}
    await record_input_projection_failure(
        scope,
        projection=owed,
        error=f"retirement left for the repair: {len(pairs)} row(s)",
        payload=payload,
        source_operation=reason,
        source_resource_id=f"{reason}:{pairs[0][1]}",
    )
