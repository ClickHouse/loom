"""memory.apply — a wide write names its scope, its limit and its reason,
and previews by default.

The rule is about the target, not the verb: every wide write addresses
`ids | filter | search | all`, a filter or search target must carry a numeric
`limit`, and `all` is refused without a dry run or an explicit confirmation.
`memory.clear` demands a confirm subject, `memory.consolidate` a limit and a dry
run, `memory.delete` takes one key; none of them covers "forget everything
tagged q3-migration" or "lock every runbook I wrote last week".

`memory.apply` is that one grammar over loom's existing write paths:

    target   keys | filter | search   (exactly one; `all` is memory.clear)
    limit    required for filter and search; keys are their own limit
    action   delete | expire | lock | label | demote
    dry_run  True by default — the enforcing call says dry_run=false
    reason   required; carried where a column exists (lock_reason,
             expire_reason) and into the trace for every row

Resolution is one primary-key-pinned read (org, namespace, agent[, user,
session]) with the filters and `LIMIT n + 1` so truncation is reported,
or one search call, over-fetched per key, read up to one past `limit` of
the caller's rows. Execution goes through the same
handlers a single-row call uses — delete, touch, lock — so every guard
(locks, policy, blacklist) fires per row; a 423 becomes a `refused` row,
never a failed batch. Label and demote are the two writes loom had no
single-row op for; they are version rows here, mirrored onto the
embeddings table the way touch mirrors expiry. Never an ALTER UPDATE on
loom.memory.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from server.core.datetimes import naive_utc_now
from server.core.datetimes import to_naive_utc as _naive
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.api.models_apply import (
    ApplyDemote,
    ApplyExpire,
    ApplyLabel,
    ApplyLock,
    ApplyRow,
    ApplyTarget,
)
from server.memory.api.models_crud import MemoryDeleteInput, MemoryTouchInput
from server.memory.api.models_lock import MemoryLockInput
from server.memory.api.models_search import MemorySearchInput
from server.memory.domain import lock as _lock
from server.memory.domain.search_types import hit_row_key, names_row
from server.memory.store.inserts import insert_memory_versions

log = logging.getLogger(__name__)


# Which lock class each action is (label is metadata: no lock gates it).
_LOCK_CLASS: dict[str, str | None] = {
    "delete": "delete",
    "expire": "expire",
    "lock": None,
    "label": None,
    "demote": "value",
}


def _ttl_from(ttl_seconds: int | None, until: datetime | None) -> int:
    if ttl_seconds is not None:
        return int(ttl_seconds)
    assert until is not None
    secs = int((_naive(until) - naive_utc_now()).total_seconds())
    if secs < 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "VALIDATION", "message": "`until` must be in the future"},
        )
    return secs


# ---------------------------------------------------------------- resolve


async def resolve(input: Any) -> tuple[list[Any], bool]:
    """Return (rows, truncated). One PK-pinned read for keys/filter; one
    search call for search. Rows are hydrated MemoryRow objects."""
    import server.memory.store as memory_repo

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    target: ApplyTarget = input.target
    if target.keys:
        rows = []
        for key in dict.fromkeys(target.keys):  # de-dup, keep order
            row = await memory_repo.get_by_key(
                identity, key, fresh=True, strong=True, scope=WHOLE_NAMESPACE
            )
            if row is not None:
                rows.append(row)
        return rows, False
    limit = int(input.limit)
    if target.filter:
        f = target.filter
        where = [
            "org = {org:String}",
            "namespace = {ns:String}",
            "agent = {agent:String}",
            "user_id = {uid:String}",
            "session_id = {sid:String}",
            "tombstoned_at IS NULL",
        ]
        params: dict[str, Any] = {
            "org": input.org,
            "ns": input.namespace,
            "agent": input.agent,
            "uid": input.user_id,
            "sid": input.session_id,
            "lim": limit + 1,
        }
        if f.memory_type:
            where.append("memory_type = {mt:String}")
            params["mt"] = f.memory_type
        if f.tags_any:
            where.append("hasAny(tags, {tags_any:Array(String)})")
            params["tags_any"] = list(f.tags_any)
        if f.tags_all:
            where.append("hasAll(tags, {tags_all:Array(String)})")
            params["tags_all"] = list(f.tags_all)
        if f.key_prefix:
            where.append("startsWith(memory_key, {kp:String})")
            params["kp"] = f.key_prefix
        if f.created_before is not None:
            where.append("created_at < {cb:DateTime}")
            params["cb"] = _naive(f.created_before)
        if f.created_after is not None:
            where.append("created_at >= {ca:DateTime}")
            params["ca"] = _naive(f.created_after)
        if f.confidence_lte is not None:
            where.append("confidence <= {cl:Float32}")
            params["cl"] = float(f.confidence_lte)
        if f.source_trace:
            where.append("source_trace = {st:String}")
            params["st"] = f.source_trace
        from server.core.ch import get_client

        ch = get_client()
        raw = await ch.query_all(
            memory_repo._MEMORY_FINAL_PROJECTION
            + "\n        WHERE "
            + "\n          AND ".join(where)
            + "\n        ORDER BY created_at DESC\n        LIMIT {lim:UInt32}",
            params=params,
        )
        rows = [memory_repo._hydrate(r) for r in raw]
        truncated = len(rows) > limit
        return rows[:limit], truncated
    # search
    from server.memory.app.search import handle_memory_search
    from server.memory.app.search.scoring import per_key_fetch

    assert target.search is not None
    # No `session_id`: a session filter would leave out a long value's set chunks. One row
    # past `limit` says `truncated`.
    top_k = per_key_fetch(limit + 1)
    out = await handle_memory_search(
        MemorySearchInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent,
            user_id=input.user_id,
            query=target.search.query,
            top_k=top_k,
            namespace_scope="this",
        )
    )
    rows = []
    seen: set[str] = set()
    mine = (input.agent, input.user_id, input.session_id)
    for hit in out.results:
        if target.search.min_score is not None and hit.similarity < target.search.min_score:
            continue
        key = hit.memory_key
        if key in seen:
            continue
        # act only on the caller's own row; a hit that names none (a record) reads its key
        # under the caller's identity
        owner = hit_row_key(hit, input.namespace)
        if names_row(hit) and (owner.agent, owner.user_id, owner.session_id) != mine:
            continue
        seen.add(key)
        row = await memory_repo.get_by_key(
            identity, key, fresh=True, strong=True, scope=WHOLE_NAMESPACE
        )
        if row is not None:
            rows.append(row)
            if len(rows) > limit:  # one row past `limit`: more matched
                return rows[:limit], True
    # a full window, as large as the search served it, may hold back rows of the caller's
    window = out.top_k_ceiling or top_k
    return rows, len(out.results) >= window


# ---------------------------------------------------------------- execute


def _clamp(v: float, floor: float) -> float:
    return round(max(floor, min(1.0, v)), 4)


async def _label(row: Any, spec: ApplyLabel, *, reason: str) -> int:
    import server.memory.store as memory_repo
    from server.core.ch import get_client

    current = list(getattr(row, "tags", None) or [])
    if spec.mode == "replace":
        new = list(dict.fromkeys(spec.tags))
    elif spec.mode == "remove":
        drop = set(spec.tags)
        new = [t for t in current if t not in drop]
    else:
        new = list(dict.fromkeys(current + [t for t in spec.tags if t not in current]))
    if new == current:
        return 0
    version = memory_repo.next_version()
    vrow = memory_repo.build_memory_version_row(
        row, version=version, tags=new, updated_at=memory_repo.written_now()
    )
    await insert_memory_versions([vrow], ch=get_client())
    await _mirror(row, tags=new)
    return version


async def _demote(row: Any, spec: ApplyDemote, *, reason: str) -> int:
    import server.memory.store as memory_repo
    from server.core.ch import get_client

    conf = float(getattr(row, "confidence", 1.0) or 0.0)
    if spec.confidence_delta is not None:
        new_conf = _clamp(conf + spec.confidence_delta, spec.floor)
    else:
        new_conf = _clamp(conf * float(spec.confidence_factor or 1.0), spec.floor)
    if abs(new_conf - conf) < 1e-6:
        return 0
    version = memory_repo.next_version()
    vrow = memory_repo.build_memory_version_row(
        row, version=version, confidence=new_conf, updated_at=memory_repo.written_now()
    )
    await insert_memory_versions([vrow], ch=get_client())
    await _mirror(row, confidence=new_conf)
    return version


async def _mirror(row: Any, **overrides: Any) -> None:
    """Keep the embeddings rows (what search reads) in step: a versioned
    re-emit of the key's live rows with the changed columns.
    Never a lightweight UPDATE — its patch part would switch the fact leg's
    codes-first read off for every search. Non-fatal: the
    loom.memory version row already landed; a stray embedding is the known,
    tolerated failure mode."""
    import server.memory.store as memory_repo

    if row.sensitivity == "high":  # a 'high' head keeps no vector
        return
    try:
        await memory_repo.reemit_embedding_rows_for_key(
            row.org,
            row.namespace,
            row.agent,
            row.user_id,
            row.session_id,
            row.memory_key,
            **overrides,
        )
    except Exception as e:  # noqa: BLE001 — the loom.memory row already landed
        log.warning("memory.apply: embeddings mirror failed for %s: %s", row.memory_key, e)


async def apply_one(row: Any, input: Any) -> ApplyRow:
    """Apply `input.action` to one resolved row through the single-row
    handlers, translating a refusal into a row status."""
    from server.memory.app.handlers import handle_memory_delete, handle_memory_touch
    from server.memory.app.lock import handle_memory_lock

    base = {
        "memory_key": row.memory_key,
        "memory_id": str(row.memory_id),
        "agent": row.agent,
        "user_id": row.user_id,
        "session_id": row.session_id,
    }
    ident = {
        "org": row.org,
        "namespace": row.namespace,
        "agent": row.agent,
        "user_id": row.user_id,
        "session_id": row.session_id,
    }
    lock_class = _LOCK_CLASS[input.action]
    mode = _lock.effective_mode(row)
    if lock_class and mode and lock_class in _lock.REFUSES[mode]:
        return ApplyRow(
            **base,
            status="refused",
            detail=f"locked {mode}: {row.lock_reason or 'no reason recorded'}",
        )
    if input.dry_run:
        return ApplyRow(**base, status="would_apply")
    try:
        if input.action == "delete":
            out = await handle_memory_delete(MemoryDeleteInput(**ident, memory_key=row.memory_key))
            return ApplyRow(
                **base, status="applied" if out.tombstoned else "noop", version=out.version
            )
        if input.action == "expire":
            spec: ApplyExpire = input.expire
            out_t = await handle_memory_touch(
                MemoryTouchInput(
                    **ident,
                    memory_key=row.memory_key,
                    ttl_seconds=_ttl_from(spec.ttl_seconds, spec.until),
                    on_expire=spec.on_expire,
                    expire_reason=input.reason,
                )
            )
            return ApplyRow(
                **base, status="applied" if out_t.touched else "noop", version=out_t.version
            )
        if input.action == "lock":
            spec_l: ApplyLock = input.lock
            out_l = await handle_memory_lock(
                MemoryLockInput(
                    **ident,
                    memory_key=row.memory_key,
                    mode=spec_l.mode,
                    reason=input.reason,
                    until=spec_l.until,
                    ttl_seconds=spec_l.ttl_seconds,
                )
            )
            return ApplyRow(
                **base, status="applied" if out_l.locked else "noop", version=out_l.version
            )
        if input.action == "label":
            v = await _label(row, input.label, reason=input.reason)
            return ApplyRow(**base, status="applied" if v else "noop", version=v or None)
        v = await _demote(row, input.demote, reason=input.reason)
        return ApplyRow(**base, status="applied" if v else "noop", version=v or None)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
        code = str(detail.get("code", e.status_code))
        if e.status_code in (403, 409, 422, 423):
            return ApplyRow(
                **base, status="refused", detail=f"{code}: {detail.get('message', '')}"[:300]
            )
        return ApplyRow(
            **base, status="failed", detail=f"{code}: {detail.get('message', '')}"[:300]
        )
    except Exception as e:  # reported on this row's ApplyRow; the batch continues
        log.exception("memory.apply: %s failed for %s", input.action, row.memory_key)
        return ApplyRow(**base, status="failed", detail=f"{type(e).__name__}: {e}"[:300])
