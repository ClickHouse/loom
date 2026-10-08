"""Op declarations for self-service namespace ops.

The four ops (browse / join / leave / list_mine) must be registered
on the wire surface, role-gated appropriately, and (where needed)
opted out of the transport's namespace-access check. These tests
verify the static shape — handler-level logic is exercised by the
integration suite which needs a live ClickHouse.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def test_four_new_ops_are_registered() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    for name in (
        "namespace.browse",
        "namespace.join",
        "namespace.leave",
        "namespace.list_mine",
    ):
        assert name in OPERATIONS_BY_NAME, f"{name} missing from the wire surface"


def test_self_service_ops_are_viewer_role() -> None:
    """Any authenticated caller can browse / join / leave / list — the
    handler enforces the finer-grained checks (visibility, membership)."""
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    for name in (
        "namespace.browse",
        "namespace.join",
        "namespace.leave",
        "namespace.list_mine",
    ):
        op = OPERATIONS_BY_NAME[name]
        assert op.required_role == Role.VIEWER, (
            f"{name} required_role should be VIEWER, got {op.required_role}"
        )


def test_join_leave_register_skip_namespace_authz() -> None:
    """These ops operate ON a namespace the caller may not yet have
    access to — the transport's namespace-access gate must be skipped
    so the handler runs and enforces its own rules."""
    from server.operations.registry import OPERATIONS_BY_NAME

    for name in ("namespace.join", "namespace.leave", "namespace.register"):
        assert OPERATIONS_BY_NAME[name].skip_namespace_authz is True, (
            f"{name} must opt out of namespace authz"
        )


def test_browse_and_list_mine_keep_namespace_authz() -> None:
    """Browse + list_mine don't take an `input.namespace`, so the gate
    is naturally inert. The default skip_namespace_authz=False is fine."""
    from server.operations.registry import OPERATIONS_BY_NAME

    assert OPERATIONS_BY_NAME["namespace.browse"].skip_namespace_authz is False
    assert OPERATIONS_BY_NAME["namespace.list_mine"].skip_namespace_authz is False


def test_namespace_register_visibility_field_defaults_private() -> None:
    """A new namespace is private unless the caller asks for `public`."""
    from datetime import datetime

    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    inp = NamespaceRegisterInput(
        org="o",
        namespace="n",
        owner_email="alice@example.com",
    )
    assert registry_row(inp, None, datetime(2026, 1, 1))["visibility"] == "private"


def test_grants_list_role_relaxed_to_viewer() -> None:
    """A namespace member (any role) can see who else is in. Non-members
    fall through check_authorized's grants-table lookup and get 403."""
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["grants.list"]
    assert op.required_role == Role.VIEWER
