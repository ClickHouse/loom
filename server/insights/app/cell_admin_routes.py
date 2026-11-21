"""The authenticated capabilities of this cell's administration surface."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.core.ch import set_query_tag, statement_timeout
from server.core.ch_bounded import BoundedReadError
from server.core.hashing import sha256_value
from server.insights.api.models_cell_admin import (
    CellActionInput,
    CellBlacklistGlobalAddInput,
    CellBlacklistGlobalListInput,
    CellBudgetSetInput,
    CellCapabilitiesInput,
    CellRatelimitNamespaceSetInput,
    CellSettingsHistoryInput,
    CellSettingsListInput,
    CellSettingsSetInput,
    CellSigningKeysListInput,
    CellSigningKeysRetireInput,
    CellSigningKeysRotateInput,
    CellSubjectRevokeInput,
)
from server.insights.app import cell_admin_actions as actions
from server.insights.app import cell_admin_security_actions as security_actions
from server.insights.app.admin_routes import refuse_browser_reach
from server.insights.app.cell_admin_auth import require_cell_admin, verify_automation
from server.observability import audit

cell_admin_router = APIRouter(
    prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_cell_admin)]
)

AVAILABLE_ACTIONS = (
    "settings_list",
    "settings_set",
    "settings_history",
    "ratelimit_namespace_set",
    "budget_set",
    "signing_keys_list",
    "signing_keys_rotate",
    "signing_keys_retire",
    "subject_revoke",
    "blacklist_global_add",
    "blacklist_global_list",
)
_REFUSALS = frozenset(
    {
        "automation_required",
        "security_unavailable",
        "short_token_required",
        "cell_unavailable",
        "cell_identity_changed",
        "automation_unprovisioned",
        "automation_identity_changed",
        "automation_key_changed",
        "automation_revoked",
        "invalid_arguments",
        "action_unavailable",
    }
)


def _encode(value: Any, maximum: int) -> bytes:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > maximum:
        raise BoundedReadError("Cell administration response exceeds its allowance")
    return raw


def _audit_subject(subject: dict[str, Any]) -> None:
    # audit_row uses this ASCII JSON representation, including its spaces.
    if len(json.dumps(subject, sort_keys=True, default=str).encode()) > 2048:
        raise BoundedReadError("Cell administration audit exceeds its allowance")


def _intent(body: CellActionInput, action: str) -> None:
    arguments = body.model_dump(mode="json", exclude={"context"})
    digest = sha256_value({"action": action, "arguments": arguments})
    if digest != body.context.command_digest:
        raise HTTPException(422, detail={"code": "invalid_arguments"})


async def _audit(event: str, actor: str, subject: dict[str, Any], deadline: float) -> str:
    _audit_subject(subject)
    remaining = min(2.0, deadline - time.monotonic())
    if remaining <= 0:
        raise BoundedReadError("Cell administration allowance exhausted")
    with statement_timeout(remaining):
        row_hash = await audit.emit_strict(
            event, org="-", namespace="-", actor=actor, subject=subject
        )
    if not row_hash:
        raise BoundedReadError("Cell administration audit was not confirmed")
    return row_hash


def _refusal(body: CellActionInput, action: str, error: Exception) -> Response:
    code = (
        error.detail.get("code")
        if isinstance(error, HTTPException) and isinstance(error.detail, dict)
        else "security_unavailable"
    )
    if not isinstance(code, str) or code not in _REFUSALS:
        code = "invalid_arguments"
    value = {
        "version": 1,
        "status": "refused",
        "phase": "before_business",
        "context": body.context.model_dump(),
        "action": action,
        "code": code,
    }
    status = error.status_code if isinstance(error, HTTPException) else 503
    return Response(_encode(value, 2048), status_code=status, media_type="application/json")


async def _perform(
    request: Request,
    body: CellActionInput,
    auth: AuthInfo,
    action: str,
    handler: Callable[..., Awaitable[dict[str, Any]]],
) -> Response:
    deadline = time.monotonic() + 30.0
    subject = {"context": body.context.model_dump(), "action": action}
    token = current_auth_var.set(auth)
    try:
        async with asyncio.timeout_at(deadline):
            try:
                refuse_browser_reach(request, "cell administration")
                _intent(body, action)
                verified = await verify_automation(auth, body.context, deadline=deadline)
                set_query_tag("cell.each." + action)
                _audit_subject({**subject, "started_audit": "0" * 64, "result_digest": "0" * 64})
                started = await _audit(
                    "cell.each_started", verified.auth.subject, subject, deadline
                )
            except Exception as error:  # noqa: BLE001 — only this pre-business block may certify refusal
                return _refusal(body, action, error)
            options: dict[str, Any] = {"deadline": deadline}
            if action == "settings_set":
                options["actor"] = verified.auth.subject
            # Every failure from this invocation onward remains unsettled to the client.
            result = await handler(body, **options)
            _encode(result, 2 * 1024**2)
            digest = sha256_value(result)
            completed = await _audit(
                "cell.each_completed",
                verified.auth.subject,
                {**subject, "started_audit": started, "result_digest": digest},
                deadline,
            )
            metadata = {
                "version": 1,
                "status": "completed",
                "context": body.context.model_dump(),
                "action": action,
                "started_audit": started,
                "completed_audit": completed,
            }
            _encode(metadata, 2048)
            return Response(
                _encode({**metadata, "result": result}, 2 * 1024**2),
                media_type="application/json",
            )
    finally:
        current_auth_var.reset(token)


@cell_admin_router.post("/cell.capabilities")
async def cell_capabilities(
    body: CellCapabilitiesInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Report this verified cell's supported administration actions and fixed limits."""
    deadline = time.monotonic() + 30.0
    async with asyncio.timeout_at(deadline):
        await verify_automation(auth, body.context, deadline=deadline)
        value = {
            "version": 1,
            "context": body.context.model_dump(),
            "actions": list(AVAILABLE_ACTIONS),
            "limits": {
                "action_timeout_s": 30,
                "max_body_bytes": 65536,
                "max_response_bytes": 2097152,
            },
        }
        return Response(_encode(value, 8192), media_type="application/json")


@cell_admin_router.post("/cell.settings_list")
async def cell_settings_list(
    request: Request,
    body: CellSettingsListInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """List non-secret settings after fresh cell and automation authorization."""
    return await _perform(request, body, auth, "settings_list", actions.settings_list)


@cell_admin_router.post("/cell.settings_set")
async def cell_settings_set(
    request: Request,
    body: CellSettingsSetInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Change one live setting and confirm its completed cell administration audits."""
    return await _perform(request, body, auth, "settings_set", actions.settings_set)


@cell_admin_router.post("/cell.settings_history")
async def cell_settings_history(
    request: Request,
    body: CellSettingsHistoryInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Read the bounded change history of one live setting in this verified cell."""
    return await _perform(request, body, auth, "settings_history", actions.settings_history)


@cell_admin_router.post("/cell.ratelimit_namespace_set")
async def cell_ratelimit_namespace_set(
    request: Request,
    body: CellRatelimitNamespaceSetInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Change a namespace-label rate override through the cell's ordinary limiter."""
    return await _perform(
        request, body, auth, "ratelimit_namespace_set", actions.ratelimit_namespace_set
    )


@cell_admin_router.post("/cell.budget_set")
async def cell_budget_set(
    request: Request,
    body: CellBudgetSetInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Change one latency budget through this verified cell's ordinary budget owner."""
    return await _perform(request, body, auth, "budget_set", actions.budget_set)


@cell_admin_router.post("/cell.signing_keys_list")
async def cell_signing_keys_list(
    request: Request,
    body: CellSigningKeysListInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """List public signing-key metadata after fresh cell and automation authorization."""
    return await _perform(
        request, body, auth, "signing_keys_list", security_actions.signing_keys_list
    )


@cell_admin_router.post("/cell.signing_keys_rotate")
async def cell_signing_keys_rotate(
    request: Request,
    body: CellSigningKeysRotateInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Rotate a signing key through the ordinary cell owner and return public metadata."""
    return await _perform(
        request, body, auth, "signing_keys_rotate", security_actions.signing_keys_rotate
    )


@cell_admin_router.post("/cell.signing_keys_retire")
async def cell_signing_keys_retire(
    request: Request,
    body: CellSigningKeysRetireInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Retire one signing key through this verified cell's ordinary retirement checks."""
    return await _perform(
        request, body, auth, "signing_keys_retire", security_actions.signing_keys_retire
    )


@cell_admin_router.post("/cell.blacklist_global_add")
async def cell_blacklist_global_add(
    request: Request,
    body: CellBlacklistGlobalAddInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Add one exact global blacklist pattern with the verified bot as its author."""
    return await _perform(
        request, body, auth, "blacklist_global_add", security_actions.blacklist_global_add
    )


@cell_admin_router.post("/cell.subject_revoke")
async def cell_subject_revoke(
    request: Request,
    body: CellSubjectRevokeInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """Revoke one exact global subject through the bounded ordinary security owner."""
    return await _perform(request, body, auth, "subject_revoke", security_actions.subject_revoke)


@cell_admin_router.post("/cell.blacklist_global_list")
async def cell_blacklist_global_list(
    request: Request,
    body: CellBlacklistGlobalListInput,
    auth: Annotated[AuthInfo, Depends(require_cell_admin)],
) -> Response:
    """List bounded exact-global blacklist rows and report whether more rows exist."""
    return await _perform(
        request, body, auth, "blacklist_global_list", security_actions.blacklist_global_list
    )
