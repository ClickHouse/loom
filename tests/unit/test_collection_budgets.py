"""A collection's admins can change its spend policy without rewriting its identity."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import pydantic
import pytest

from server.core.errors import ValidationError
from server.identity.api.models import NamespaceRegisterInput
from server.identity.api.models_collections import CollectionBudgetSetInput
from server.identity.app import collection_budgets as h
from server.identity.app.namespace import handle_namespace_register
from server.observability import budget
from server.operations.base import Role
from server.operations.registry import ALL_OPERATIONS


def test_a_collection_has_an_audited_admin_budget_operation() -> None:
    ops = {op.name: op for op in ALL_OPERATIONS}
    assert "collection.budget_set" in ops
    op = ops["collection.budget_set"]
    assert op.required_role == Role.ADMIN and op.audit and op.idempotent
    assert not op.skip_namespace_authz and not op.deployment_scoped


class Store:
    def __init__(self) -> None:
        self.held = False
        self.live = True
        self.inbox_checks: list[tuple[str, str, str]] = []
        self.writes: list[dict[str, Any]] = []
        self.row: dict[str, Any] | None = {
            "org": "o",
            "namespace": "col-a",
            "kind": "collection",
            "active": True,
            "daily_cost_cap_usd": 2.0,
            "daily_user_cost_cap_usd": 1.0,
            "budget_alert_inbox": "",
            "updated_at": datetime(2026, 1, 2),
            "created_at": datetime(2025, 1, 1),
            "classification": "restricted",
            "visibility": "private",
            "title": "Plans",
            "tags": ["a"],
            "owner_email": "owner",
            "password_hash": "hash",
            "capture_generation_io": False,
            "future_column": {"must": "remain"},
        }

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert self.held and table == "loom.namespaces"
        [row] = rows
        self.writes.append(dict(row))
        self.row = dict(row)

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        assert params["org"] == "o" and params["ns"] == "col-a"
        if "FROM loom.namespaces" in sql:
            return dict(self.row or {})
        # Offline cost rows prove the existing guard consumes the new stored cap.
        return {"spent": 0.5, "own": 0, "user_spent": 0.2, "user_own": 0}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Store:
    s = Store()

    @asynccontextmanager
    async def held(org: str, ns: str) -> Any:
        assert (org, ns) == ("o", "col-a") and not s.held
        s.held = True
        try:
            yield
        finally:
            s.held = False

    async def fetch(org: str, ns: str, **kw: Any) -> dict[str, Any] | None:
        assert s.held and (org, ns) == ("o", "col-a")
        return dict(s.row) if s.row else None

    async def live(org: str, ns: str, **kw: Any) -> dict[str, Any] | None:
        assert s.held and (org, ns) == ("o", "col-a")
        return {"namespace": ns} if s.live else None

    async def inbox(org: str, ns: str, name: str) -> bool:
        s.inbox_checks.append((org, ns, name))
        return name == "owned"

    monkeypatch.setattr(h, "account_change", held)
    monkeypatch.setattr(h.namespaces_repo, "fetch_row", fetch)
    monkeypatch.setattr(h.collections_repo, "fetch", live)
    monkeypatch.setattr(h, "inbox_owned_by", inbox)
    monkeypatch.setattr(h, "get_client", lambda: s)
    monkeypatch.setattr(h, "naive_utc_now_seconds", lambda: datetime(2026, 1, 1))
    return s


def request(**fields: Any) -> CollectionBudgetSetInput:
    return CollectionBudgetSetInput(org="o", namespace="col-a", **fields)


async def test_patch_preserves_every_unrelated_column_and_never_moves_the_timestamp_back(
    store: Store,
) -> None:
    before = dict(store.row or {})
    out = await h.handle_collection_budget_set(request(daily_cost_cap_usd=0.25))
    assert out.changed and out.daily_cost_cap_usd == 0.25
    assert out.daily_user_cost_cap_usd == 1.0 and out.budget_alert_inbox == ""
    assert store.row == {**before, "daily_cost_cap_usd": 0.25}
    assert not store.held and len(store.writes) == 1


async def test_user_cap_and_owned_inbox_can_be_changed_then_restored(store: Store) -> None:
    before = dict(store.row or {})
    changed = await h.handle_collection_budget_set(
        request(daily_user_cost_cap_usd=0.125, budget_alert_inbox="owned")
    )
    assert changed.daily_cost_cap_usd == 2.0 and changed.daily_user_cost_cap_usd == 0.125
    assert store.inbox_checks == [("o", "col-a", "owned")]
    await h.handle_collection_budget_set(request(daily_user_cost_cap_usd=1, budget_alert_inbox=""))
    assert store.row == before


async def test_an_identical_patch_does_not_rewrite_the_directory(store: Store) -> None:
    out = await h.handle_collection_budget_set(request(daily_cost_cap_usd=2))
    assert not out.changed and not store.writes


async def test_foreign_or_missing_inbox_is_refused_without_changing_policy(store: Store) -> None:
    before = dict(store.row or {})
    with pytest.raises(ValidationError, match="owned by this collection"):
        await h.handle_collection_budget_set(
            request(daily_cost_cap_usd=0.25, budget_alert_inbox="foreign")
        )
    assert store.inbox_checks == [("o", "col-a", "foreign")]
    assert not store.writes and store.row == before


@pytest.mark.parametrize("state", ["missing", "inactive", "wrong_kind", "retired_tree"])
async def test_missing_inactive_or_retired_collections_are_not_resurrected(
    store: Store, state: str
) -> None:
    if state == "missing":
        store.row = None
    elif state == "retired_tree":
        store.live = False
    else:
        assert store.row
        store.row["active" if state == "inactive" else "kind"] = (
            False if state == "inactive" else "agent"
        )
    with pytest.raises(ValidationError, match="active collection"):
        await h.handle_collection_budget_set(request(daily_cost_cap_usd=0.25))
    assert not store.writes


@pytest.mark.parametrize("ns", ["account", "col-me-12345678"])
async def test_account_and_personal_names_cannot_enter_collection_policy_writes(
    store: Store, ns: str
) -> None:
    with pytest.raises(ValidationError, match="non-personal collection"):
        await h.handle_collection_budget_set(
            CollectionBudgetSetInput(org="o", namespace=ns, daily_cost_cap_usd=1)
        )
    assert not store.writes


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"daily_cost_cap_usd": None},
        {"daily_user_cost_cap_usd": None},
        {"budget_alert_inbox": None},
    ],
)
async def test_empty_and_null_patches_do_not_silently_clear_policy(
    store: Store, patch: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError, match="at least one budget field"):
        await h.handle_collection_budget_set(request(**patch))
    assert not store.writes


@pytest.mark.parametrize("field", ["daily_cost_cap_usd", "daily_user_cost_cap_usd"])
@pytest.mark.parametrize("value", [-1, float("inf"), float("-inf"), float("nan")])
def test_caps_are_finite_nonnegative_dollar_values(field: str, value: float) -> None:
    with pytest.raises(pydantic.ValidationError):
        request(**{field: value})


async def test_zero_restores_existing_default_semantics_and_positive_caps_reach_the_guard(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "default_daily_cost_cap_usd", 3)
    monkeypatch.setattr(settings, "default_daily_user_cost_cap_usd", 2)
    await h.handle_collection_budget_set(request(daily_cost_cap_usd=0, daily_user_cost_cap_usd=0))
    snapshot = await budget.budget_snapshot("o", "col-a", user_id="named", ch=store)
    assert (snapshot.cap, snapshot.cap_source, snapshot.user_cap, snapshot.user_cap_source) == (
        3,
        "global",
        2,
        "global_user",
    )
    assert not snapshot.over("named", "", 1)
    await h.handle_collection_budget_set(
        request(daily_cost_cap_usd=0.25, daily_user_cost_cap_usd=0.125)
    )
    snapshot = await budget.budget_snapshot("o", "col-a", user_id="named", ch=store)
    assert snapshot.over("named", "", 1)
    assert (snapshot.cap_source, snapshot.user_cap_source) == ("namespace", "namespace_user")


async def test_namespace_register_still_refuses_the_original_collection_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("server.identity.app.namespace.resolve_subject", lambda: "owner")
    with pytest.raises(ValidationError, match="collection operations only"):
        await handle_namespace_register(
            NamespaceRegisterInput(
                org="o",
                namespace="col-a",
                owner_email="owner",
                daily_cost_cap_usd=0.05,
                daily_user_cost_cap_usd=0.025,
            )
        )
