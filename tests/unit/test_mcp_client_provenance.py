"""Unit-level checks for MCP client-provenance attribution.

The MCP `initialize` handshake carries `params.clientInfo.name` (Cursor /
Claude Code / custom clients). The transport slugifies it into a Loom agent
id and uses it as the agent default so writes are attributed to the actual
client rather than a blanket "claude-desktop".

These exercise the pure normalizer/resolver/cache helpers (no I/O) plus the
integrated `_caller_defaults` agent derivation. `_caller_defaults`'s
ClickHouse lookup fails open (`except Exception: pass`), so org/namespace/
agent are still returned without a live cluster — letting the agent
derivation be asserted CH-free.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.settings import settings
from server.transport.mcp_config import _DEFAULT_MCP_AGENT
from server.transport.mcp_identity import (
    _MCP_AGENT_BY_SESSION,
    _caller_defaults,
    _client_name_to_agent,
    _remember_client_for_session,
    _resolve_client_name,
)

# ---- _client_name_to_agent (pure slugifier) ----


def test_client_name_none_falls_back_to_default() -> None:
    assert _client_name_to_agent(None) == _DEFAULT_MCP_AGENT
    assert _client_name_to_agent(None) == "claude-desktop"


def test_client_name_empty_or_blank_falls_back() -> None:
    assert _client_name_to_agent("") == "claude-desktop"
    assert _client_name_to_agent("   ") == "claude-desktop"
    # A name with no alphanumerics slugs to empty → default.
    assert _client_name_to_agent("***") == "claude-desktop"


def test_client_name_slugifies() -> None:
    assert _client_name_to_agent("Cursor") == "cursor"
    assert _client_name_to_agent("Custom Chat") == "custom-chat"
    # Already-hyphenated names survive intact.
    assert _client_name_to_agent("claude-ai") == "claude-ai"
    # Runs of non-alphanumerics collapse to a single hyphen, trimmed.
    assert _client_name_to_agent("Custom Chat (loom bridge)") == "custom-chat-loom-bridge"
    assert _client_name_to_agent("  Claude Code  ") == "claude-code"


def test_client_name_truncated_to_64() -> None:
    out = _client_name_to_agent("x" * 200)
    assert out == "x" * 64


# ---- _resolve_client_name + session cache ----


def test_resolve_prefers_inline_client_info() -> None:
    req = {"method": "initialize", "params": {"clientInfo": {"name": "Cursor"}}}
    # Inline clientInfo wins even when a session has a different cached value.
    _remember_client_for_session("sess-inline", "Custom Chat")
    try:
        assert _resolve_client_name(req, "sess-inline") == "Cursor"
    finally:
        _MCP_AGENT_BY_SESSION.pop("sess-inline", None)


def test_resolve_falls_back_to_session_cache() -> None:
    _remember_client_for_session("sess-cache", "Cursor")
    try:
        call = {"method": "tools/call", "params": {"name": "loom_memory_set"}}
        assert _resolve_client_name(call, "sess-cache") == "cursor"
    finally:
        _MCP_AGENT_BY_SESSION.pop("sess-cache", None)


def test_resolve_returns_none_without_signal() -> None:
    call = {"method": "tools/call", "params": {"name": "loom_memory_set"}}
    assert _resolve_client_name(call, None) is None
    assert _resolve_client_name(call, "unknown-session") is None


def test_remember_ignores_blank_and_default() -> None:
    # No session id, or no name → no cache entry.
    _remember_client_for_session(None, "Cursor")
    _remember_client_for_session("sess-x", None)
    assert "sess-x" not in _MCP_AGENT_BY_SESSION
    # A client that slugs to the default agent isn't worth caching.
    _remember_client_for_session("sess-default", "Claude Desktop")
    assert "sess-default" not in _MCP_AGENT_BY_SESSION


# ---- _caller_defaults integrated agent derivation (CH-free) ----


class _FakeAuth:
    subject = "agent-detector"
    org = "clickhouse"


def test_caller_defaults_uses_client_name_for_agent() -> None:
    async def _run() -> None:
        cursor = await _caller_defaults(_FakeAuth(), "Cursor")
        default = await _caller_defaults(_FakeAuth(), None)
        assert cursor is not None and default is not None
        # Provenance: the resolved client becomes the agent default.
        assert cursor["agent"] == "cursor"
        assert default["agent"] == "claude-desktop"
        # org / namespace resolution is untouched by client_name.
        assert cursor["org"] == default["org"]
        assert cursor["namespace"] == default["namespace"]

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_loom_search_uses_grant_namespace_and_fans_profile_across_memberships(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grant-only namespaces must remain valid loom_search seeds.

    The synthetic loom_search path bypasses the normal registry dispatcher,
    so it has to use the same all_mine membership resolver as the concrete
    search handlers. A JWT-only fallback would incorrectly replace the
    grant-derived default namespace with the first namespace in the token.
    """

    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.transport import mcp_call, mcp_outcome

    async def _no_provisioning(**_kw: Any) -> None:
        return None

    captured: dict[str, Any] = {}

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "test", "namespace": "team-grant", "agent": "cursor"}

    async def _resolve_my_namespaces(org: str, subject: str | None = None) -> list[str]:
        captured["resolved_org"] = org
        return ["team-grant", "personal-token"]

    async def _check_authorized(
        auth: Any,
        required_role: Role,
        org: str | None = None,
        namespace: str | None = None,
        **_flags: Any,
    ) -> None:
        captured["authz"] = (required_role, org, namespace)

    class _Dump:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def model_dump(self) -> dict[str, Any]:
            return self.payload

    async def _memory_search(input_obj: Any) -> _Dump:
        captured["memory_input"] = input_obj
        return _Dump({"results": []})

    async def _knowledge_search(input_obj: Any) -> _Dump:
        captured["knowledge_input"] = input_obj
        return _Dump({"results": []})

    async def _conversation_search(input_obj: Any) -> _Dump:
        captured["conversation_input"] = input_obj
        return _Dump({"results": []})

    sections = [
        {
            "namespace": "team-grant",
            "agent": "cursor",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "location",
            "value": '"User works from Pune."',
        },
        {
            "namespace": "personal-token",
            "agent": "cursor",
            "user_id": "-",
            "session_id": "-",
            "memory_key": "timezone",
            "value": '"User prefers IST."',
        },
    ]

    class _CH:
        # The profile leg locates keys, then fetches them.
        async def query_all(
            self,
            sql: str,
            params: dict[str, Any] | None = None,
        ) -> list[dict[str, Any]]:
            if "SELECT DISTINCT" in sql:
                captured["profile_params"] = params or {}
                return [{k: r[k] for k in r if k != "value"} for r in sections]
            return sections

    monkeypatch.setattr("server.transport.mcp_call._caller_defaults", _defaults)
    monkeypatch.setattr(
        "server.auth.personal_namespace.ensure_personal_namespace_cached", _no_provisioning
    )
    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _resolve_my_namespaces)
    monkeypatch.setattr("server.auth.authz.check_authorized", _check_authorized)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _memory_search)
    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search", _knowledge_search
    )
    monkeypatch.setattr(
        "server.knowledge.app.conversations.handle_conversation_search",
        _conversation_search,
    )
    for mod in (mcp_call, mcp_outcome):
        monkeypatch.setattr(mod, "_emit_mcp_event", lambda *_, **__: None)
    # The fake CH answers the vector read with the section rows (no current
    # vector) and the fake embedder's vectors carry no meaning: keep this test
    # on the word rule it asserts the fan-out with (on-the-fly scoring would
    # otherwise score the sections from their text).
    monkeypatch.setattr(settings, "memory_profile_search_embed_cap", 0)

    auth = AuthInfo(
        subject="mcp-user",
        org="test",
        namespaces=("personal-token",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    params = {"name": "loom_search", "arguments": {"query": "user location and timezone"}}
    resp = await mcp_call._call_tool(99, params, auth=auth, client_name="Cursor")

    assert resp["result"]["isError"] is False
    assert captured["authz"] == (Role.VIEWER, "test", "team-grant")
    assert captured["memory_input"].namespace == "team-grant"
    assert captured["memory_input"].namespace_scope == "all_mine"
    assert captured["profile_params"]["ns"] == ["team-grant", "personal-token"]
    profile_hits = resp["result"]["structuredContent"]["profile"]["results"]
    assert {h["namespace"] for h in profile_hits} == {"team-grant", "personal-token"}


@pytest.mark.asyncio
async def test_loom_search_does_not_rewrite_explicit_unauthorized_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.transport import mcp_call

    async def _no_provisioning(**_kw: Any) -> None:
        return None

    captured: dict[str, Any] = {}

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "test", "namespace": "team-grant", "agent": "cursor"}

    async def _resolve_my_namespaces(org: str, subject: str | None = None) -> list[str]:
        return ["team-grant"]

    async def _check_authorized(
        auth: Any,
        required_role: Role,
        org: str | None = None,
        namespace: str | None = None,
        **_flags: Any,
    ) -> None:
        captured["authz_namespace"] = namespace
        raise PermissionError(f"cannot access {namespace}")

    async def _should_not_search(*_: Any, **__: Any) -> None:
        raise AssertionError("search handlers should not run after auth failure")

    monkeypatch.setattr("server.transport.mcp_call._caller_defaults", _defaults)
    monkeypatch.setattr(
        "server.auth.personal_namespace.ensure_personal_namespace_cached", _no_provisioning
    )
    monkeypatch.setattr("server.auth.context.resolve_my_namespaces", _resolve_my_namespaces)
    monkeypatch.setattr("server.auth.authz.check_authorized", _check_authorized)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _should_not_search)
    monkeypatch.setattr(
        "server.knowledge.app.knowledge_search.handle_knowledge_search", _should_not_search
    )
    monkeypatch.setattr(
        "server.knowledge.app.conversations.handle_conversation_search",
        _should_not_search,
    )

    auth = AuthInfo(
        subject="mcp-user",
        org="test",
        namespaces=("team-grant",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    params = {
        "name": "loom_search",
        "arguments": {"query": "deployment", "namespace": "other-team"},
    }
    resp = await mcp_call._call_tool(100, params, auth=auth, client_name="Cursor")

    assert captured["authz_namespace"] == "other-team"
    assert resp["result"]["isError"] is True
    assert resp["result"]["structuredContent"]["code"] == "AUTH_INSUFFICIENT"


async def test_caller_defaults_seeds_default_from_token_namespace_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A token's explicit namespace claim seeds the default namespace, so a
    Setup-tab service-account token (subject sa:<client>-<hash>) lands in its
    real namespace instead of a junk personal-sa-<client> one. With no saved
    default and no grants (query_one -> None), the claim's first real entry
    stands; a claim of only "*" falls back to the personal slug.
    """
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.transport import mcp_identity

    class _CH:
        async def query_one(self, *a: Any, **k: Any) -> None:
            return None  # no saved user_state default, no DB grants

        async def query_all(self, *a: Any, **k: Any) -> list[Any]:
            return []  # no restricted namespace

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())

    claimed = AuthInfo(
        subject="sa:claude-desktop-abc123",
        org="clickhouse",
        namespaces=("alice", "core-tracker"),
        roles=(Role.WRITER,),
        is_service_account=True,
    )
    out = await mcp_identity._caller_defaults(claimed)
    assert out is not None and out["namespace"] == "alice"

    wildcard_only = AuthInfo(
        subject="sa:cursor-xyz789",
        org="clickhouse",
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )
    out2 = await mcp_identity._caller_defaults(wildcard_only)
    assert out2 is not None and out2["namespace"] == "personal-sa-cursor-xyz789"


# ---- the agent rides the server-assigned session id ----


def test_new_session_id_carries_the_slug() -> None:
    from server.transport.mcp_identity import new_session_id

    sid = new_session_id("Claude Code")
    head, _, slug = sid.partition(".")
    assert len(head) == 32 and slug == "claude-code"
    # The default agent needs no suffix; an absent name mints a bare id.
    assert "." not in new_session_id(None)
    assert "." not in new_session_id("Claude Desktop")
    assert all(0x21 <= ord(c) <= 0x7E for c in sid)  # MCP: visible ASCII


def test_a_worker_that_missed_initialize_reads_the_slug_from_the_id() -> None:
    """Four uvicorn workers, one per-process cache: the tools/call that lands
    on a worker which never saw `initialize` must still resolve the client, so
    one Claude Code session never writes as claude-desktop and reads history
    as claude-code."""
    from server.transport.mcp_identity import new_session_id

    sid = new_session_id("Claude Code")
    assert sid not in _MCP_AGENT_BY_SESSION
    call = {"method": "tools/call", "params": {"name": "loom_memory_history"}}
    assert _client_name_to_agent(_resolve_client_name(call, sid)) == "claude-code"


def test_foreign_session_ids_are_not_parsed() -> None:
    call = {"method": "tools/call", "params": {"name": "loom_memory_set"}}
    # A client-chosen id with a dot, a short head, or a non-slug tail names
    # no agent; the cache miss stays a miss.
    assert _resolve_client_name(call, "abc.cursor") is None
    assert _resolve_client_name(call, "z" * 32 + ".cursor") is None
    assert _resolve_client_name(call, "a" * 32 + ".Cursor App") is None
    assert _resolve_client_name(call, "a" * 32 + ".") is None


@pytest.mark.parametrize("op_name", ["memory.history", "memory.related"])
def test_key_reads_default_to_every_writer_in_the_namespace(
    op_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """History / related name a KEY; under the caller's own
    slug a key another client wrote answered `{}` / source_not_found. An
    explicit, different agent still narrows."""
    import dataclasses

    import server.transport.mcp_call as mcp_call

    seen: list[str] = []

    async def _handler(inp: Any) -> Any:
        seen.append(inp.agent)
        return op.output_model.model_construct()

    op = mcp_call.TOOLS_BY_NAME[f"loom_{op_name.replace('.', '_')}"]
    monkeypatch.setitem(
        mcp_call.TOOLS_BY_NAME,
        f"loom_{op_name.replace('.', '_')}",
        dataclasses.replace(op, handler=_handler),
    )

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "n", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    name = f"loom_{op_name.replace('.', '_')}"
    for extra in ({}, {"agent": "codex"}):
        asyncio.run(
            mcp_call._call_tool(
                1,
                {"name": name, "arguments": {"memory_key": "k", **extra}},
                auth=None,
            )
        )
    assert seen == ["*", "codex"]


def test_codex_mcp_client_writes_as_its_hooks_agent() -> None:
    # Codex's MCP clientInfo.name is `codex-mcp-client`; its plugin hooks write
    # as `codex`. Both must land under one agent.
    assert _client_name_to_agent("codex-mcp-client") == "codex"
    assert _client_name_to_agent("Codex MCP Client") == "codex"
    assert _client_name_to_agent("codex") == "codex"
