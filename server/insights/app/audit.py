"""Audit plane handler — query `loom.audit_log` scoped to the caller — and the
audit trace other domains read (`audit_rows_for_request`)."""

from __future__ import annotations

import base64
from datetime import datetime
from functools import partial
from typing import Any

from server.core.ch import get_client
from server.core.cursors import decode_cursor as _decode_cursor
from server.core.cursors import encode_cursor as _encode_cursor
from server.core.errors import ValidationError
from server.erasure.domain.failure_reason import caller_audit_subject
from server.insights.api.models_audit import AuditListInput, AuditListOutput, AuditListRow
from server.insights.store import audit as audit_repo
from server.operations.read_projection import project_rows


async def audit_rows_for_request(request_id: str, *, limit: int = 100) -> list[audit_repo.AuditRow]:
    """Every audit event tagged with `request_id`, oldest first (at most `limit`)."""
    return await audit_repo.list_by_request_id(request_id, limit=limit)


# A page cursor comes in two formats. The previous build reads only the first,
# `<ts>|<request_id>` (everything after the first bar is the id), so this build
# still writes it, and keeps a page from ending inside one write's rows instead
# (`_finish_the_write`). The second, `v2|<ts>|<row_hash>|<request_id>`, adds the row
# hash; no first-format cursor starts with `v` (it starts with the year), and the
# request id goes last so a `|` in it is kept. This build reads both; the next
# release may write v2 (docs/architecture/rolling-upgrades.md, rule 4).
# `record_cursor` writes v2 already: its op is one the previous build does not serve.
_V2 = "v2|"


def record_cursor(row: AuditListRow) -> str:
    """A cursor whose page starts strictly after `row`."""
    raw = f"{_V2}{row.ts.isoformat()}|{row.row_hash}|{row.request_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_page_cursor(cursor: str) -> tuple[datetime, str, str | None]:
    """A page cursor's keyset: (ts, request_id, row_hash), the hash None for a
    first-format cursor."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except ValueError:
        raise ValidationError("cursor is not one this op issued") from None
    if not raw.startswith(_V2):
        ts, request_id = _decode_cursor(cursor)
        return ts, request_id, None
    try:
        ts_str, row_hash, request_id = raw[len(_V2) :].split("|", 2)
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        raise ValidationError("cursor is not one this op issued") from None
    # The hash binds as FixedString(64): a SHA-256 hex, or empty for a legacy row.
    if len(row_hash.encode("utf-8")) not in (0, 64):
        raise ValidationError("cursor is not one this op issued")
    return ts, request_id, row_hash


async def namespace_events(
    org: str,
    namespace: str,
    event_type: str,
    *,
    since: datetime | None,
    cursor: str | None,
    limit: int,
) -> list[AuditListRow]:
    """Up to `limit` of one namespace's `event_type` rows, newest first, after `cursor`
    (one `record_cursor` or `audit.list` issued). The caller authorizes the namespace."""
    page = await audit_repo.list_page(
        org,
        scope_where=" AND namespace = {ns:String}",
        scope_params={"ns": namespace},
        event_type=event_type,
        since=since,
        before=_decode_page_cursor(cursor) if cursor else None,
        limit=limit,
    )
    return [AuditListRow(**r.model_dump()) for r in page]


def _same_write(a: audit_repo.AuditRow, b: audit_repo.AuditRow) -> bool:
    return (a.ts, a.request_id) == (b.ts, b.request_id)


# One write's audit rows, read whole: a request writes far fewer.
_WRITE_ROWS = 10_000  # not a setting: a read cap far above one request's rows


async def _finish_the_write(
    page: list[audit_repo.AuditRow], limit: int, read_page: Any
) -> tuple[list[audit_repo.AuditRow], bool]:
    """Cut `page` (up to `limit` + 1 rows) to `limit`. When the cut falls inside
    one write (rows of the same millisecond and request id), the page ends with
    every row of that write, read again whole: their hashes need not differ (a
    legacy row has none), so they cannot be read "after the last hash". The
    cursor names only (ts, request_id), so the next page starts after all of
    them; a page can then hold more than `limit` rows. Returns (rows, has_more)."""
    rows, rest = page[:limit], page[limit:]
    if not (rows and rest and _same_write(rest[0], rows[-1])):
        return rows, bool(rest)
    last = rows[-1]
    write = await read_page(write=(last.ts, last.request_id), limit=_WRITE_ROWS)
    rows = [r for r in rows if not _same_write(r, last)] + write
    older = await read_page(before=(last.ts, last.request_id, None), limit=1)
    return rows, bool(older)


async def handle_audit_list(input: AuditListInput) -> AuditListOutput:
    ch = get_client()
    scope_where = ""
    params: dict[str, Any] = {}
    if input.namespace:
        # A supplied namespace must still be one the caller may see. The op sets
        # skip_namespace_authz, so the transport gate doesn't cover this branch —
        # without the check a non-admin could read any namespace's audit trail by
        # naming it. Mirror the org-scope rule: a restricted namespace the caller
        # fails the two-factor check for is hidden from everyone, admins and `*`
        # tokens included (resolve_org_scope drops the same set); otherwise
        # admins (and auth-off) pass and a non-admin is held to their own
        # namespaces.
        from server.auth.authz import claim_cap, may_be_restricted, restricted_allows
        from server.auth.context import (
            current_auth,
            resolve_my_namespaces,
        )
        from server.auth.fail_closed import refused_unverified, reset
        from server.operations.base import Role

        auth = current_auth()
        if auth is not None and auth.subject:
            # The caller named this one: one check decides it, and marks the
            # request when a read it needed failed.
            if await may_be_restricted(input.org, input.namespace) and not await restricted_allows(
                auth, Role.VIEWER, input.org, input.namespace
            ):
                return AuditListOutput(rows=[], next_cursor=None)
            is_admin = any(
                r.value >= Role.ORG_ADMIN.value for r in getattr(auth, "roles", []) or []
            )
            if not is_admin:
                # Covered if the JWT reaches the namespace — can_access honours
                # the `*` org/namespace wildcards (operator tooling) — or a
                # grant elevates the caller into it.
                reset()
                covered = (
                    auth.can_access(input.org, input.namespace)
                    and await claim_cap(auth, input.org, input.namespace) != 0
                ) or input.namespace in await resolve_my_namespaces(input.org)
                if not covered:
                    refused_unverified()
                    return AuditListOutput(rows=[], next_cursor=None)
        scope_where = " AND namespace = {ns:String}"
        params["ns"] = input.namespace
    else:
        from server.auth.context import resolve_org_scope

        scope = await resolve_org_scope(input.org)
        if not scope.accessible:
            return AuditListOutput(rows=[], next_cursor=None)
        scope_where = scope.where
        params.update(scope.params)
    read_page = partial(
        audit_repo.list_page,
        input.org,
        scope_where=scope_where,
        scope_params=params,
        event_type=input.event_type,
        since=input.since,
        limit=input.limit + 1,  # +1 to detect "more"
        ch=ch,
    )
    before = _decode_page_cursor(input.cursor) if input.cursor else None
    page = await read_page(before=before)
    # The page's last write is read again under the same cursor.
    page, has_more = await _finish_the_write(page, input.limit, partial(read_page, before=before))
    rows = [
        AuditListRow(**{**r.model_dump(), "subject": caller_audit_subject(r.event_type, r.subject)})
        for r in page
    ]
    next_cursor = _encode_cursor(page[-1].ts, page[-1].request_id) if rows and has_more else None
    project_rows(rows, AuditListRow, input.fields, always=frozenset({"ts"}))
    return AuditListOutput(rows=rows, next_cursor=next_cursor)
