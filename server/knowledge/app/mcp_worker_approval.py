"""Apply an administrator's approval within the deployment's worker provision."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.auth import authz
from server.auth.answer_tokens import is_answer_token
from server.auth.context import current_auth
from server.auth.session_protection import protected_for
from server.core.diagnostics import private_work
from server.knowledge.app import connected_accounts, session_admission
from server.knowledge.store import pg_mcp_worker_approvals as approvals
from server.knowledge.store.pg_mcp_worker_enrollment import WriteOrigin
from server.memory.app.provenance import writer_actor
from server.memory.domain.provenance import source_class_for
from server.operations.base import Role
from server.settings import settings

if TYPE_CHECKING:
    from asyncpg import Connection

    from server.auth.jwt import AuthInfo


class WorkerApprovalRefused(ValueError):
    def __init__(self) -> None:
        super().__init__("source worker approval unavailable")


@dataclass(frozen=True, repr=False)
class WorkerProvision:
    key: approvals.ApprovalKey
    modes: int
    exact_team_checks: bool


@dataclass(frozen=True, repr=False)
class ApprovalChange:
    key: approvals.ApprovalKey
    expected_generation: int
    modes: int
    team_access: bool


# The provision is deployment-owned. Its lookup and the erasure fence must
# participate in the same authority transaction as an approval mutation.
type GetProvision = Callable[[approvals.ApprovalKey, Connection], Awaitable[WorkerProvision | None]]
type CheckErasure = Callable[[str, str, str, Connection], Awaitable[bool]]


def _can_approve(change: ApprovalChange, provision: WorkerProvision | None) -> bool:
    if type(change.modes) is not int or change.modes < 0 or type(change.team_access) is not bool:
        return False
    if change.modes == 0:
        return not change.team_access
    return (
        provision is not None
        and provision.key == change.key
        and change.modes & provision.modes == change.modes
        and (not change.team_access or provision.exact_team_checks is True)
    )


async def _admin(auth: AuthInfo, key: approvals.ApprovalKey) -> str:
    if (
        current_auth() is not auth
        or auth.org != key.org
        or auth.impersonated_by
        or is_answer_token(auth)
        or (auth.write_namespaces and not auth.can_write(key.org, key.namespace))
        or not settings.auth_actually_required()
        or not settings.knowledge_mirrored_sources_enabled
        or not protected_for(auth)
    ):
        raise WorkerApprovalRefused()
    await session_admission.require(auth)
    if not await authz.row_in_scope(key.org, key.namespace, Role.ADMIN):
        raise WorkerApprovalRefused()
    org, subject = await connected_accounts.own_subject(auth)
    if org != key.org:
        raise WorkerApprovalRefused()
    return subject


async def apply_worker_approval(
    change: ApprovalChange, *, provision: GetProvision, not_erasing: CheckErasure
) -> approvals.WorkerApproval:
    with private_work():
        auth = current_auth()
        if auth is None:
            raise WorkerApprovalRefused()
        subject = await _admin(auth, change.key)
        async with approvals.scope_transaction(change.key) as conn:
            if await _admin(auth, change.key) != subject or not await not_erasing(
                change.key.org, change.key.namespace, subject, conn
            ):
                raise WorkerApprovalRefused()
            bound = await provision(change.key, conn) if change.modes else None
            if not _can_approve(change, bound):
                raise WorkerApprovalRefused()
            who = approvals.ApprovalOwner(
                subject,
                WriteOrigin(
                    auth.agent_principal.removeprefix("agent:") or "-",
                    auth.session_id or "-",
                    writer_actor(),
                    source_class_for(default="asserted"),
                ),
            )
            saved = await approvals.replace_on(
                conn,
                change.key,
                expected_generation=change.expected_generation,
                modes=change.modes,
                team_access=change.team_access,
                owner=who,
            )
            if saved is None:
                raise WorkerApprovalRefused()
            return saved
