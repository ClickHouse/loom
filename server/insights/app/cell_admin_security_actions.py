"""Cell adapters for signing, revocation and global refusal policy owners."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import HTTPException

from server.auth import keyring, subject_revocation
from server.auth.context import current_auth
from server.core.ch import statement_timeout
from server.core.ch_bounded import BoundedReadError
from server.core.repo_base import _default_client
from server.insights.api.models_cell_admin import (
    CellBlacklistGlobalAddInput,
    CellBlacklistGlobalListInput,
    CellSigningKeysListInput,
    CellSigningKeysRetireInput,
    CellSigningKeysRotateInput,
    CellSubjectRevokeInput,
)
from server.insights.app.cell_admin_actions import _allowance
from server.memory.app import blacklist
from server.observability import audit


async def signing_keys_list(
    input: CellSigningKeysListInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        rows = await keyring.listing_bounded(ch=ch or _default_client(), deadline=deadline)
        return {
            "keys": [
                {
                    "kid": row["kid"],
                    "algorithm": row["algorithm"],
                    "created_at": row["created_at"].isoformat(),
                    "activate_at": row["activate_at"].isoformat(),
                    "retired_at": row["retired_at"].isoformat()
                    if row["retired_at"] is not None
                    else None,
                }
                for row in rows
            ]
        }


async def signing_keys_rotate(
    input: CellSigningKeysRotateInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        key = await keyring.rotate_bounded(
            input.algorithm,
            activate_in_s=input.activate_in,
            ch=ch or _default_client(),
            deadline=deadline,
        )
        return {
            "kid": key.kid,
            "algorithm": key.algorithm,
            "activate_at": key.activate_at.isoformat(),
        }


async def signing_keys_retire(
    input: CellSigningKeysRetireInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        retired = await keyring.retire_bounded(
            input.kid,
            ch=ch or _default_client(),
            deadline=deadline,
        )
        return {"kid": input.kid, "retired": retired}


async def blacklist_global_list(
    input: CellBlacklistGlobalListInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        return await blacklist.list_global_bounded(
            input.limit,
            ch=ch or _default_client(),
            deadline=deadline,
        )


async def blacklist_global_add(
    input: CellBlacklistGlobalAddInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    async with _allowance(deadline) as deadline:
        result = await blacklist.add_global_bounded(
            input.pattern_id,
            input.pattern,
            input.pattern_type,
            input.reason,
            ch=ch or _default_client(),
            deadline=deadline,
        )
        await _blacklist_audit(
            "blacklist.add",
            input.pattern_id,
            input.context.command_digest,
            deadline,
        )
        return {"pattern_id": result.pattern_id, "created_at": result.created_at.isoformat()}


async def _blacklist_audit(event: str, pattern_id: str, digest: str, deadline: float) -> None:
    auth = current_auth()
    remaining = min(2.0, deadline - time.monotonic())
    if auth is None or remaining <= 0:
        raise BoundedReadError("Blacklist audit allowance exhausted")
    async with asyncio.timeout(remaining):
        with statement_timeout(remaining):
            confirmed = await audit.emit_strict(
                event,
                org="-",
                namespace="-",
                actor=auth.subject,
                subject={
                    "pattern_id": pattern_id,
                    "scope": "global",
                    "command_digest": digest,
                    "confirmed": True,
                },
            )
    if not confirmed:
        raise BoundedReadError("Blacklist domain audit was not confirmed")


async def subject_revoke(
    input: CellSubjectRevokeInput,
    *,
    deadline: float,
    ch: Any = None,
) -> dict[str, Any]:
    auth = current_auth()
    if auth is None or subject_revocation.refusal(input.subject) is not None:
        raise BoundedReadError("An authenticated outright subject action is required")
    progress: dict[str, Any] = {"recorded": False, "sessions": 0, "tokens": 0}
    async with _allowance(deadline) as deadline:
        client = ch or _default_client()
        try:
            row, kept = await subject_revocation.record(
                input.subject,
                by=auth.subject,
                reason=input.reason,
                deadline=deadline,
                ch=client,
                progress=progress,
            )
            result = await subject_revocation.finish(
                input.subject,
                row,
                kept,
                by=auth.subject,
                org="-",
                deadline=deadline,
                ch=client,
                progress=progress,
            )
            if not result.outright or result.cutoff is not None:
                raise BoundedReadError("The outright subject revocation was not confirmed")
        except Exception as error:  # Confirmed partial facts must never become success.
            if not progress["recorded"]:
                raise
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "SUBJECT_REVOCATION_INCOMPLETE",
                    "recorded": True,
                    "sessions_at_least": progress["sessions"],
                    "tokens_at_least": progress["tokens"],
                },
            ) from error
        return {
            "subject": input.subject.strip(),
            "recorded": True,
            "already_revoked": result.kept,
            "outright": result.outright,
            "cutoff": result.cutoff.isoformat() if result.cutoff else None,
            "sessions": result.sessions,
            "tokens": result.tokens,
        }
