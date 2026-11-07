"""Op declarations for namespace.snapshot + namespace.restore + cost.summary.memory_key.

Lifecycle behavior (snapshot → restore → verify rows in target) is
covered by the live smoke against a running CH; these unit tests lock
in the wire shape so accidental breakages surface in CI.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


def test_snapshot_and_restore_ops_registered() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME

    assert "namespace.snapshot" in OPERATIONS_BY_NAME
    assert "namespace.restore" in OPERATIONS_BY_NAME


def test_restore_skips_namespace_authz() -> None:
    """Target namespace may not exist; transport must let the handler
    enforce the fresh-vs-existing precondition."""
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["namespace.restore"]
    assert op.skip_namespace_authz is True
    from server.operations.base import Role

    assert op.required_role == Role.VIEWER


def test_snapshot_op_is_admin() -> None:
    """Snapshot reads every row in a namespace — ADMIN gate, with the
    grants-table fallback letting existing namespace admins through."""
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    op = OPERATIONS_BY_NAME["namespace.snapshot"]
    assert op.required_role == Role.ADMIN
    assert op.skip_namespace_authz is False
    assert op.audit is True


def test_cross_promote_op_registered() -> None:
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME

    assert "memory.cross_promote" in OPERATIONS_BY_NAME
    op = OPERATIONS_BY_NAME["memory.cross_promote"]
    # Operates across namespaces — transport gate would fail on the
    # namespace the caller's JWT doesn't already cover. Handler
    # enforces ADMIN on both source and target via grants lookup.
    assert op.skip_namespace_authz is True
    assert op.required_role == Role.VIEWER
    assert op.audit is True


def test_snapshot_carries_live_grants_only(monkeypatch) -> None:
    """A restore writes every payload grant active, so a pending join request
    in the snapshot would come back as a real grant."""
    import asyncio

    import server.identity.app.namespace as registry
    import server.identity.app.namespace_snapshot as ns_handlers
    import server.memory.app.namespace_types as types
    from server.identity.api.models import NamespaceSnapshotInput

    class _CH:
        async def query_all(self, sql, params=None):
            if "loom.namespace_grants" not in sql:
                return []
            rows = [{"subject": "alice", "role": "admin", "granted_by": "op"}]
            if "status = 'active'" not in sql:
                rows.append({"subject": "mallory", "role": "admin", "granted_by": "mallory"})
            return rows

        async def query_one(self, sql, params=None):
            return None

        async def insert(self, *a, **k):
            return None

    async def _no_types(org, namespace):
        return []

    monkeypatch.setattr(ns_handlers, "get_client", lambda: _CH())
    monkeypatch.setattr(types, "list_type_configs", _no_types)
    monkeypatch.setattr(registry, "resolve_subject", lambda *a: "alice")
    out = asyncio.run(
        ns_handlers.handle_namespace_snapshot(NamespaceSnapshotInput(org="o", namespace="n"))
    )
    assert [g.subject for g in out.grants] == ["alice"]


def test_snapshot_audits_each_high_row_it_carries(monkeypatch) -> None:
    """A snapshot carries high rows on purpose, for a restore; each one is audited as a key
    read of a high row is."""
    import asyncio

    import server.identity.app.namespace as registry
    import server.identity.app.namespace_snapshot as ns_handlers
    import server.memory.app.namespace_types as types
    from server.identity.api.models import NamespaceSnapshotInput
    from server.memory.app import snapshot_rows
    from server.observability import audit

    def row(key: str, sensitivity: str) -> dict:
        return {
            "memory_id": str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
            "agent": "a",
            "user_id": "-",
            "session_id": "-",
            "run_id": "-",
            "memory_key": key,
            "memory_type": "semantic",
            "value": '"v"',
            "confidence": 1.0,
            "source_trace": "",
            "pinned": 0,
            "sensitivity": sensitivity,
        }

    class _CH:
        async def query_all(self, sql, params=None):
            return []

        async def query_one(self, sql, params=None):
            return None

        async def insert(self, *a, **k):
            return None

    async def _rows(*_a, **_k):
        return [row("plain", ""), row("vault", "high")]

    async def _no_types(org, namespace):
        return []

    audited: list[list[str]] = []

    async def _emit_many(event, **kw):
        audited.append([s["memory_key"] for s in kw["subjects"]])

    monkeypatch.setattr(ns_handlers, "get_client", lambda: _CH())
    monkeypatch.setattr(snapshot_rows, "read_namespace", _rows)
    monkeypatch.setattr(types, "list_type_configs", _no_types)
    monkeypatch.setattr(registry, "resolve_subject", lambda *a: "alice")
    monkeypatch.setattr(audit, "emit_many", _emit_many)
    out = asyncio.run(
        ns_handlers.handle_namespace_snapshot(NamespaceSnapshotInput(org="o", namespace="n"))
    )
    assert len(out.memories) == 2 and audited == [["vault"]]
