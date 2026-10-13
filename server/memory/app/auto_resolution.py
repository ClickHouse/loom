"""The record of what Loom retired on its own.

Several write paths retire memories without anyone asking: the same-key judge, the
`newest_wins` policy, a refines verdict, a value update. Each sets
`status='superseded'` and `superseded_by`, and the row leaves recall; nothing on the
row says why. Each path that retired rows calls `record_auto_supersessions` once,
after the retirement, and it writes one `memory.auto_supersede` audit row naming
the retired rows, the row each retired behind and the reason. The Review queue's
Auto-resolved tab lists those rows, and `memory.restore` undoes one.

The subject holds ids, keys and identity only, never value text: the audit log is
hash-only by design."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

log = logging.getLogger(__name__)

EVENT_TYPE = "memory.auto_supersede"

# Why a row retired, one per automatic path.
SAME_KEY_JUDGE = "same_key_judge"
SAME_KEY_NEWEST_WINS = "same_key_newest_wins"
REFINES = "refines"
NEWEST_WINS_CONTRADICTION = "newest_wins_contradiction"
VALUE_UPDATE = "value_update"

# The most retired rows one audit row lists; `count` still says how many retired.
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
    """A `skip` for `supersede_duplicates` / `write_versions_by_ids` that remembers
    which rows the write did not skip, so the record names only rows that retired
    (a lock-refused or vanished row is never asked, or answers True)."""

    def __init__(self, base: Callable[[Any], bool] | None = None) -> None:
        self._base = base
        self.written: set[str] = set()

    def skip(self, row: Any) -> bool:
        if self._base is not None and self._base(row):
            return True
        self.written.add(str(getattr(row, "memory_id", "") or ""))
        return False

    def retired(self, victims: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        return [v for v in victims if str(v.get("memory_id") or "") in self.written]


async def record_auto_supersessions(
    *,
    org: str,
    namespace: str,
    retired: Sequence[Mapping[str, Any]],
    reason: str,
) -> None:
    """One `memory.auto_supersede` audit row for the rows one path just retired.
    `retired`: dicts with `memory_id`, `memory_key`, `agent`, `user_id`,
    `session_id`, `superseded_by` and optionally `label` / `superseded_by_key`.
    Nothing to record writes nothing. Never raises."""
    try:
        if not retired:
            return
        from server.memory.app.provenance import writer_actor
        from server.observability import audit as _audit

        entries = [retired_entry(r) for r in retired]
        await _audit.emit(
            EVENT_TYPE,
            org=org,
            namespace=namespace,
            actor=writer_actor(fallback="process:loom") or "process:loom",
            subject={
                "reason": reason,
                "count": len(entries),
                "retired": entries[:MAX_ENTRIES],
            },
        )
    except Exception as e:  # noqa: BLE001 — the retirement stands; its record is best effort
        log.warning("auto_resolution: audit emit failed (non-fatal): %s", e)
