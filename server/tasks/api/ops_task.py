"""The `task.*` and `inbox.*` operation declarations; their wire models are in `models_task`."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from server.operations.base import Operation, Role
from server.tasks.api.models_task import (
    InboxListInput,
    InboxListOutput,
    InboxPolicyListInput,
    InboxPolicyListOutput,
    InboxPolicySetInput,
    InboxPolicySetOutput,
    InboxPrincipalsInput,
    InboxPrincipalsOutput,
    InboxRegisterInput,
    InboxRegisterOutput,
    TaskAcceptInput,
    TaskBlockInput,
    TaskClaimInput,
    TaskCompleteInput,
    TaskCountsInput,
    TaskCountsOutput,
    TaskCreateInput,
    TaskCreateOutput,
    TaskDeferInput,
    TaskFailInput,
    TaskGetInput,
    TaskGetOutput,
    TaskHeartbeatInput,
    TaskHistoryInput,
    TaskHistoryOutput,
    TaskListInput,
    TaskListOutput,
    TaskOutput,
    TaskProgressInput,
    TaskPullInput,
    TaskPullOutput,
    TaskReasonInput,
    TaskReassignInput,
    TaskReassignOutput,
    TaskResumeInput,
    TaskUnblockInput,
    TaskUpdateInput,
    TaskUpdateOutput,
    TaskUpdatesInput,
    TaskUpdatesOutput,
)
from server.tasks.app.inboxes import (
    handle_inbox_list,
    handle_inbox_policy_list,
    handle_inbox_policy_set,
    handle_inbox_principals,
    handle_inbox_register,
)
from server.tasks.app.reads import (
    handle_task_counts,
    handle_task_get,
    handle_task_history,
    handle_task_list,
    handle_task_updates,
)
from server.tasks.app.transitions import (
    handle_task_accept,
    handle_task_block,
    handle_task_cancel,
    handle_task_claim,
    handle_task_complete,
    handle_task_create,
    handle_task_defer,
    handle_task_fail,
    handle_task_heartbeat,
    handle_task_progress,
    handle_task_pull,
    handle_task_reassign,
    handle_task_reopen,
    handle_task_resume,
    handle_task_unblock,
    handle_task_update,
    handle_task_wont_do,
)

# ============================================================
# Operation declarations
# ============================================================


def _op(
    name: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    handler: Any,
    *,
    role: Role,
    idempotent: bool,
    audit: bool = False,
) -> Operation:
    return Operation(
        name=name,
        maturity="experimental",
        input_model=input_model,
        output_model=output_model,
        handler=handler,
        idempotent=idempotent,
        required_role=role,
        audit=audit,
    )


inbox_register = _op(
    "inbox.register",
    InboxRegisterInput,
    InboxRegisterOutput,
    handle_inbox_register,
    role=Role.ADMIN,
    idempotent=True,
    # An admin changing who may post and how the inbox behaves: audited like
    # policy.set and grants.create.
    audit=True,
)
inbox_list = _op(
    "inbox.list",
    InboxListInput,
    InboxListOutput,
    handle_inbox_list,
    role=Role.VIEWER,
    idempotent=True,
)
inbox_principals = _op(
    "inbox.principals",
    InboxPrincipalsInput,
    InboxPrincipalsOutput,
    handle_inbox_principals,
    role=Role.ADMIN,
    idempotent=True,
)
inbox_policy_set = _op(
    "inbox.policy_set",
    InboxPolicySetInput,
    InboxPolicySetOutput,
    handle_inbox_policy_set,
    role=Role.ADMIN,
    idempotent=True,
    # An admin changing who may post and how the inbox behaves: audited like
    # policy.set and grants.create.
    audit=True,
)
inbox_policy_list = _op(
    "inbox.policy_list",
    InboxPolicyListInput,
    InboxPolicyListOutput,
    handle_inbox_policy_list,
    role=Role.VIEWER,
    idempotent=True,
)

task_create = _op(
    "task.create",
    TaskCreateInput,
    TaskCreateOutput,
    handle_task_create,
    role=Role.WRITER,
    idempotent=True,
)
task_accept = _op(
    "task.accept",
    TaskAcceptInput,
    TaskOutput,
    handle_task_accept,
    role=Role.WRITER,
    idempotent=False,
)
task_pull = _op(
    "task.pull",
    TaskPullInput,
    TaskPullOutput,
    handle_task_pull,
    role=Role.WRITER,
    idempotent=False,
)
task_claim = _op(
    "task.claim",
    TaskClaimInput,
    TaskOutput,
    handle_task_claim,
    role=Role.WRITER,
    idempotent=False,
)
task_heartbeat = _op(
    "task.heartbeat",
    TaskHeartbeatInput,
    TaskOutput,
    handle_task_heartbeat,
    role=Role.WRITER,
    idempotent=True,
)
task_progress = _op(
    "task.progress",
    TaskProgressInput,
    TaskOutput,
    handle_task_progress,
    role=Role.WRITER,
    idempotent=False,
)
task_defer = _op(
    "task.defer",
    TaskDeferInput,
    TaskOutput,
    handle_task_defer,
    role=Role.WRITER,
    idempotent=False,
)
task_block = _op(
    "task.block",
    TaskBlockInput,
    TaskOutput,
    handle_task_block,
    role=Role.WRITER,
    idempotent=False,
)
task_resume = _op(
    "task.resume",
    TaskResumeInput,
    TaskOutput,
    handle_task_resume,
    role=Role.WRITER,
    idempotent=False,
)
task_unblock = _op(
    "task.unblock",
    TaskUnblockInput,
    TaskOutput,
    handle_task_unblock,
    role=Role.WRITER,
    idempotent=False,
)
task_complete = _op(
    "task.complete",
    TaskCompleteInput,
    TaskOutput,
    handle_task_complete,
    role=Role.WRITER,
    idempotent=False,
)
task_fail = _op(
    "task.fail",
    TaskFailInput,
    TaskOutput,
    handle_task_fail,
    role=Role.WRITER,
    idempotent=False,
)
task_wont_do = _op(
    "task.wont_do",
    TaskReasonInput,
    TaskOutput,
    handle_task_wont_do,
    role=Role.WRITER,
    idempotent=False,
)
task_cancel = _op(
    "task.cancel",
    TaskReasonInput,
    TaskOutput,
    handle_task_cancel,
    role=Role.WRITER,
    idempotent=False,
)
task_reopen = _op(
    "task.reopen",
    TaskReasonInput,
    TaskOutput,
    handle_task_reopen,
    role=Role.WRITER,
    idempotent=False,
)
task_update = _op(
    "task.update",
    TaskUpdateInput,
    TaskUpdateOutput,
    handle_task_update,
    role=Role.WRITER,
    idempotent=False,
)
task_reassign = _op(
    "task.reassign",
    TaskReassignInput,
    TaskReassignOutput,
    handle_task_reassign,
    role=Role.WRITER,
    idempotent=False,
)
task_get = _op(
    "task.get",
    TaskGetInput,
    TaskGetOutput,
    handle_task_get,
    role=Role.VIEWER,
    idempotent=True,
)
task_list = _op(
    "task.list",
    TaskListInput,
    TaskListOutput,
    handle_task_list,
    role=Role.VIEWER,
    idempotent=True,
)
task_counts = _op(
    "task.counts",
    TaskCountsInput,
    TaskCountsOutput,
    handle_task_counts,
    role=Role.VIEWER,
    idempotent=True,
)
task_history = _op(
    "task.history",
    TaskHistoryInput,
    TaskHistoryOutput,
    handle_task_history,
    role=Role.VIEWER,
    idempotent=True,
)
task_updates = _op(
    "task.updates",
    TaskUpdatesInput,
    TaskUpdatesOutput,
    handle_task_updates,
    role=Role.VIEWER,
    idempotent=True,
)

OPERATIONS: list[Operation] = [
    inbox_register,
    inbox_list,
    inbox_principals,
    inbox_policy_set,
    inbox_policy_list,
    task_create,
    task_accept,
    task_pull,
    task_claim,
    task_heartbeat,
    task_progress,
    task_defer,
    task_block,
    task_unblock,
    task_resume,
    task_complete,
    task_fail,
    task_wont_do,
    task_cancel,
    task_reopen,
    task_update,
    task_reassign,
    task_get,
    task_list,
    task_counts,
    task_history,
    task_updates,
]
