"""Blacklist handlers — add / list / remove."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from server.core.datetimes import naive_utc_now_seconds
from server.memory.api.models_blacklist import (
    BlacklistAddInput,
    BlacklistAddOutput,
    BlacklistListInput,
    BlacklistListOutput,
    BlacklistRemoveInput,
    BlacklistRemoveOutput,
    BlacklistSweepInput,
    BlacklistSweepOutput,
    _BlacklistRow,
)
from server.memory.app.blacklist_guard import (
    REGEX_PROGRAM_PER_SCOPE,
    _bump_version,
    compile_regex,
    invalidate_cache,
    scope_program,
)
from server.memory.store import blacklist as blacklist_repo

if TYPE_CHECKING:
    from server.operations.base import Role


def _scope_args(scope: str, org: str, namespace: str) -> tuple[str, str]:
    """Encode scope as the (org, namespace) pair the cache matches via `_applies_to`."""
    if scope == "global":
        return ("*", "*")
    if scope == "org":
        return (org, "*")
    return (org, namespace)


def required_role_for_scope(scope: str) -> Role:
    """The role a principal needs to write or remove a pattern of `scope`.

    A pattern's reach is its scope: a namespace pattern refuses one
    namespace's writes, an org pattern every namespace of the org, a global
    pattern every tenant's. Authority follows reach:
    namespace → the op's own gate (WRITER to add, ADMIN to remove, both on
    that namespace); org → ORG_ADMIN; global → PLATFORM_ADMIN, the
    deployment-wide role, so the admin of one namespace in one org cannot write a global
    pattern.
    """
    from server.operations.base import Role  # handlers import lazily: operations import us

    if scope == "global":
        return Role.PLATFORM_ADMIN
    if scope == "org":
        return Role.ORG_ADMIN
    return Role.WRITER


def _require_scope_authority(scope: str, verb: str, row_org: str | None = None) -> None:
    """Raise AUTH_INSUFFICIENT unless the current principal holds the role
    `scope` demands. For an org-scoped row the principal's org must also be
    the row's org (or `*`): an ORG_ADMIN of one org is nothing in another.
    No-op when auth is off (auth is None: nothing is enforced anywhere)."""
    if scope == "namespace":
        return
    from server.auth.authz import may_act_in_org
    from server.auth.context import current_auth
    from server.core.errors import AuthInsufficientError

    auth = current_auth()
    if auth is None:
        return
    needed = required_role_for_scope(scope)
    if not auth.has_role(needed):
        raise AuthInsufficientError(
            f"blacklist.{verb} scope={scope!r} requires role {needed.name}; "
            f"principal has {[r.name for r in auth.roles]} "
            "(namespace-scoped patterns need only the op's own role)"
        )
    if scope == "org" and row_org is not None and not may_act_in_org(auth, row_org):
        raise AuthInsufficientError(
            f"blacklist.{verb}: principal {auth.subject!r} (org {auth.org!r}) "
            f"cannot act on an org-scoped pattern of org {row_org!r}"
        )


def _stamped_author(body_value: str) -> str:
    """Who wrote the row: the writing principal as `writer_actor` stamps
    every memory row (`human:` / `process:` + subject). The body's value
    counts only with no principal and no pipeline stage to stamp."""
    from server.memory.app.provenance import writer_actor

    return writer_actor(fallback=body_value)


async def _reject_bad_regex(pattern: str, scope: str, org: str, namespace: str) -> None:
    """422 for a regex `compile_regex` refuses, or one past the scope's program budget:
    every regex a write's scopes hold runs on that write."""
    from fastapi import HTTPException

    from server.core.errors import ValidationError

    try:
        size = compile_regex(pattern).programsize
    except ValidationError as e:
        reason = str(e)
    else:
        held = await scope_program(scope, org, namespace)
        if held + size <= REGEX_PROGRAM_PER_SCOPE:
            return
        reason = (
            f"this {scope}'s regexes would pass {REGEX_PROGRAM_PER_SCOPE} RE2 instructions "
            f"({held} held, {size} more)"
        )
    raise HTTPException(
        status_code=422,
        detail={"code": "BLACKLIST_PATTERN_INVALID", "message": reason},
    )


async def handle_blacklist_add(input: BlacklistAddInput) -> BlacklistAddOutput:
    # Op role is WRITER (namespace-scoped Refuse from agent sessions). A wider scope filters
    # reads and blocks writes for other namespaces or other tenants, so it needs the role
    # whose reach matches.
    _require_scope_authority(input.scope, "add", row_org=input.org)
    if input.pattern_type == "regex":
        await _reject_bad_regex(
            input.pattern, input.scope, *_scope_args(input.scope, input.org, input.namespace)
        )

    pattern_id = str(uuid.uuid4())
    now = naive_utc_now_seconds()
    stored_org, stored_ns = _scope_args(input.scope, input.org, input.namespace)
    created_by = _stamped_author(input.created_by)
    await blacklist_repo.insert_row(
        {
            "pattern_id": pattern_id,
            "org": stored_org,
            "namespace": stored_ns,
            "pattern": input.pattern,
            "pattern_type": input.pattern_type,
            "scope": input.scope,
            "reason": input.reason,
            "created_by": created_by,
            "hit_count": 0,
            "created_at": now,
            "tombstoned_at": None,
        }
    )
    invalidate_cache()
    await _bump_version(updated_by=f"add:{created_by or 'unknown'}")
    if not input.sweep:
        return BlacklistAddOutput(pattern_id=pattern_id, created_at=now)
    # The pattern reaches the rows written before it.
    from server.memory.app.blacklist_sweep import sweep_pattern

    res = await sweep_pattern(
        org=input.org,
        namespace=input.namespace,
        pattern_id=pattern_id,
        pattern=input.pattern,
        pattern_type=input.pattern_type,
        scope=input.scope,
    )
    return BlacklistAddOutput(
        pattern_id=pattern_id,
        created_at=now,
        swept=res.tombstoned,
        swept_truncated=res.truncated,
        sweep_error=res.error,
    )


async def handle_blacklist_sweep(input: BlacklistSweepInput) -> BlacklistSweepOutput:
    """Tombstone the live rows an existing pattern matches.

    The pattern is addressed by id, so the row decides who may sweep with it,
    by the reach of the sweep. A namespace pattern tombstones rows in ITS
    namespace, so the caller needs WRITER there (what adding it with sweep=true
    needs) — the transport only checked the body's namespace; anyone else gets
    the same 404 as an unknown id. An org pattern sweeps the whole org, so it
    needs ORG_ADMIN of that org, as its add did. A global pattern sweeps this
    org only — one tenant's Refuse never reaches another's rows from here — so
    its reach, and its gate, is ORG_ADMIN of this org."""
    from fastapi import HTTPException

    from server.auth.authz import Role, row_in_scope

    not_found = HTTPException(
        status_code=404,
        detail={"code": "NOT_FOUND", "message": f"no active pattern {input.pattern_id!r}"},
    )
    existing = await blacklist_repo.find_active_pattern(input.pattern_id, org=input.org)
    if existing is None:
        # Only a global row (org='*') is visible from another org's call;
        # another org's org-scoped pattern is not this org's to sweep with.
        existing = await blacklist_repo.find_active_pattern(input.pattern_id, org="*")
    if existing is None:
        raise not_found
    scope = str(existing.get("scope") or "namespace")
    if scope == "namespace":
        # Hidden, not refused: a 403 would confirm the id names a pattern.
        if not await row_in_scope(input.org, str(existing.get("namespace") or ""), Role.WRITER):
            raise not_found
    else:
        # Both wider scopes sweep one org (this one): ORG_ADMIN of it.
        _require_scope_authority("org", "sweep", row_org=input.org)
    from server.memory.app.blacklist_sweep import sweep_pattern

    res = await sweep_pattern(
        org=input.org,
        namespace=str(existing.get("namespace") or input.namespace),
        pattern_id=input.pattern_id,
        pattern=str(existing.get("pattern") or ""),
        pattern_type=str(existing.get("pattern_type") or "substring"),
        scope=scope,
    )
    return BlacklistSweepOutput(
        pattern_id=res.pattern_id,
        matched=res.matched,
        tombstoned=res.tombstoned,
        truncated=res.truncated,
        error=res.error,
    )


async def handle_blacklist_list(
    input: BlacklistListInput,
) -> BlacklistListOutput:
    # A token narrowed to one user never reaches here (NARROW_SAFE_OPS).
    rows = await blacklist_repo.list_for_scope(
        input.org,
        input.namespace,
        scope=input.scope,
        limit=input.limit,
    )
    return BlacklistListOutput(
        rows=[
            _BlacklistRow(
                pattern_id=str(r["pattern_id"]),
                org=r["org"],
                namespace=r["namespace"],
                pattern=r["pattern"],
                pattern_type=r["pattern_type"],
                scope=r["scope"],
                reason=r["reason"],
                created_by=r["created_by"],
                hit_count=int(r["hit_count"]),
                last_hit_at=r.get("last_hit_at"),
                created_at=r["created_at"],
            )
            for r in rows
        ]
    )


async def handle_blacklist_remove(
    input: BlacklistRemoveInput,
) -> BlacklistRemoveOutput:
    """Soft-delete via tombstoned_at; the cache drops tombstoned rows on refresh.

    One lookup by id; the row's own scope decides who may remove it. A namespace pattern
    needs ADMIN on that namespace and is hidden (removed=False) from anyone else, as a
    foreign row is elsewhere; an org pattern needs ORG_ADMIN of its org; a global pattern
    needs PLATFORM_ADMIN. There is no fall-through from one scope's lookup to another's (an
    org-scoped miss never falls back to a global lookup).
    """

    now = naive_utc_now_seconds()
    existing = await blacklist_repo.find_active_pattern(input.pattern_id)
    if existing is None:
        return BlacklistRemoveOutput(removed=False)
    row_org = str(existing.get("org") or "")
    row_ns = str(existing.get("namespace") or "")
    scope = str(existing.get("scope") or "namespace")
    if scope == "namespace":
        # Another org's row, or a namespace the caller does not administer:
        # answer as if it did not exist rather than confirm the id.
        if row_org != input.org:
            return BlacklistRemoveOutput(removed=False)
        from server.auth.authz import Role, row_in_scope

        if not await row_in_scope(row_org, row_ns, Role.ADMIN):
            return BlacklistRemoveOutput(removed=False)
    else:
        if scope == "org" and row_org != input.org:
            # An org-scoped pattern of another org is not addressable here.
            return BlacklistRemoveOutput(removed=False)
        _require_scope_authority(scope, "remove", row_org=row_org)
    await blacklist_repo.tombstone_pattern(input.pattern_id, now)
    invalidate_cache()
    await _bump_version(updated_by=f"remove:{input.removed_by or 'unknown'}")
    return BlacklistRemoveOutput(removed=True, removed_at=now)
