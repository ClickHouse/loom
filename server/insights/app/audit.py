"""Audit plane handler — query `loom.audit_log` scoped to the caller — and the
audit trace other domains read (`audit_rows_for_request`)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from server.core.ch import get_client
from server.core.cursors import decode_cursor as _decode_cursor
from server.core.cursors import encode_cursor as _encode_cursor
from server.insights.api.models_audit import AuditListInput, AuditListOutput, AuditListRow
from server.insights.store import audit as audit_repo
from server.operations.read_projection import project_rows


async def audit_rows_for_request(request_id: str, *, limit: int = 100) -> list[audit_repo.AuditRow]:
    """Every audit event tagged with `request_id`, oldest first (at most `limit`)."""
    return await audit_repo.list_by_request_id(request_id, limit=limit)


_ROW_HASH = re.compile(r"[0-9a-f]{64}")


def _decode_page_cursor(cursor: str) -> tuple[datetime, str, str | None]:
    """A page cursor's keyset: (ts, request_id, row_hash). The request id is the
    client's (`X-Request-Id`) and may hold `|`, so the hash is the last part, and only
    if it is one; a cursor issued before the hash was added has none."""
    ts, extra = _decode_cursor(cursor)
    request_id, _, row_hash = extra.rpartition("|")
    if _ROW_HASH.fullmatch(row_hash):
        return ts, request_id, row_hash
    return ts, extra, None


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
        from server.auth.context import (
            current_auth,
            resolve_my_namespaces,
            restricted_namespaces_hidden_from,
        )
        from server.operations.base import Role

        auth = current_auth()
        if auth is not None and auth.subject:
            hidden = await restricted_namespaces_hidden_from(input.org)
            if hidden == ["*"] or input.namespace in hidden:
                return AuditListOutput(rows=[], next_cursor=None)
            is_admin = any(
                r.value >= Role.ORG_ADMIN.value for r in getattr(auth, "roles", []) or []
            )
            if not is_admin:
                # Covered if the JWT reaches the namespace — can_access honours
                # the `*` org/namespace wildcards (operator tooling) — or a
                # grant elevates the caller into it.
                covered = auth.can_access(input.org, input.namespace) or (
                    input.namespace in await resolve_my_namespaces(input.org)
                )
                if not covered:
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
    page = await audit_repo.list_page(
        input.org,
        scope_where=scope_where,
        scope_params=params,
        event_type=input.event_type,
        since=input.since,
        before=_decode_page_cursor(input.cursor) if input.cursor else None,
        limit=input.limit + 1,  # +1 to detect "more"
        ch=ch,
    )
    has_more = len(page) > input.limit
    page = page[: input.limit]
    rows = [AuditListRow(**r.model_dump()) for r in page]
    next_cursor = (
        _encode_cursor(page[-1].ts, page[-1].request_id, page[-1].row_hash)
        if rows and has_more
        else None
    )
    project_rows(rows, AuditListRow, input.fields, always=frozenset({"ts"}))
    return AuditListOutput(rows=rows, next_cursor=next_cursor)
