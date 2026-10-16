"""An unaddressed MCP call never lands in a QA/code plane.

Even when the owner's saved dashboard default is a QA namespace (`qa-clickhouse`)
and the grant ranking puts an admin grant on a QA namespace first, an MCP
`memory.set` / `profile.append` / `working_memory.set` / `knowledge.add` with
no namespace writes to the owner's memory namespace (`alice`), not the shared
QA namespace."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.transport.mcp_identity as mi

KINDS = {
    "alice": "memory",
    "qa-clickhouse": "qa",
    "qa-example-private": "qa",
    "code-loom": "code",
    "code-clickhouse": "code",
    "agent-box-claude-code": "memory",
}


class _Auth:
    def __init__(self, subject: str, namespaces: tuple[str, ...] = (), sa: bool = False) -> None:
        self.subject = subject
        self.org = "clickhouse"
        self.namespaces = namespaces
        self.is_service_account = sa
        self.issued_at = None

    def can_write(self, org: str, ns: str) -> bool:
        return ns in self.namespaces


class _CH:
    def __init__(self, saved: str | None, grants: list[str]) -> None:
        self.saved, self.grants = saved, grants

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        if "user_state" in sql:
            return {"namespace": self.saved} if self.saved else None
        if "namespace_grants" in sql:  # the saved default's grant check
            return {"role": "admin"} if (params or {}).get("ns") in self.grants else None
        return None  # service_accounts lookup etc.

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [{"namespace": n, "role": "admin"} for n in self.grants]


@pytest.fixture()
def _wired(monkeypatch: pytest.MonkeyPatch):
    def _wire(ch: _CH) -> None:
        import server.auth.aliases as aliases
        import server.core.ch as chmod
        import server.identity.app.namespace_kinds as nk

        async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
            return [subject]

        async def _kind_map(org: str) -> dict[str, str]:
            return dict(KINDS)

        monkeypatch.setattr(chmod, "get_client", lambda: ch)
        monkeypatch.setattr(aliases, "subject_aliases", _aliases)
        monkeypatch.setattr(nk, "kind_map", _kind_map)
        from server.settings import settings

        monkeypatch.setattr(settings, "identity_provider", "trust-network")

    return _wire


def _defaults(auth: _Auth) -> dict[str, str]:
    out = asyncio.run(mi._caller_defaults(auth, "claude-code"))
    assert out is not None
    return out


# The grant ranking puts the QA/code admin grants first (role tier, recency).
_OWNER_GRANTS = [
    "qa-clickhouse",
    "qa-example-private",
    "code-loom",
    "code-clickhouse",
    "alice",
]


def test_a_saved_qa_default_is_where_the_owner_looked_not_where_memory_goes(_wired) -> None:
    _wired(_CH(saved="qa-clickhouse", grants=_OWNER_GRANTS))
    token = _Auth(
        "alice",
        ("alice", "code-loom", "code-clickhouse", "qa-clickhouse", "qa-example-private"),
    )
    assert _defaults(token)["namespace"] == "alice"


def test_the_grant_ranking_skips_planes_and_prefers_the_callers_own(_wired) -> None:
    _wired(_CH(saved=None, grants=_OWNER_GRANTS))
    assert _defaults(_Auth("alice"))["namespace"] == "alice"


def test_a_saved_memory_default_still_wins(_wired) -> None:
    _wired(_CH(saved="agent-box-claude-code", grants=_OWNER_GRANTS + ["agent-box-claude-code"]))
    assert _defaults(_Auth("alice"))["namespace"] == "agent-box-claude-code"


def test_the_self_signup_service_account_keeps_its_own_namespace(_wired) -> None:
    _wired(_CH(saved=None, grants=["agent-box-claude-code"]))
    sa = _Auth("sa:agent-box-claude-code-1234", ("agent-box-claude-code",), sa=True)
    assert _defaults(sa)["namespace"] == "agent-box-claude-code"


def test_only_planes_reachable_falls_to_the_personal_slug_never_a_plane(_wired) -> None:
    _wired(_CH(saved="qa-clickhouse", grants=["qa-clickhouse", "code-loom"]))
    ns = _defaults(_Auth("viewer@example.com", ("code-loom",)))["namespace"]
    assert ns == "personal-viewer" and mi.is_memory_home(KINDS, ns)


def test_memory_home_helpers() -> None:
    rows = [{"namespace": n} for n in _OWNER_GRANTS]
    assert mi.memory_home_from_grants(rows, KINDS, "alice") == "alice"
    assert mi.memory_home_from_grants(rows[:4], KINDS, "alice") == ""
    assert mi.is_memory_home(KINDS, "unknown-ns") is True  # directory default is memory
    assert mi.is_memory_home(KINDS, "code-loom") is False
