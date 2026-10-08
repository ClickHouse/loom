"""An admin write on the task inbox plane leaves an audit row, like policy.set
and grants.create: who changed an inbox's posters, policy or webhook is
answerable from loom.audit_log."""

from __future__ import annotations

from server.operations.base import Role
from server.operations.registry import ALL_OPERATIONS

_OPS = {op.name: op for op in ALL_OPERATIONS}
# ADMIN-role ops on the plane that only read; they need no audit row.
_ADMIN_READS = {"inbox.principals"}


def test_admin_inbox_writes_are_audited() -> None:
    assert _OPS["inbox.register"].audit
    assert _OPS["inbox.policy_set"].audit


def test_every_admin_write_on_the_plane_is_audited() -> None:
    admin = [
        op
        for name, op in _OPS.items()
        if name.startswith(("inbox.", "task.")) and op.required_role.value >= Role.ADMIN.value
    ]
    assert admin, "the plane has admin ops"
    for op in admin:
        assert op.audit or op.name in _ADMIN_READS, op.name
