"""A row's `created_by` names the verified caller, never a value from the body.

Attribution is server-stamped (the identity rule every write path follows for
`actor`): a caller who could write `created_by` could attribute a blacklist
pattern, a policy rule or a record schema to someone else in every list that
shows it. The author is `writer_actor`'s stamp, the one every memory row's
`actor` carries (`human:` / `process:` + subject); the body's value counts
only with no principal to stamp.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.operations.base import Role

# The caller as `writer_actor` stamps a human principal.
_CALLER = {"human:alice"}


@pytest.fixture
def alice() -> Any:
    set_current_auth(
        AuthInfo(
            subject="alice",
            org="o",
            namespaces=("n",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    yield
    set_current_auth(None)


async def test_blacklist_add_stamps_the_caller(
    alice: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_blacklist import BlacklistAddInput
    from server.memory.app import blacklist as h

    rows: list[dict[str, Any]] = []

    async def _insert(row: dict[str, Any]) -> None:
        rows.append(row)

    async def _bump(**_k: Any) -> None:
        return None

    monkeypatch.setattr(h.blacklist_repo, "insert_row", _insert)
    monkeypatch.setattr(h, "_bump_version", _bump)
    monkeypatch.setattr(h, "invalidate_cache", lambda: None)
    await h.handle_blacklist_add(
        BlacklistAddInput(org="o", namespace="n", pattern="p", created_by="mallory")
    )
    assert rows and {r["created_by"] for r in rows} <= _CALLER


async def test_policy_set_stamps_the_caller(alice: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.api.models_policy import PolicySetInput
    from server.identity.app import history_policy_ops as h

    rows: list[dict[str, Any]] = []

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    async def _upsert(row: dict[str, Any]) -> None:
        rows.append(row)

    monkeypatch.setattr(h.policy_repo, "get_rule_row", _none)
    monkeypatch.setattr(h.policy_repo, "upsert_rule", _upsert)
    monkeypatch.setattr(h, "_audit_widened", _none)
    monkeypatch.setattr(h._policy, "invalidate", lambda *a: None)
    await h.handle_policy_set(
        PolicySetInput(
            org="o",
            namespace="n",
            policy_id="p1",
            effect="forbid",
            actions=["memory.delete"],
            condition={"op": "breadth_within", "within": "1h", "cmp": "gt", "fraction": 0.05},
            created_by="mallory",
        )
    )
    assert rows and {r["created_by"] for r in rows} <= _CALLER


async def test_memory_schema_register_stamps_the_caller(
    alice: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_records import MemorySchemaRegisterInput
    from server.memory.app import records as h

    inserted: dict[str, list[dict[str, Any]]] = {}

    class _CH:
        async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
            inserted.setdefault(table, []).extend(rows)

        async def query_all(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(h, "get_client", lambda: _CH())
    await h.handle_memory_schema_register(
        MemorySchemaRegisterInput(
            org="o",
            namespace="n",
            name="ticket",
            fields=[{"name": "title", "type": "string"}],
            created_by="mallory",
        )
    )
    rows = inserted["loom.memory_schemas"]
    assert rows and {r["created_by"] for r in rows} <= _CALLER


async def test_memory_schema_approve_stamps_the_reviewer(
    alice: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_records import MemorySchemaDecisionInput
    from server.memory.app import records as h

    inserted: list[dict[str, Any]] = []

    class _CH:
        async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
            inserted.extend(rows)

    async def _row(*_a: Any) -> dict[str, Any]:
        return {"schema_id": "00000000-0000-4000-8000-000000000001", "description": ""}

    monkeypatch.setattr(h, "get_client", lambda: _CH())
    monkeypatch.setattr(h, "_schema_by_name", _row)
    await h.handle_memory_schema_approve(
        MemorySchemaDecisionInput(
            org="o", namespace="n", name="ticket", decision="approve", reviewed_by="mallory"
        )
    )
    assert inserted and {r["created_by"] for r in inserted} <= _CALLER


async def test_with_auth_off_the_body_names_the_author(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_blacklist import BlacklistAddInput
    from server.memory.app import blacklist as h

    rows: list[dict[str, Any]] = []

    async def _insert(row: dict[str, Any]) -> None:
        rows.append(row)

    async def _bump(**_k: Any) -> None:
        return None

    monkeypatch.setattr(h.blacklist_repo, "insert_row", _insert)
    monkeypatch.setattr(h, "_bump_version", _bump)
    monkeypatch.setattr(h, "invalidate_cache", lambda: None)
    set_current_auth(None)
    await h.handle_blacklist_add(
        BlacklistAddInput(org="o", namespace="n", pattern="p", created_by="ops-script")
    )
    assert [r["created_by"] for r in rows] == ["ops-script"]


async def test_a_service_account_is_stamped_as_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_blacklist import BlacklistAddInput
    from server.memory.app import blacklist as h

    rows: list[dict[str, Any]] = []

    async def _insert(row: dict[str, Any]) -> None:
        rows.append(row)

    async def _bump(**_k: Any) -> None:
        return None

    monkeypatch.setattr(h.blacklist_repo, "insert_row", _insert)
    monkeypatch.setattr(h, "_bump_version", _bump)
    monkeypatch.setattr(h, "invalidate_cache", lambda: None)
    set_current_auth(
        AuthInfo(
            subject="sa:bot",
            org="o",
            namespaces=("n",),
            roles=(Role.WRITER,),
            is_service_account=True,
        )
    )
    try:
        await h.handle_blacklist_add(
            BlacklistAddInput(org="o", namespace="n", pattern="p", created_by="mallory")
        )
    finally:
        set_current_auth(None)
    assert [r["created_by"] for r in rows] == ["process:sa:bot"]
