"""Read-side memory query handlers — list / aggregate / bulk_get."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import HTTPException

from server.auth.context import current_auth
from server.core.ch import caller_shaped, get_client
from server.core.datetimes import naive_utc_now
from server.core.hashing import hash_text
from server.core.hashing import sha256_value_json as _sha256_value_json
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_where
from server.memory.api.models_bulk import MemoryBulkGetInput, MemoryBulkGetOutput
from server.memory.api.models_crud import (
    MemoryGetOutput,
    MemoryListInput,
    MemoryListOutput,
    _MemoryListEntry,
)
from server.memory.api.models_search import (
    MemoryAggregateInput,
    MemoryAggregateOutput,
    _AggregateRow,
)
from server.memory.app.served_superseded import ServedFlags, shown_successors
from server.memory.domain import trust as _trust
from server.memory.domain.entity_sentinels import public_entities

log = logging.getLogger(__name__)


def _wants_usage(keep: frozenset[str] | None) -> bool:
    """Whether a projection serves a usage counter, which costs one more statement."""
    return keep is None or bool(keep & {"evidence_count", "recall_count", "last_recalled_at"})


def _row_content_sha256(row: Any) -> str:
    """Canonical hash of a MemoryRow's value; '' when there is no value.

    The stored `content_sha256` wins when set (a list row carries the 2 KB preview, not the
    value, so hashing it would read a verified row as stale); a whole, un-truncated value hashes
    exactly; a truncated one without a stored hash cannot be confirmed."""
    stored = hash_text(getattr(row, "content_sha256", None))
    if _trust.hash_is_set(stored):
        return stored
    if getattr(row, "value", None) is None or getattr(row, "value_truncated", False):
        return ""
    return _sha256_value_json(json.dumps(row.value, default=str))


def _list_verification(r: dict[str, Any]) -> str:
    """Verification state for a fast-list row.

    The list query selects a 2 KB value preview, so the live hash comes from
    the stored `content_sha256` column when populated; a preview that was not
    truncated hashes exactly; anything else cannot be confirmed and derives
    as `stale` (an attestation that cannot be confirmed confers nothing).
    """
    stored = hash_text(r.get("content_sha256"))
    if _trust.hash_is_set(stored):
        current = stored
    elif not r.get("value_truncated") and r.get("value") is not None:
        current = _sha256_value_json(str(r.get("value")))
    else:
        current = ""
    return _trust.verification_state(
        r.get("verified_by") or "", r.get("verified_sha256") or "", current
    )


def _preview_for_list(row: Any, max_chars: int) -> tuple[Any, bool]:
    """`(value, value_truncated)` for one list row —
    the value cut to `max_chars`, and whether the row holds more than was
    sent (the stored preview was already cut at 2,048, or this cut did it)."""
    raw = row.value
    value = _truncate_value_for_list(raw, max_chars)
    stored_cut = bool(getattr(row, "value_truncated", False))
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
    widest read a token can make."""
    from server.core.ch import own_write_reads

    input = _cap_viewer_list_limit(input)
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


async def _hidden_under_prefix(
    input: MemoryListInput,
    identity: Any,
    tags: list[str] | None,
    served: ServedFlags,
    total: int,
) -> dict[str, Any]:
    """How many rows under the asked prefix the status filter hid, when the answer is otherwise
    empty — a prefix listing is a lookup by name, and "0 keys" while the keys exist misleads a
    caller (a cleanup would conclude there is nothing to delete). A listing that shows keys
    pays no extra count."""
    import server.memory.store as memory_repo

    pairs = (
        ("duplicate", "include_duplicates"),
        ("superseded", "include_superseded"),
        ("proposed", "include_proposed"),
    )
    hides = [(s, f) for s, f in pairs if not served[f]]  # type: ignore[literal-required]
    if total or not input.prefix or not hides or input.count_only:
        return {}
    every = await memory_repo.count_by_namespace(
        identity,
        memory_type=input.memory_type or None,
        key_prefix=input.prefix,
        source_trace=input.source_trace or None,
        tags=tags,
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

    # The entry field whitelist (None = every field) and the hard OR tag filter, the predicate
    # memory.search applies.
    keep = read_projection.resolve(
        _MemoryListEntry,
        list(input.fields or []),
        always=read_projection.LIST_ENTRY_ALWAYS,
    )
    tags = [str(t) for t in (input.tags or []) if t] or None

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
        return _project_entries(await _list_by_score(input, tags, _served), keep)

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
            tags=tags,
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
        tuple(tags or ()),
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
            tags=tags,
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
            tags=tags,
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
            **await _hidden_under_prefix(input, identity, tags, _served, total),
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
                    status=str(getattr(r, "status", "") or "active"),
                    pinned=r.pinned,
                    verified_by=r.verified_by,
                    verification=_trust.verification_state(
                        r.verified_by,
                        r.verified_sha256,
                        _row_content_sha256(r),
                    ),
                    evidence_count=r.evidence_count,
                    recall_count=r.recall_count,
                    last_recalled_at=r.last_recalled_at,
                    tags=list(r.tags or []),
                    entities=public_entities(r.entities),
                    agent=r.agent or "",
                    user_id=r.user_id or "",
                    session_id=r.session_id or "",
                    source_trace=getattr(r, "source_trace", "") or "",
                    value=pv,
                    value_truncated=pcut,
                    content_sha256=_row_content_sha256(r),
                    char_limit=getattr(r, "char_limit", None),
                )
                for r, (pv, pcut) in ((r, _preview_for_list(r, preview_chars)) for r in rows)
            ],
            truncated=truncated,
        ),
        keep,
    )


async def _list_by_score(
    input: MemoryListInput, tags: list[str] | None, served: ServedFlags
) -> MemoryListOutput:
    """sort_by="score": `evidence_count + recall_count*0.5` DESC with the optional
    per-memory_type `quotas` cap (the tags predicate is memory.search's)."""
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
    if tags:
        where.append("hasAny(tags, {tags:Array(String)})")
        params["tags"] = tags
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
            tags, entities, source_trace,
            verified_by, verified_sha256, content_sha256,
            agent, user_id, session_id,
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
    rows_dict = rows_dict[: input.limit]
    # Total in scope (pre-limit, pre-quota) so callers can show "N of TOTAL".
    _total_row = await get_client().query_one(
        f"SELECT count() AS n FROM loom.memory FINAL WHERE {' AND '.join(where)}",
        params=params,
    )
    score_total = int(_total_row["n"]) if _total_row else 0
    return MemoryListOutput(
        total=score_total,
        entries=[
            _MemoryListEntry(
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
                entities=public_entities(r.get("entities")),
                agent=r.get("agent") or "",
                user_id=r.get("user_id") or "",
                session_id=r.get("session_id") or "",
                source_trace=r.get("source_trace") or "",
                value=_decode_value(r.get("value")),
                verified_by=r.get("verified_by") or "",
                verification=_list_verification(r),
            )
            for r in rows_dict
        ],
        truncated=truncated,
    )


def _project_entries(out: MemoryListOutput, keep: frozenset[str] | None) -> MemoryListOutput:
    """Stamp the resolved field whitelist on every entry so
    the wire carries only those keys (plus `memory_key`). None = untouched."""
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
            as_of=input.as_of,
            include_usage_totals=True,
            fresh=True,
            scope=request_scope(),
        )
        if canon is None or getattr(canon, "tombstoned_at", None) is not None:
            break
        if str(getattr(canon, "namespace", "") or "") != str(input.namespace):
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
    out = await _handle_memory_bulk_get(input)
    _record_read_exposure("memory.bulk_get", input, list(out.results.keys()))
    return out


async def _handle_memory_bulk_get(
    input: MemoryBulkGetInput,
) -> MemoryBulkGetOutput:
    """Fetch many memory rows in a single CH query.

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
    # instead of a miss. The parent's row is answered under every id that asked for it, with
    # `canonical_key` naming the parent, so the caller can fold.
    from server.memory.app.get_output import duplicate_of_shown, row_output
    from server.memory.app.handlers import chunk_parent_key
    from server.observability import audit as _audit

    parent_of: dict[str, str] = {}
    for k in input.memory_keys:
        parent = chunk_parent_key(k)
        if parent:
            parent_of[k] = parent
    wanted = list(dict.fromkeys([parent_of.get(k, k) for k in input.memory_keys]))
    rows = await memory_repo.get_many_by_keys(identity, wanted, scope=request_scope())
    if _wants_usage(keep):
        rows = await memory_repo.overlay_usage_totals_many(rows)
    canonical = await _canonical_keys_for_rows(input.org, input.namespace, rows)
    successors = await shown_successors(input.org, input.namespace, rows)
    # Same answer as memory.get, so a bulk reader (the OKF exporter, the dashboard preload)
    # sees identity, lifecycle, sensitivity and attestation without N gets. The conflict
    # verdict and code links cost a read per row and are left to memory.get.
    results: dict[str, MemoryGetOutput] = {}
    for r in rows:
        canonical_key = canonical.get(r.memory_key)
        results[r.memory_key] = row_output(
            r,
            sha=_row_content_sha256(r),
            canonical_key=canonical_key,
            duplicate_of=duplicate_of_shown(r, canonical_key),
            superseded_by=successors.get(str(r.memory_id), ""),
        )
        if getattr(r, "sensitivity", "") == "high":
            # A key read of a 'high' row is audited, as memory.get audits it.
            await _audit.emit(
                "memory.sensitive_read",
                org=input.org,
                namespace=input.namespace,
                actor=input.agent,
                subject={"memory_key": r.memory_key, "memory_id": str(r.memory_id)},
            )
        results[r.memory_key]._projection = keep
    for chunk_id, parent in parent_of.items():
        if parent in results and chunk_id not in results:
            parent_row = results[parent]
            canonical_key = parent_row.canonical_key or parent
            row = parent_row.model_copy(update={"canonical_key": canonical_key})
            row._projection = keep
            results[chunk_id] = row
    missing = [k for k in input.memory_keys if k not in results]
    return MemoryBulkGetOutput(results=results, missing=missing)
