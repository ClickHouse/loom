"""The record of what Loom retired on its own.

Each automatic path that retires rows (`status='superseded'`) records them once,
after the retirement: one hash-only `memory.auto_supersede` audit row per
`MAX_ENTRIES` rows, naming each row, the row it retired behind and the reason.
`memory.restore` undoes one. See "Automatic retirements" in
docs/features/memory/conflicts-and-refusal.md."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple

from server.identity.domain.read_scope import row_in_read_scope
from server.memory.api.models_crud import (
    MemoryAutoRetirement,
    MemoryAutoRetirementsInput,
    MemoryAutoRetirementsOutput,
)
from server.memory.app.projection_errors import ProjectionScope
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
MAX_ENTRIES = 200  # not a setting: the size of one stored audit row


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
    # The conflict resolver's grounds for a retirement. Prefixed: a memory row has its own
    # `confidence`, which an entry built from a whole row must not carry.
    "judge_verdict",
    "judge_confidence",
    "judge_model",
    "judge_prompt_version",
)


def retired_entry(row: Mapping[str, Any] | Any, **extra: Any) -> dict[str, str]:
    """The audit entry for one retired row (a dict or a `MemoryRow`): ids, keys,
    identity, the label (the conflict resolver's `update`) and the resolver's judge_*
    fields. Any other field, the value and the row's own `confidence` first, is
    dropped."""
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
    `write_versions_by_ids`. `skip` refuses a row `base` refuses
    (`server.memory.domain.lock`), a pinned row and a row that is no longer live
    (`LIVE_STATUSES`); `written` hears the rows whose retirement the re-read confirmed,
    `unconfirmed` those it could not tell about. `retired(victims)` keeps the victims
    among both, the second marked `unconfirmed`, so a refused, vanished, overtaken or
    never-written row is never recorded. `rows` holds the written rows themselves, for
    a caller with no victim list of its own."""

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


class Follow(NamedTuple):
    """A retired pair a point write follows beside its response, and the intent that owes
    it once its record landed ('' otherwise): resolved when the follow lands too."""

    loser: str
    winner: str
    intent: str = ""


async def settle_retirements(
    *,
    org: str,
    namespace: str,
    retired: Sequence[Mapping[str, Any]],
    reason: str,
    follows: list[Follow],
    agent: str = "",
    intent: str = "",
) -> None:
    """Record the rows a point write just retired and add their pairs to `follows`, which
    the caller follows once its own relations are projected (a follow before them writes
    the successor fact they then write again). Never raises."""
    recorded = await record_or_owe(
        org=org, namespace=namespace, retired=retired, reason=reason, agent=agent, intent=intent
    )
    owed = intent if recorded else ""
    follows.extend(Follow(str(r["memory_id"]), str(r["superseded_by"]), owed) for r in retired)


async def owe_retirements(
    *, org: str, namespace: str, victims: Sequence[Mapping[str, Any]], reason: str, agent: str
) -> str:
    """Owe the record and follow of rows about to be retired, before the supersede: a lost
    answer or a stop before either leaves them to the repair. Its id, or '' when not
    written (the record is then owed after the supersede, as before)."""
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
            # Entries as the record holds them: ids, keys and identity, never the value.
            payload={
                "pairs": pairs,
                "retired": [retired_entry(v) for v in victims],
                "reason": reason,
            },
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
) -> bool:
    """Record the retired rows, naming `intent` (the one `owe_retirements` wrote, which
    already owes them), else owe the failed entries to the repair; True when every record
    landed. Never raises: an unrecorded retirement is the repair's to record."""
    left = await unrecorded(
        org=org, namespace=namespace, retired=retired, reason=reason, request_id=intent or None
    )
    if left and not intent:
        await _owe(org=org, namespace=namespace, left=left, reason=reason, agent=agent)
    return not left


async def _owe(
    *, org: str, namespace: str, left: list[dict[str, Any]], reason: str, agent: str
) -> None:
    from server.memory.app.projection_errors import record_input_projection_failure
    from server.memory.app.projection_failures import FOLLOW_OWED

    pairs = [[str(r["memory_id"]), str(r["superseded_by"])] for r in left]
    await record_input_projection_failure(
        # No top-level user: each `retired` entry names its row's, as erasure reads it.
        ProjectionScope(org=org, namespace=namespace, agent=agent, user_id=""),
        projection=FOLLOW_OWED,
        error=f"retirement left for the repair to record: {len(pairs)} row(s)",
        payload={"pairs": pairs, "retired": left, "reason": reason},
        source_operation=reason,
        source_resource_id=f"{reason}:{pairs[0][1]}",
    )


def follow_beside_response(
    *, org: str, namespace: str, pairs: list[Follow], op: str, agent: str = "", user_id: str = ""
) -> None:
    """The triple plane's follow of rows a point write retired, beside the response
    (`after_write.schedule`) once the write's relations are in; what it could not follow
    is left to the projection repair (FOLLOW_ROWS). Once all are followed, the intents
    whose record landed are resolved in the next batched mark. Never raises."""
    if not pairs:
        return
    try:
        from server.memory.app import after_write

        intents = sorted({p.intent for p in pairs if p.intent})
        scope = ProjectionScope(org=org, namespace=namespace, agent=agent, user_id=user_id)
        follow = _follow(scope, [(p.loser, p.winner) for p in pairs], op, intents)
        after_write.schedule(follow, name=f"{op}.follow")
    except Exception as e:  # noqa: BLE001 — the rows are retired; the repair follows them
        log.warning("%s: scheduling the triples follow failed: %s", op, e)


async def follow_pairs(scope: Any, pairs: list[tuple[str, str]], op: str) -> list[tuple[str, str]]:
    """The triple plane follows the retired (loser, winner) `pairs` as `scope`; the pairs
    left unfollowed, every one when the follow failed."""
    from server.core.ch import get_client
    from server.memory.app.triple_follow import follow_row_supersessions

    try:
        return (await follow_row_supersessions(scope, pairs, ch=get_client())).unfollowed
    except Exception as e:  # noqa: BLE001 — the rows are retired; the repair follows them
        log.warning("%s: triples follow failed (left for the repair): %s", op, e)
        return pairs


async def _follow(
    scope: Any, pairs: list[tuple[str, str]], op: str, intents: list[str] | None = None
) -> None:
    """Follow the pairs as `scope` (org, namespace, agent and the writer's user_id, which
    a failure records); what is left goes to the repair."""
    from server.memory.app.projection_errors import record_input_projection_failure
    from server.memory.app.projection_failures import FOLLOW_ROWS

    left = await follow_pairs(scope, pairs, op)
    if left:
        await record_input_projection_failure(
            scope,
            projection=FOLLOW_ROWS,
            error=f"supersession pairs left for the repair to follow: {len(left)}",
            payload={"pairs": [list(p) for p in left]},
            source_operation=op,
            source_resource_id=f"{op}:{left[0][1]}",
        )
    elif intents:
        from server.memory.app import intent_marks

        # Recorded and followed: nothing is left for the intent to owe.
        intent_marks.resolve_soon(scope.org, intents)


def _confidence(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def retirements_in(ts: datetime, subject: str) -> list[MemoryAutoRetirement]:
    """The retired memories one record's `subject` lists; an entry with no id or key is
    left out, as is every entry of a subject that is not a record."""
    try:
        record = json.loads(subject)
    except ValueError:
        return []
    if not isinstance(record, dict) or not isinstance(record.get("retired"), list):
        return []
    out = []
    for e in record["retired"]:
        if not isinstance(e, dict) or not e.get("memory_id") or not e.get("memory_key"):
            continue
        fields: dict[str, Any] = {f: str(e[f]) for f in _ENTRY_FIELDS if e.get(f)}
        fields["unconfirmed"] = fields.get("unconfirmed") == "true"
        fields["judge_confidence"] = _confidence(fields.get("judge_confidence"))
        out.append(MemoryAutoRetirement(ts=ts, reason=str(record.get("reason") or ""), **fields))
    return out


async def handle_memory_auto_retirements(
    input: MemoryAutoRetirementsInput,
) -> MemoryAutoRetirementsOutput:
    """memory.auto_retirements: the `memory.auto_supersede` records of the namespace,
    one row per retired memory inside the request's read scope. A call reads at most
    `limit` records and stops after the record that brings the page to `limit` rows, so a
    page can hold more than `limit` rows (a record is never split) or, when the scope
    leaves records out, fewer with `next_cursor` set."""
    from server.identity.app.request_scope import request_scope
    from server.insights.app.audit import namespace_events, record_cursor

    scope = request_scope()
    # The Auto-resolved tab's window (AUTO_RESOLVED_WINDOW_MS in frontend/src/api/audit.ts).
    since = input.since or datetime.now(UTC) - timedelta(days=30)
    records = await namespace_events(
        input.org,
        input.namespace,
        EVENT_TYPE,
        since=since,
        cursor=input.cursor,
        limit=input.limit + 1,  # +1 to tell whether an older page exists
    )
    rows: list[MemoryAutoRetirement] = []
    read = 0
    for record in records[: input.limit]:
        if len(rows) >= input.limit:
            break
        read += 1
        for r in retirements_in(record.ts, record.subject):
            if row_in_read_scope(scope, r.user_id, r.session_id):
                rows.append(r)
    next_cursor = record_cursor(records[read - 1]) if read < len(records) else None
    return MemoryAutoRetirementsOutput(rows=rows, next_cursor=next_cursor)
