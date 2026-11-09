"""Read-side memory query handlers — list / aggregate / bulk_get."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import HTTPException

from server.auth.context import current_auth
from server.core.ch import caller_shaped, get_client
from server.core.datetimes import naive_utc_now
from server.core.tag_sql import TagFilter, list_tag_filter, tag_where
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_where
from server.memory.api.models_bulk import MemoryBulkGetInput, MemoryBulkGetOutput
from server.memory.api.models_crud import (
    MemoryGetOutput,
    MemoryKeyRef,
    MemoryKeysInput,
    MemoryKeysOutput,
    MemoryListInput,
    MemoryListOutput,
    MemoryStatus,
    MemoryStatusesInput,
    MemoryStatusesOutput,
    _MemoryListEntry,
)
from server.memory.api.models_search import (
    MemoryAggregateInput,
    MemoryAggregateOutput,
    _AggregateRow,
)
from server.memory.app.get_output import duplicate_of_shown, row_output
from server.memory.app.provenance import writer_actor
from server.memory.app.served_superseded import ServedFlags, shown_successors
from server.memory.domain import trust as _trust
from server.memory.domain.entity_sentinels import public_entities
from server.memory.domain.identity_columns import IdentityColumn
from server.memory.store.rows import MemoryRow
from server.observability import audit as _audit
from server.observability.budget_shape import note_budget_shape

log = logging.getLogger(__name__)


def _wants_usage(keep: frozenset[str] | None) -> bool:
    """Whether a projection serves a usage counter, which costs one more statement."""
    return keep is None or bool(keep & {"evidence_count", "recall_count", "last_recalled_at"})


def _preview_for_list(row: MemoryRow, max_chars: int) -> tuple[Any, bool]:
    """`(value, value_truncated)` for one list row —
    the value cut to `max_chars`, and whether the row holds more than was
    sent (the stored preview was already cut at 2,048, or this cut did it). A 'high'
    row's value is readable by key only, through an audited read: none is sent."""
    if row.sensitivity == "high":
        return None, True
    raw = row.value
    value = _truncate_value_for_list(raw, max_chars)
    stored_cut = row.value_truncated
    decoded = _decode_value(raw)
    if isinstance(decoded, str):
        text = decoded
    else:
        text = raw if isinstance(raw, str) else json.dumps(decoded, default=str)
    return value, stored_cut or len(text) > max_chars


def _truncate_value_for_list(raw: Any, max_bytes: int) -> Any:
    """JSON-decoded `value` capped at `max_bytes` to bound memory.list payloads.

    The cap applies to every shape: an object or list whose JSON text exceeds it comes back as
    that text cut to `max_bytes` — the list shows a preview and the card round-trips
    `memory.get` for the whole value. Capping only string values would let a namespace of
    JSON-object memories (alerts, reports) ship every value whole (100 such rows can be several
    MB per page, refetched by a polling view)."""
    decoded = _decode_value(raw)
    if isinstance(decoded, str):
        return decoded[:max_bytes] + "…" if len(decoded) > max_bytes else decoded
    if isinstance(decoded, (dict, list)):
        text = raw if isinstance(raw, str) else json.dumps(decoded, default=str)
        if len(text) > max_bytes:
            return text[:max_bytes] + "…"
    return decoded


def _decode_value(raw: Any) -> Any:
    """Best-effort JSON decode of the stored `value`; accepts already-parsed
    objects and falls back to the raw scalar on parse failure."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


async def handle_memory_list(input: MemoryListInput) -> MemoryListOutput:
    """memory.list, then one exposure-ledger row for the keys it returned: the list op is the
    widest read a token can make. Under Postgres it reads once ClickHouse holds every memory
    row the namespace committed (503 PROJECTION_BEHIND past the wait): a write answers at
    its commit, and the caller's own write is listed."""
    from server.core.ch import own_write_reads
    from server.memory.app.search import pg_validation
    from server.memory.store import pg_memory

    input = _cap_viewer_list_limit(input)
    await pg_validation.admit(input.org, [input.namespace], owed=pg_memory.MEMORY_ROW)
    with own_write_reads():
        out = await _handle_memory_list(input)
    _record_read_exposure("memory.list", input, [e.memory_key for e in out.entries])
    return out


def _caller_is_below_writer(org: str, namespace: str) -> bool:
    """True when the verified token's role on (org, namespace) is below
    writer. Scoped tokens answer from ns_roles; unscoped tokens from their flat
    roles (a grant that elevates a viewer is not consulted here — the cap is
    a page size, not an authorization decision, and a page is still
    readable)."""
    from server.operations.base import Role

    auth = current_auth()
    if auth is None:
        return False
    if auth.has_ns_roles():
        eff = auth.effective_role(org, namespace)
        return eff is None or eff.value < Role.WRITER.value
    return not auth.has_role(Role.WRITER)


def _cap_viewer_list_limit(input: Any) -> Any:
    """memory.list is the widest read a token can make; for a below-writer token one call
    returns at most LOOM_MEMORY_LIST_VIEWER_LIMIT rows. The caller pages with `offset`."""
    from server.settings import settings

    cap = int(settings.memory_list_viewer_limit)
    if cap <= 0 or int(getattr(input, "limit", 0) or 0) <= cap:
        return input
    if not _caller_is_below_writer(input.org, input.namespace):
        return input
    return input.model_copy(update={"limit": cap})


def _record_read_exposure(operation: str, input: Any, keys: list[str]) -> None:
    try:
        from server.memory.app.exposure_ledger import record_exposure

        record_exposure(
            operation=operation,
            org=input.org,
            namespace=input.namespace,
            agent=getattr(input, "agent", "") or "",
            user_id=getattr(input, "user_id", None),
            session_id=getattr(input, "session_id", None),
            run_id=getattr(input, "run_id", None),
            top_k=int(getattr(input, "limit", 0) or 0),
            keys=keys,
        )
    except Exception as e:  # noqa: BLE001 — telemetry never fails a read
        log.warning("exposure-ledger schedule failed (non-fatal): %s", e)


_list_counts_cache: dict[tuple[Any, ...], tuple[int, int]] = {}


def reset_list_counts_cache() -> None:
    """Tests."""
    _list_counts_cache.clear()


# Each status a listing hides unless the request asks for it, and the flag that asks.
_STATUS_FLAGS: tuple[
    tuple[str, Literal["include_duplicates", "include_superseded", "include_proposed"]], ...
] = (
    ("duplicate", "include_duplicates"),
    ("superseded", "include_superseded"),
    ("proposed", "include_proposed"),
)


async def _hidden_under_prefix(
    input: MemoryListInput,
    identity: Any,
    tag_filter: TagFilter | None,
    served: ServedFlags,
    total: int,
) -> dict[str, Any]:
    """How many rows under the asked prefix the status filter hid, when the answer is otherwise
    empty — a prefix listing is a lookup by name, and "0 keys" while the keys exist misleads a
    caller (a cleanup would conclude there is nothing to delete). A listing that shows keys
    pays no extra count."""
    import server.memory.store as memory_repo

    hides = [(s, f) for s, f in _STATUS_FLAGS if not served[f]]
    if total or not input.prefix or not hides or input.count_only:
        return {}
    every = await memory_repo.count_by_namespace(
        identity,
        memory_type=input.memory_type or None,
        key_prefix=input.prefix,
        source_trace=input.source_trace or None,
        # The same tag match as `total`, so only the status filter makes the difference.
        tag_filter=tag_filter,
        contains=input.contains or None,
        include_tombstoned=input.include_tombstoned,
        as_of=input.as_of,
        include_duplicates=True,
        include_superseded=True,
        include_proposed=True,
        scope=request_scope(),
    )
    if every <= total:
        return {}
    flags = " / ".join(f"{f}=true" for _, f in hides)
    return {
        "hidden": every - total,
        "hidden_hint": (
            f"{every - total} more key(s) under this prefix are {' or '.join(s for s, _ in hides)}"
            f" (merged into or replaced by another row); {flags} lists them."
        ),
    }


async def _handle_memory_list(input: MemoryListInput) -> MemoryListOutput:
    """List entries under an identity tuple.

    sort_by="score" orders by `evidence_count + recall_count*0.5` DESC with an
    optional per-memory_type `quotas` cap; otherwise newest-first. Both filter
    tombstoned rows. `truncated` flags that more rows would have returned.
    """
    import server.memory.store as memory_repo
    from server.operations import read_projection

    # The entry field whitelist (None = every field) and the hard tag filter, in any case
    # (memory.search matches tags exactly).
    keep = read_projection.resolve(
        _MemoryListEntry,
        list(input.fields or []),
        always=read_projection.LIST_ENTRY_ALWAYS,
    )
    tag_filter = list_tag_filter(input.tags, every=input.tags_every)

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    scope = request_scope()
    from server.memory.app.served_superseded import served_flags

    _served = served_flags(input)  # duplicates and superseded rows

    if input.sort_by == "score":
        return _project_entries(await _list_by_score(input, tag_filter, _served), keep)

    # The namespace's newest version is the change watermark. A poller that sends the one it
    # has learns "nothing moved" from this one small read instead of the page, the total and the
    # today-count under FINAL. As a decimal string: the value is above 2^53 and a JavaScript
    # client rounds a number that large (the watermark would never match).
    nsv = await memory_repo.namespace_version(input.org, input.namespace)
    namespace_version = str(nsv) if nsv else ""
    known = input.known_version
    if known is not None and namespace_version and str(known) == namespace_version:
        return MemoryListOutput(
            entries=[],
            truncated=False,
            namespace_version=namespace_version,
            unchanged=True,
        )

    # A caller that wants the number and not the page gets the two counts and no rows (asking
    # for `limit 1` instead would cost the whole page statement — ~4 MB for one integer).
    count_only = bool(input.count_only)
    rows: list[Any] = []
    if not count_only:
        # +1 to detect truncation.
        rows = await memory_repo.list_by_namespace(
            identity,
            memory_type=input.memory_type or None,
            key_prefix=input.prefix or None,
            source_trace=input.source_trace or None,
            tag_filter=tag_filter,
            contains=input.contains or None,
            order_by="created_at_desc",
            limit=input.limit + 1,
            offset=input.offset,
            include_tombstoned=input.include_tombstoned,
            as_of=input.as_of,
            preview=True,  # The stored 2 KB preview, not the value
            **_served,
            scope=scope,
        )
    truncated = len(rows) > input.limit
    rows = rows[: input.limit]
    rows = await _served_by_authority(input, rows, _served)
    if _wants_usage(keep):
        rows = await memory_repo.overlay_usage_totals_many(rows)
    # The two counts under FINAL are remembered per (scope, namespace version, UTC day) — a
    # changed page recomputes them once, not on every poll that saw a change.
    from datetime import UTC, datetime

    _today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    count_key = (
        scope,
        input.org,
        input.namespace,
        input.agent,
        input.user_id,
        input.session_id,
        input.memory_type or "",
        input.prefix or "",
        input.source_trace or "",
        tag_filter,
        input.contains or "",
        bool(input.include_tombstoned),
        str(input.as_of or ""),
        tuple(sorted(_served.items())),
        namespace_version,
        _today_start.date().isoformat(),
    )
    cached = _list_counts_cache.get(count_key)
    if cached is not None:
        total, today = cached
    else:
        total = await memory_repo.count_by_namespace(
            identity,
            memory_type=input.memory_type or None,
            key_prefix=input.prefix or None,
            source_trace=input.source_trace or None,
            tag_filter=tag_filter,
            contains=input.contains or None,
            include_tombstoned=input.include_tombstoned,
            as_of=input.as_of,
            **_served,
            scope=scope,
        )
        # "Today" = created since UTC midnight. CH stores naive-UTC DateTimes, so
        # pass a naive UTC boundary to match.
        today = await memory_repo.count_by_namespace(
            identity,
            memory_type=input.memory_type or None,
            key_prefix=input.prefix or None,
            source_trace=input.source_trace or None,
            tag_filter=tag_filter,
            contains=input.contains or None,
            include_tombstoned=input.include_tombstoned,
            created_since=_today_start,
            **_served,
            scope=scope,
        )
        if namespace_version:
            if len(_list_counts_cache) > 2048:
                _list_counts_cache.clear()
            _list_counts_cache[count_key] = (total, today)
    VALUE_PREVIEW_BYTES = 2048
    # The caller's preview length, at most the stored one.
    preview_chars = min(
        VALUE_PREVIEW_BYTES,
        int(input.preview_chars or VALUE_PREVIEW_BYTES),
    )
    return _project_entries(
        MemoryListOutput(
            total=total,
            today=today,
            namespace_version=namespace_version,
            **await _hidden_under_prefix(input, identity, tag_filter, _served, total),
            entries=[
                _MemoryListEntry(
                    memory_key=r.memory_key,
                    memory_type=r.memory_type,
                    confidence=r.confidence,
                    version=r.version,
                    created_at=r.created_at,
                    updated_at=r.updated_at or r.created_at,
                    expires_at=r.expires_at,
                    valid_at=r.valid_at,
                    tombstoned_at=r.tombstoned_at,
                    status=r.status or "active",
                    pinned=r.pinned,
                    verified_by=r.verified_by,
                    verification=_trust.verification_state(
                        r.verified_by,
                        r.verified_sha256,
                        _trust.row_content_sha256(r),
                    ),
                    evidence_count=r.evidence_count,
                    recall_count=r.recall_count,
                    last_recalled_at=r.last_recalled_at,
                    sensitivity=r.sensitivity,
                    tags=list(r.tags or []),
                    # A row labelled high after the extractor ran keeps entities mined from
                    # its value.
                    entities=[] if r.sensitivity == "high" else public_entities(r.entities),
                    agent=r.agent or "",
                    user_id=r.user_id or "",
                    session_id=r.session_id or "",
                    source_trace=r.source_trace,
                    value=pv,
                    value_truncated=pcut,
                    # A short secret's hash can be guessed; a high row's comes with the value.
                    content_sha256="" if r.sensitivity == "high" else _trust.row_content_sha256(r),
                    char_limit=r.char_limit,
                )
                for r, (pv, pcut) in ((r, _preview_for_list(r, preview_chars)) for r in rows)
            ],
            truncated=truncated,
        ),
        keep,
    )


async def _served_by_authority(
    input: MemoryListInput, rows: list[Any], served: ServedFlags
) -> list[Any]:
    """Under Postgres, only what the authority still serves at this version: a delete, a
    rewrite or a redaction not yet published leaves the page. A recovery listing shows a
    head the authority has since only deleted as that deleted head, within the listing's
    own filters."""
    import server.memory.store as memory_repo

    if not input.include_tombstoned:
        return await memory_repo.served_rows(rows)
    hidden = frozenset(status for status, flag in _STATUS_FLAGS if not served[flag])
    return await memory_repo.recovered_rows(
        rows, source_trace=input.source_trace or None, hidden_statuses=hidden, as_of=input.as_of
    )


async def _list_by_score(
    input: MemoryListInput, tag_filter: TagFilter | None, served: ServedFlags
) -> MemoryListOutput:
    """sort_by="score": `evidence_count + recall_count*0.5` DESC with the optional
    per-memory_type `quotas` cap; tags match in any case."""
    import server.memory.store as memory_repo

    params: dict[str, Any] = {
        "org": input.org,
        "ns": input.namespace,
        "lim": input.limit + 1,
    }
    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "tombstoned_at IS NULL",
        *scope_where(request_scope(), params),
    ]
    # `*` on an identity axis drops that axis from WHERE, the rule the recency path
    # (`memory_repo.list_by_namespace`) applies; comparing `agent = '*'` literally would return
    # 0 rows for every namespace-wide caller. The writer columns are selected so a wildcard
    # read can tell whose row each entry is, as the recency entries do.
    for axis, col, pname in (
        ("agent", "agent", "agent"),
        ("user_id", "user_id", "uid"),
        ("session_id", "session_id", "sid"),
    ):
        val = getattr(input, axis)
        if val != "*":
            where.append(f"{col} = {{{pname}:String}}")
            params[pname] = val
    if not served["include_duplicates"]:
        where.append("status != 'duplicate'")
    if not served["include_superseded"]:
        where.append("status != 'superseded'")
    if not served["include_proposed"]:
        where.append("status != 'proposed'")
    if input.memory_type:
        where.append("memory_type = {mt:String}")
        params["mt"] = input.memory_type
    if input.prefix:
        where.append("startsWith(memory_key, {p:String})")
        params["p"] = input.prefix
    if tag_filter is not None:
        where.append(tag_where(tag_filter, params))
    if input.contains:
        where.append(memory_repo.CONTAINS_PREDICATE)
        params["contains"] = input.contains
    if input.as_of is not None:
        where.append(
            "(valid_at IS NOT NULL AND valid_at <= {as_of:DateTime} "
            "OR valid_at IS NULL AND created_at <= {as_of:DateTime})"
        )
        params["as_of"] = input.as_of
    # Wider pool so per-type quota trimming has rows to choose from.
    if input.quotas:
        params["lim"] = sum(input.quotas.values()) * 4 + 1
    # Cap value preview at 2 KB so long documents-as-memories don't bloat
    # the response; callers needing full value round-trip via memory.get.
    sql = f"""
        SELECT
            memory_key, memory_type, confidence, version,
            created_at, expires_at, valid_at, pinned, status,
            evidence_count, recall_count, last_recalled_at,
            tags, entities, source_trace, sensitivity,
            verified_by, verified_sha256, content_sha256,
            agent, user_id, session_id, redacted_at,
            value_preview AS value,
            value_len > 2048 AS value_truncated
        FROM loom.memory FINAL
        WHERE {" AND ".join(where)}
        ORDER BY (evidence_count + recall_count * 0.5) DESC, created_at DESC
        LIMIT {{lim:UInt64}}
    """
    rows_dict = await get_client().query_all(sql, params=params)
    if input.quotas:
        per_type_counts: dict[str, int] = {}
        kept: list[dict[str, Any]] = []
        for r in rows_dict:
            t = r.get("memory_type", "")
            cap = int(input.quotas.get(t, 0))
            used = per_type_counts.get(t, 0)
            if cap > 0 and used >= cap:
                continue
            kept.append(r)
            per_type_counts[t] = used + 1
            if len(kept) >= input.limit + 1:
                break
        rows_dict = kept
    truncated = len(rows_dict) > input.limit
    rows_dict = await memory_repo.served_rows(
        [{**r, "org": input.org, "namespace": input.namespace} for r in rows_dict[: input.limit]]
    )
    # Total in scope (pre-limit, pre-quota) so callers can show "N of TOTAL".
    _total_row = await get_client().query_one(
        f"SELECT count() AS n FROM loom.memory FINAL WHERE {' AND '.join(where)}",
        params=params,
    )
    score_total = int(_total_row["n"]) if _total_row else 0
    return MemoryListOutput(
        total=score_total,
        entries=[_score_entry(r) for r in rows_dict],
        truncated=truncated,
    )


def _score_entry(r: dict[str, Any]) -> _MemoryListEntry:
    """One score-ordered list row. A 'high' row's value, hash and entities are readable by
    key only, through an audited read."""
    high = r.get("sensitivity") == "high"
    return _MemoryListEntry(
        memory_key=r["memory_key"],
        memory_type=r["memory_type"],
        confidence=float(r.get("confidence") or 0.0),
        version=int(r.get("version") or 0),
        created_at=r["created_at"],
        updated_at=r["created_at"],
        expires_at=r.get("expires_at"),
        valid_at=r.get("valid_at"),
        status=str(r.get("status") or "active"),
        pinned=bool(r.get("pinned") or False),
        evidence_count=int(r.get("evidence_count") or 1),
        recall_count=int(r.get("recall_count") or 0),
        last_recalled_at=r.get("last_recalled_at"),
        tags=list(r.get("tags") or []),
        entities=[] if high else public_entities(r.get("entities")),
        agent=r.get("agent") or "",
        user_id=r.get("user_id") or "",
        session_id=r.get("session_id") or "",
        source_trace=r.get("source_trace") or "",
        value=None if high else _decode_value(r.get("value")),
        value_truncated=high or bool(r.get("value_truncated")),
        verified_by=r.get("verified_by") or "",
        verification=_trust.row_verification(r),
        sensitivity=r.get("sensitivity") or "",
    )


def _project_entries(out: MemoryListOutput, keep: frozenset[str] | None) -> MemoryListOutput:
    """Stamp the resolved field whitelist on every entry so
    the wire carries only those keys (plus `memory_key` and `sensitivity`). None = untouched."""
    if keep is not None:
        for e in out.entries:
            e._projection = keep
    return out


# JSON field names are concatenated into SQL, so they must be safe identifiers.
_SAFE_IDENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
_AGGREGATE_FUNCS = frozenset({"count", "sum", "avg", "min", "max"})


def _assert_safe_ident(name: str, label: str) -> None:
    if not _SAFE_IDENT_RE.match(name or ""):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_IDENTIFIER",
                "message": f"{label}={name!r} is not a valid field name",
            },
        )


async def handle_memory_aggregate(
    input: MemoryAggregateInput,
) -> MemoryAggregateOutput:
    """Deterministic SQL count/sum/avg/min/max over loom.memory.value JSON
    fields — the predicate queries ranking-based memory.search can't answer.

    Safety: agg_func whitelist; field names pass a strict identifier regex;
    filter values bind as CH parameters; identity tuple ANDed into the query.
    Free-text rows fall out (JSONExtract returns 0/'') rather than erroring.
    """
    if input.agg_func not in _AGGREGATE_FUNCS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_AGG",
                "message": f"unsupported agg_func: {input.agg_func}",
            },
        )
    if input.agg_func != "count":
        _assert_safe_ident(input.agg_field, "agg_field")
    for gf in input.group_by:
        _assert_safe_ident(gf, "group_by")
    for k in input.filter:
        _assert_safe_ident(k, "filter key")

    # Read the native value_json column; dict-shaped writes populate it, but
    # scalar values still live in the String `value`, so fall back to
    # JSONExtract on value when value_json is empty/null.
    select_parts: list[str] = []
    group_aliases: list[str] = []

    def _json_string(field: str) -> str:
        return (
            f"coalesce("
            f"  nullIf(toString(value_json.{field}), ''), "
            f"  JSONExtractString(value, '{field}')"
            f")"
        )

    def _json_float(field: str) -> str:
        return (
            f"if(isNull(value_json.{field}), "
            f"  JSONExtractFloat(value, '{field}'), "
            f"  toFloat64(value_json.{field}))"
        )

    for gf in input.group_by:
        select_parts.append(f"{_json_string(gf)} AS {gf}_g")
        group_aliases.append(f"{gf}_g")

    if input.agg_func == "count":
        select_parts.append("count() AS metric")
    else:
        select_parts.append(f"{input.agg_func}({_json_float(input.agg_field)})::Float64 AS metric")

    select_clause = ", ".join(select_parts)
    group_clause = "GROUP BY " + ", ".join(group_aliases) if group_aliases else ""

    where = [
        "org = {org:String}",
        "namespace = {ns:String}",
        "agent = {agent:String}",
        "user_id = {uid:String}",
        "tombstoned_at IS NULL",
        # A high value is readable by key only: never a group key, and never a filter that
        # would let a caller test guesses against it by count.
        "sensitivity != 'high'",
    ]
    params: dict[str, object] = {
        "org": input.org,
        "ns": input.namespace,
        "agent": input.agent,
        "uid": input.user_id,
    }
    where.extend(scope_where(request_scope(), params))
    for i, (k, v) in enumerate(input.filter.items()):
        ph = f"f{i}"
        where.append(f"{_json_string(k)} = {{{ph}:String}}")
        params[ph] = v

    where_clause = " AND ".join(where)

    sql = (
        f"SELECT {select_clause} "
        f"FROM loom.memory FINAL "
        f"WHERE {where_clause} "
        f"{group_clause} "
        f"ORDER BY metric DESC "
        f"LIMIT {int(input.limit)}"
    )

    ch = get_client()
    # the caller picks the fields, groups and filters: the caller-shaped ceilings apply
    with caller_shaped():
        rows = await ch.query_all(sql, params=params)

    out_rows: list[_AggregateRow] = []
    for r in rows:
        group = {gf: str(r.get(f"{gf}_g", "")) for gf in input.group_by}
        metric_raw = r.get("metric", 0)
        out_rows.append(
            _AggregateRow(
                group=group,
                metric=float(metric_raw),
            )
        )

    return MemoryAggregateOutput(rows=out_rows, sql_executed=sql)


async def follow_duplicate(input: Any, row: Any) -> tuple[Any, str | None]:
    """`memory.get(follow=true)`: the live canonical a `status='duplicate'` row points to, read
    by `duplicate_of` in this org and namespace — search's substitution rule on a point read —
    following a chain up to three hops. Returns `(row, resolved_from)`: the canonical and the
    key asked for, or the exact row and None when the row is not a duplicate or its canonical
    is gone, tombstoned, expired, in another namespace or not live (the orphan case
    `memory.resolve_orphan_duplicates` repairs)."""

    import server.memory.store as memory_repo

    cur = row
    for _ in range(3):
        if str(getattr(cur, "status", "") or "") != "duplicate":
            break
        target = str(getattr(cur, "duplicate_of", "") or "")
        if not target:
            break
        canon = await memory_repo.get_by_id(
            target,
            org=input.org,
            namespace=input.namespace,
            as_of=input.as_of,
            include_usage_totals=True,
            fresh=True,
            scope=request_scope(),
        )
        if canon is None or getattr(canon, "tombstoned_at", None) is not None:
            break
        exp = getattr(canon, "expires_at", None)
        if exp is not None and exp.replace(tzinfo=None) <= naive_utc_now():
            break
        cur = canon
    if cur is row or str(getattr(cur, "status", "") or "") not in ("active", "conflicted"):
        return row, None
    return cur, str(input.memory_key)


async def _canonical_keys_for_rows(org: str, namespace: str, rows: list[Any]) -> dict[str, str]:
    """Memory_key → canonical key for the `duplicate` rows of a read ("" when the canonical is
    gone); one keyed read, only when a duplicate is among them. Fail-open: the rows still
    return."""
    dups = {
        r.memory_key: str(getattr(r, "duplicate_of", "") or "")
        for r in rows
        if str(getattr(r, "status", "") or "") == "duplicate"
    }
    if not dups:
        return {}
    from server.memory.app.served_duplicates import canonical_keys_for

    try:
        by_id = await canonical_keys_for(org, namespace, list(dups.values()))
    except Exception as e:  # noqa: BLE001 — rows return without canonicals
        log.warning("memory.get: canonical lookup failed: %s", e)
        by_id = {}
    return {k: by_id.get(cid, "") for k, cid in dups.items()}


async def handle_memory_bulk_get(
    input: MemoryBulkGetInput,
) -> MemoryBulkGetOutput:
    """Fetch many memory rows by key, each answered as memory.get answers it, except that a
    row past `expires_at` is served until the expiry sweep, as memory.list serves it (a
    'high' one is not).

    Excludes tombstoned rows from `found`. Missing keys land in `missing`."""
    import server.memory.store as memory_repo
    from server.operations import read_projection

    # One whitelist for every entry; an unknown name is a 422
    # before the read. None = every field.
    keep = read_projection.resolve(
        MemoryGetOutput,
        list(input.fields or []),
        always=read_projection.GET_ALWAYS,
    )
    if not input.memory_keys:
        return MemoryBulkGetOutput(results={}, missing=[])

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    # A `chunk:memory.set:<key>:<n>` id (a memory.search chunk hit) names its parent fact in
    # the id; a bulk reader that got chunk ids back from a search reads the parent rows here
    # instead of a miss, when no row is stored under the id itself. The parent's row answers
    # under every id that asked for it, with `canonical_key` naming the parent (so the caller
    # can fold) and `resolved_from` the id.
    from server.memory.app.handlers import chunk_parent_key

    parent_of: dict[str, str] = {}
    for k in input.memory_keys:
        parent = chunk_parent_key(k)
        if parent:
            parent_of[k] = parent
    # The literal keys too: `memory.set` takes a key shaped like a chunk id, and its own row
    # answers before the parent (the alias below fills only a real miss).
    wanted = list(dict.fromkeys([*input.memory_keys, *parent_of.values()]))
    # Like memory.list, a bulk read serves a row past `expires_at` until the expiry sweep
    # tombstones it (memory.get does not), so the export lists and reads the same rows. The
    # read leaves out a 'high' row past its retention window: the window is why it has one.
    rows = await memory_repo.get_many_by_keys(identity, wanted, scope=request_scope())
    if _wants_usage(keep):
        rows = await memory_repo.overlay_usage_totals_many(rows)
    canonical = await _canonical_keys_for_rows(input.org, input.namespace, rows)
    successors = await shown_successors(input.org, input.namespace, rows)
    # Same answer as memory.get, so a bulk reader (the OKF exporter, the dashboard preload)
    # sees identity, lifecycle, sensitivity and attestation without N gets. The conflict
    # verdict and code links cost a read per row and are left to memory.get.
    fetched: dict[str, tuple[MemoryRow, MemoryGetOutput]] = {}
    for r in rows:
        canonical_key = canonical.get(r.memory_key)
        out = row_output(
            r,
            sha=_trust.row_content_sha256(r),
            canonical_key=canonical_key,
            duplicate_of=duplicate_of_shown(r, canonical_key),
            superseded_by=successors.get(str(r.memory_id), ""),
        )
        out._projection = keep
        fetched[r.memory_key] = (r, out)
    # Only the keys asked are served (and audited): a parent read to fill a chunk-id alias
    # answers under that alias, never under its own key unless that was asked too.
    results: dict[str, MemoryGetOutput] = {}
    answered: dict[str, MemoryRow] = {}
    for k in input.memory_keys:
        if k in fetched:
            row, served = fetched[k]
            results[k] = served
        elif parent_of.get(k) in fetched:
            row, parent_out = fetched[parent_of[k]]
            # `resolved_from` marks an answer given by another row, as memory.get marks one
            alias = parent_out.model_copy(
                update={
                    "canonical_key": parent_out.canonical_key or parent_of[k],
                    "resolved_from": k,
                }
            )
            results[k] = alias
        else:
            continue
        answered[str(row.memory_id)] = row
    high = [
        {"memory_key": r.memory_key, "memory_id": mid}
        for mid, r in answered.items()
        if r.sensitivity == "high"
    ]
    # A key read of a 'high' row is audited, as memory.get audits it.
    if high:
        note_budget_shape("audited")
    await _audit.emit_many(
        "memory.sensitive_read",
        org=input.org,
        namespace=input.namespace,
        actor=writer_actor(input.agent),
        subjects=high,
    )
    # the rows served, by their own keys: a chunk id answers with its parent's row
    _record_read_exposure(
        "memory.bulk_get", input, list(dict.fromkeys(r.memory_key for r in answered.values()))
    )
    missing = [k for k in input.memory_keys if k not in results]
    return MemoryBulkGetOutput(results=results, missing=missing)


async def handle_memory_statuses(input: MemoryStatusesInput) -> MemoryStatusesOutput:
    """memory.statuses: each id's current status in the namespace, inside the request's
    read scope. An id that is not a UUID, or that the namespace does not hold, is left
    out, as one the caller may not read is."""
    import server.memory.store as memory_repo

    ids: list[str] = []
    for raw in input.memory_ids:
        try:
            ids.append(str(uuid.UUID(raw)))
        except ValueError:
            continue
    ids = list(dict.fromkeys(ids))  # an id asked twice is read once
    if not ids:
        return MemoryStatusesOutput()
    rows = await memory_repo.statuses_by_ids(
        input.org, input.namespace, ids, scope=request_scope(), ch=get_client()
    )
    return MemoryStatusesOutput(rows=[MemoryStatus(**r) for r in rows])


async def handle_memory_keys(input: MemoryKeysInput) -> MemoryKeysOutput:
    """Every live row's identity and key in one read: a set an exporter can check its paged
    listing against, since a row that moves while pages are read can be skipped."""
    import server.memory.store as memory_repo

    rows = await memory_repo.live_keys(
        input.org, input.namespace, scope=request_scope(), limit=input.limit
    )
    keys = [MemoryKeyRef(**r) for r in rows[: input.limit]]
    _record_read_exposure("memory.keys", input, list(dict.fromkeys(k.memory_key for k in keys)))
    return MemoryKeysOutput(keys=keys, truncated=len(rows) > input.limit)


async def memory_tally(org: str, namespace: str) -> tuple[int, datetime | None]:
    """How many live memory rows the namespace holds inside the request's read scope,
    and the newest write."""
    import server.memory.store as memory_repo

    return await memory_repo.live_tally(org, namespace, scope=request_scope())


async def document_decision_tally(
    org: str, namespace: str, source_uris: list[str]
) -> list[dict[str, Any]]:
    """The live decision rows citing a document of the namespace, inside the request's
    read scope, per document of `source_uris` and '' for the rest."""
    from server.memory.store import document_decisions

    return await document_decisions.decisions_by_document(
        org, namespace, source_uris, scope=request_scope()
    )


async def identity_tally(
    org: str, namespace: str, column: IdentityColumn, *, limit: int
) -> tuple[int, list[tuple[str, int]]]:
    """Distinct agents, actors or users on live rows inside the request's read scope,
    and the `limit` with the most rows."""
    import server.memory.store as memory_repo

    return await memory_repo.identity_tally(
        org, namespace, column, scope=request_scope(), limit=limit
    )
