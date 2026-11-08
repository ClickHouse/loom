"""`namespace.register` keeps every setting a re-register omits: the stored value carries
forward, a fresh row takes the defaults, and an explicit value always wins. `tags` are the
one field replaced whole; `conflict_threshold` tells an omitted value (keep) from an
explicit null (inherit the server's threshold)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.identity.api.models import NamespaceRegisterInput
from server.identity.app import namespace as h
from server.identity.app.namespace_autoregister import AUTO_REGISTER_TAG

STORED: dict[str, Any] = {
    "description": "the team's facts",
    "released_aliases": ["mal", "victim@corp"],
    "active": False,
    "visibility": "public",
    "consolidation_enabled": True,
    "daily_cost_cap_usd": 5.0,
    "requires_approval": True,
    "retention_days": 30,
    "warm_days": 7,
    "conflict_threshold": 0.9,
    "rerank_endpoint": "https://rerank.example/v1",
    "rerank_prompt_template": "rank {query}",
    "classify_endpoint": "https://classify.example/v1",
    "classify_prompt_template": "classify {text}",
    "default_conflict_policy": "manual_review",
}
FRESH: dict[str, Any] = {
    "description": "",
    "active": True,
    "visibility": "private",
    "consolidation_enabled": False,
    "daily_cost_cap_usd": 0.0,
    "requires_approval": False,
    "retention_days": 0,
    "warm_days": 0,
    "conflict_threshold": None,
    "rerank_endpoint": "",
    "rerank_prompt_template": "",
    "classify_endpoint": "",
    "classify_prompt_template": "",
    "default_conflict_policy": "auto_reinforce",
}
NOW = datetime(2026, 9, 28, 12, 0, 0)


def _existing(**over: Any) -> dict[str, Any]:
    return {**STORED, "tags": ["qa"], "conversation_logging_enabled": False, **over}


def _row(existing: dict[str, Any] | None, **kw: Any) -> dict[str, Any]:
    inp = NamespaceRegisterInput(org="o", namespace="n", owner_email="o@x", **kw)
    return h.registry_row(inp, existing, NOW)


@pytest.mark.parametrize("field", sorted(STORED))
def test_an_omitted_setting_keeps_the_stored_value(field: str) -> None:
    assert _row(_existing())[field] == STORED[field]


@pytest.mark.parametrize("field", sorted(FRESH))
def test_a_fresh_row_takes_the_default(field: str) -> None:
    assert _row(None)[field] == FRESH[field]


@pytest.mark.parametrize("field", sorted(FRESH))
def test_an_explicit_value_wins_over_the_stored_one(field: str) -> None:
    assert _row(_existing(), **{field: FRESH[field]})[field] == FRESH[field]


@pytest.mark.parametrize(
    ("field", "stored"),
    [("conflict_threshold", 0.9), ("capture_generation_io", False), ("coverage_sweep", True)],
)
def test_an_explicit_null_inherits_and_an_omitted_one_keeps(field: str, stored: Any) -> None:
    assert _row(_existing(**{field: stored}), **{field: None})[field] is None
    assert _row(_existing(**{field: stored}))[field] == stored
    assert _row(None)[field] is None


def test_tags_are_replaced_whole() -> None:
    assert _row(_existing())["tags"] == []
    assert _row(_existing(), tags=["x"])["tags"] == ["x"]


def test_a_restricted_row_stays_private() -> None:
    row = _row(_existing(classification="restricted"), visibility="public")
    assert row["visibility"] == "private"


class _CH:
    def __init__(self, stored: dict[str, Any]) -> None:
        self.stored = stored
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        return dict(self.stored) if "FROM loom.namespaces" in sql else None

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[Any]:
        return []  # no account ties another spelling to the caller

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, [dict(r) for r in rows]))


@pytest.mark.asyncio
async def test_the_handler_writes_the_whole_stored_row_forward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-register naming one setting writes every other stored column back, including
    the columns the form never carries."""
    stored = _existing(
        owner_email="o@x",
        tags=[AUTO_REGISTER_TAG],  # the owner may complete an auto-registered row
        classification="standard",
        join_policy="closed",
        retrieval_defaults='{"k": 3}',
        duplicate_threshold=0.93,
        contact_email="o@x",
        password_hash="hash",
        created_at=datetime(2025, 1, 2),
    )
    ch = _CH(stored)
    monkeypatch.setattr(h, "get_client", lambda: ch)
    monkeypatch.setattr(h, "resolve_subject", lambda *_a, **_k: "o@x")
    out = await h.handle_namespace_register(
        NamespaceRegisterInput(
            org="o", namespace="n", owner_email="o@x", tags=[AUTO_REGISTER_TAG], warm_days=3
        )
    )
    [(table, [row])] = ch.inserts
    assert table == "loom.namespaces"
    assert row["warm_days"] == 3
    carried = {k: v for k, v in stored.items() if k != "warm_days"}
    assert {k: row[k] for k in carried} == carried
    assert out.visibility == "public", "the answer names the stored visibility"


async def test_a_new_namespace_may_not_take_an_sso_account_name(monkeypatch) -> None:
    """With JIT on, `u-…` names are the ones single sign-on creates from an
    email: made first by someone else, the person's first sign-in would land
    in it. (Without JIT the prefix is an ordinary name.)"""
    from server.core.errors import ValidationError

    async def no_row(*a: Any, **kw: Any) -> None:
        return None

    from server.settings import settings

    monkeypatch.setattr(settings, "oidc_jit_provision", True)
    monkeypatch.setattr(h, "get_client", lambda: object())
    monkeypatch.setattr(h, "resolve_subject", lambda: "mallory")
    monkeypatch.setattr(h.namespaces_repo, "fetch_row", no_row)
    with pytest.raises(ValidationError, match="reserved"):
        await h.handle_namespace_register(
            NamespaceRegisterInput(org="o", namespace="u-bob-1234abcd", owner_email="m@x")
        )


def test_the_same_owner_keeps_the_login() -> None:
    existing = _existing(owner_email="o@x", contact_email="o@x.io", password_hash="h")
    row = _row(existing)
    assert (row["contact_email"], row["password_hash"]) == ("o@x.io", "h")


@pytest.mark.parametrize(
    ("login", "owner"),
    [
        ({"contact_email": "bob@x.io", "password_hash": "h"}, "eve"),
        ({"contact_email": "bob@x.io", "password_hash": "h"}, "bob@x.io"),  # its own email
        ({"contact_email": "", "password_hash": "claim"}, "eve"),  # an agent's claim code
        # case too: the owner is the token subject, and revocations match it exactly
        ({"contact_email": "bob@x.io", "password_hash": "h"}, "BOB"),
    ],
)
def test_an_account_with_a_login_does_not_change_owner(
    monkeypatch: pytest.MonkeyPatch, login: dict[str, str], owner: str
) -> None:
    import asyncio

    from server.core.errors import PreconditionFailedError

    existing = _existing(owner_email="bob", **login)
    written: list[Any] = []

    async def _fetch(*_a: Any, **_k: Any) -> dict[str, Any]:
        return existing

    async def _admin(*_a: Any, **_k: Any) -> None:
        return None

    class _CH:
        async def insert(self, table: str, rows: list[Any], **_k: Any) -> None:
            written.append(rows)

    monkeypatch.setattr(h.namespaces_repo, "fetch_row", _fetch)
    monkeypatch.setattr(h, "require_admin_of_existing", _admin)
    monkeypatch.setattr(h, "get_client", lambda: _CH())
    monkeypatch.setattr(h, "resolve_subject", lambda: "bob")
    inp = NamespaceRegisterInput(org="o", namespace="n", owner_email=owner)
    with pytest.raises(PreconditionFailedError, match="owned by 'bob': send that owner_email"):
        asyncio.run(h.handle_namespace_register(inp))
    assert written == []
