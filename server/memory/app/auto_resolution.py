"""The record of what Loom retired on its own.

Several write paths retire memories without anyone asking: the same-key judge, the
`newest_wins` policy (same key, a judged contradiction, a repaired triple), a refines
verdict, a value update. Each sets `status='superseded'` and `superseded_by`, and the
row leaves recall; nothing on the row says why. Each path that retired rows records
them once, after the retirement: one `memory.auto_supersede` audit row per
`MAX_ENTRIES` retired rows, naming each row, the row it retired behind and the reason.
A write that retires through several paths leaves one record per path. The Review
queue's Auto-resolved tab lists those rows, and `memory.restore` undoes one.

The subject holds ids, keys and identity only, never value text: the audit log is
hash-only by design. A point write (`memory.set`, `memory.update_by_id`, the deferred
judge) records beside the response (`schedule_auto_supersessions`), so the write does
not wait on the extra insert."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

# The statuses a retirement may move to `superseded` (the live ones); any other row is
# already retired (or tombstoned, rejected, proposed) and is left as it is.
from server.memory.domain.temporal_sql import LIVE_STATUSES

log = logging.getLogger(__name__)

EVENT_TYPE = "memory.auto_supersede"

# Why a row retired, one per automatic path.
SAME_KEY_JUDGE = "same_key_judge"
SAME_KEY_NEWEST_WINS = "same_key_newest_wins"
REFINES = "refines"
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
)


def retired_entry(row: Mapping[str, Any] | Any, **extra: Any) -> dict[str, str]:
    """The audit entry for one retired row (a dict or a `MemoryRow`): ids, keys,
    identity and the judge's label. Any other field, the value first, is dropped."""
    src = dict(row) if isinstance(row, Mapping) else {f: getattr(row, f, "") for f in _ENTRY_FIELDS}
    src.update(extra)
    out: dict[str, str] = {}
    for f in _ENTRY_FIELDS:
        v = src.get(f)
        if v is None or v == "":
            continue
        out[f] = str(v)
    return out


class RetirementTracker:
    """Which rows a supersession actually retired, for the record.

    Pass `skip` and `written` to `supersede_duplicates` / `write_versions_by_ids`.
    `skip` refuses a row `base` refuses (a lock), a pinned row and a row that is no
    longer live (`LIVE_STATUSES`); `written` hears the rows whose superseded version reached
    `loom.memory`. `retired(victims)` keeps the victims among those, so a refused,
    vanished or never-written row is never recorded."""

    def __init__(self, base: Callable[[Any], bool] | None = None) -> None:
        self._base = base
        self.ids: set[str] = set()

    def skip(self, row: Any) -> bool:
        if str(getattr(row, "status", "") or "active") not in LIVE_STATUSES:
            return True
        # A pin is a write-time invariant: nothing automatic retires a pinned row.
        if bool(getattr(row, "pinned", False)):
            return True
        return self._base is not None and bool(self._base(row))

    def written(self, rows: Iterable[Any]) -> None:
        self.ids.update(str(getattr(r, "memory_id", "") or "") for r in rows)

    def retired(self, victims: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [v for v in victims if str(v.get("memory_id") or "") in self.ids]


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
) -> None:
    """`memory.auto_supersede` audit rows for the rows one path just retired, one per
    `MAX_ENTRIES` of them. `retired`: dicts with `memory_id`, `memory_key`, `agent`,
    `user_id`, `session_id`, `superseded_by` and optionally `label` /
    `superseded_by_key`. Nothing to record writes nothing. Never raises."""
    try:
        if not retired:
            return
        from server.memory.app.provenance import writer_actor
        from server.observability import audit as _audit

        entries = [retired_entry(r) for r in retired]
        actor = writer_actor(fallback="process:loom") or "process:loom"
        rid = _request_id() if request_id is None else request_id
        for start in range(0, len(entries), MAX_ENTRIES):
            chunk = entries[start : start + MAX_ENTRIES]
            await _audit.emit(
                EVENT_TYPE,
                org=org,
                namespace=namespace,
                actor=actor,
                subject={"reason": reason, "count": len(chunk), "retired": chunk},
                request_id=rid,
            )
    except Exception as e:  # noqa: BLE001 — the retirement stands; its record is best effort
        log.warning("auto_resolution: audit emit failed (non-fatal): %s", e)


def schedule_auto_supersessions(
    *, org: str, namespace: str, retired: Sequence[Mapping[str, Any]], reason: str
) -> None:
    """`record_auto_supersessions` beside the response (`after_write.schedule`, which
    keeps the request's context: its actor, request id and query tag). Never raises."""
    if not retired:
        return
    try:
        from server.memory.app import after_write

        after_write.schedule(
            record_auto_supersessions(
                org=org,
                namespace=namespace,
                retired=[dict(r) for r in retired],
                reason=reason,
            ),
            name="memory.auto_supersede",
        )
    except Exception as e:  # noqa: BLE001 — the retirement stands; its record is best effort
        log.warning("auto_resolution: scheduling the record failed (non-fatal): %s", e)
