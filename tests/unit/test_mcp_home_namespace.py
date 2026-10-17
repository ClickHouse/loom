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

import server.identity.app.caller_home as home
import server.transport.mcp_identity as mi

KINDS = {
    "alice": "memory",
    "qa-clickhouse": "qa",
    "qa-example-private": "qa",
    "code-loom": "code",
    "code-clickhouse": "code",
    "agent-box-claude-code": "memory",
}
RESTRICTED = frozenset({"secret-team"})


class _Auth:
    def __init__(
        self,
        subject: str,
        namespaces: tuple[str, ...] = (),
        sa: bool = False,
        creator: str = "",
    ) -> None:
        self.subject = subject
        self.org = "clickhouse"
        self.namespaces = namespaces
        self.ns_roles: tuple[tuple[str, str], ...] = ()
        self.is_service_account = sa
        self.creator = creator
        self.issued_at = None

    def can_access(self, org: str, ns: str) -> bool:
        return ns in self.namespaces or "*" in self.namespaces

    def can_write(self, org: str, ns: str) -> bool:
        return self.can_access(org, ns)


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
        import server.identity.app.namespace_classification as nc
        import server.identity.app.namespace_kinds as nk

        async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
            return [subject]

        async def _kind_map(org: str) -> dict[str, str]:
            return dict(KINDS)

        async def _restricted_set(org: str) -> frozenset[str]:
            return RESTRICTED

        monkeypatch.setattr(chmod, "get_client", lambda: ch)
        monkeypatch.setattr(aliases, "subject_aliases", _aliases)
        monkeypatch.setattr(nk, "kind_map", _kind_map)
        monkeypatch.setattr(nc, "restricted_set", _restricted_set)
        from server.settings import settings

        monkeypatch.setattr(settings, "identity_provider", "trust-network")

    return _wire


def _defaults(auth: Any) -> dict[str, str]:
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
    assert ns == "personal-viewer" and home.is_memory_home(KINDS, ns)


def test_memory_home_helpers() -> None:
    rows = [{"namespace": n} for n in _OWNER_GRANTS]
    assert home.memory_home_from_grants(rows, KINDS, "alice") == "alice"
    assert home.memory_home_from_grants(rows[:4], KINDS, "alice") == ""
    assert home.is_memory_home(KINDS, "unknown-ns") is True  # directory default is memory
    assert home.is_memory_home(KINDS, "code-loom") is False


class _SetupTokenCH(_CH):
    """A Setup-page `*` token: only its owner (`alice` unless named) holds
    grants and a saved default. Its `service_accounts` row names `row_creator`,
    which the default must not read: the token's signed `creator` decides."""

    def __init__(
        self,
        saved: str | None,
        grants: list[str],
        owner: str = "alice",
        row_creator: str = "sa:parent-bot",
    ) -> None:
        super().__init__(saved, grants)
        self.owner, self.row_creator = owner, row_creator
        self.sql: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        self.sql.append(sql)
        params = params or {}
        if "service_accounts" in sql:
            return {"created_by": self.row_creator}
        if "user_state" in sql:
            mine = self.saved and params["sub"] == self.owner
            return {"namespace": self.saved} if mine else None
        if "namespace_grants" in sql:
            mine = self.owner in params["subs"] and params.get("ns") in self.grants
            return {"role": "writer"} if mine else None
        return None

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if self.owner not in (params or {}).get("subs", []):
            return []
        return [{"namespace": n, "role": "writer"} for n in self.grants]


def _setup_token(creator: str = "alice") -> _Auth:
    return _Auth("sa:cursor-ab12cd", ("*",), sa=True, creator=creator)


def test_a_wildcard_setup_token_defaults_to_its_creators_home(_wired) -> None:
    ch = _SetupTokenCH(saved=None, grants=["code-loom", "alice"])
    _wired(ch)
    assert _defaults(_setup_token())["namespace"] == "alice"
    assert not any("service_accounts" in q for q in ch.sql), "the signed claim, no row read"


def test_a_wildcard_setup_token_follows_its_creators_saved_default(_wired) -> None:
    _wired(_SetupTokenCH(saved="agent-box-claude-code", grants=["alice", "agent-box-claude-code"]))
    assert _defaults(_setup_token())["namespace"] == "agent-box-claude-code"


def test_a_token_minted_by_another_service_account_follows_the_signed_creator(_wired) -> None:
    # The row says another service account made it; the gate checks alice's grants.
    _wired(_SetupTokenCH(saved="agent-box-claude-code", grants=["agent-box-claude-code"]))
    assert _defaults(_setup_token())["namespace"] == "agent-box-claude-code"


def test_a_self_signup_agent_token_keeps_its_namespace_not_its_claimers_pick(_wired) -> None:
    # Self-signup: the row's created_by is the agent account, whose claimer
    # saved a team pick there. The token carries no creator and claims its own.
    ch = _SetupTokenCH(
        saved="team-x",
        grants=["team-x"],
        owner="agent-box-claude-code",
        row_creator="agent-box-claude-code",
    )
    _wired(ch)
    agent = _Auth("sa:agent-box-claude-code-1a2b3c", ("agent-box-claude-code",), sa=True)
    assert _defaults(agent)["namespace"] == "agent-box-claude-code"


def test_a_saved_restricted_namespace_is_no_default_for_a_wildcard_token(_wired) -> None:
    _wired(_SetupTokenCH(saved="secret-team", grants=["secret-team", "alice"]))
    assert _defaults(_setup_token())["namespace"] == "alice"


def test_the_ranking_skips_a_restricted_namespace_the_token_does_not_name(_wired) -> None:
    _wired(_SetupTokenCH(saved=None, grants=["secret-team", "agent-box-claude-code"]))
    assert _defaults(_setup_token())["namespace"] == "agent-box-claude-code"


def _token(namespaces: tuple[str, ...], **kw: Any) -> Any:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    base: dict[str, Any] = {"subject": "sa:cursor-ab12cd", "org": "clickhouse", "creator": "alice"}
    return AuthInfo(
        **{**base, **kw},
        namespaces=namespaces,
        roles=(Role.WRITER,),
        is_service_account=True,
    )


def test_a_token_claiming_only_a_qa_plane_has_no_default_namespace(_wired) -> None:
    # Setup token for `qa-clickhouse` only: the creator's memory grant and
    # personal namespace are outside the claim, so neither is its default.
    _wired(_SetupTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    assert _defaults(_token(("qa-clickhouse",)))["namespace"] == ""


def test_an_unaddressed_write_from_a_qa_only_token_asks_for_a_namespace(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live shape: no namespace is filled in, so the call fails validation
    naming `namespace`, not AUTH_INSUFFICIENT on a personal namespace."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    _wired(_SetupTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    op = OPERATIONS_BY_NAME["memory.set"]
    args: dict[str, Any] = {"memory_key": "k", "value": "v"}
    token = _token(("qa-clickhouse",))

    async def _call() -> Any:
        request_id, _ = await mcp_call._bind_caller_identity(op, args, token, "claude-code")
        return mcp_call._validated_input(1, op, args, request_id)

    out = asyncio.run(_call())["result"]["structuredContent"]
    assert "namespace" not in args
    assert out["code"] == "VALIDATION_FAILED"
    assert ["namespace"] in [list(e["loc"]) for e in out["errors"]]
    assert "must name its `namespace`" in asyncio.run(mi._caller_default_context(token))


def _primer_text(token: Any) -> str:
    from server.transport import mcp_primer

    return asyncio.run(mcp_primer._loom_primer_text(token, include_profile=False))


def _coding_line(token: Any) -> str:
    from server.transport import mcp_primer

    return asyncio.run(mcp_primer._coding_workspace_line(token))


def _coding_text(token: Any) -> str:
    from server.transport import mcp_primer

    return asyncio.run(mcp_primer._coding_instructions(token, include_profile=False))


@pytest.mark.parametrize(
    "render",
    [lambda t: asyncio.run(mi._caller_default_context(t)), _coding_line, _primer_text],
    ids=["default_context", "coding_workspace_line", "loom_primer_text"],
)
def test_no_default_namespace_is_never_rendered_as_an_empty_one(_wired, render) -> None:
    _wired(_SetupTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    text = render(_token(("qa-clickhouse",)))
    assert "namespace" in text
    for phrase in ("``", "filled in when a call names none", "trust the default"):
        assert phrase not in text


@pytest.mark.parametrize("render", [_primer_text, _coding_text])
def test_with_no_default_namespace_recall_names_one(_wired, render) -> None:
    _wired(_SetupTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    text = render(_token(("qa-clickhouse",)))
    assert "`loom_search` with a `namespace`" in text
    assert "`loom_search` once" not in text


def test_loom_search_from_a_token_with_no_default_namespace_says_to_name_one(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    _wired(_SetupTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    params = {"name": "loom_search", "arguments": {"query": "what did we decide"}}
    token = _token(("qa-clickhouse",))
    out = asyncio.run(mcp_call._call_tool(1, params, token, "claude-code"))
    result = out["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "VALIDATION_FAILED"
    assert ["namespace"] in [e["loc"] for e in result["structuredContent"]["errors"]]
    assert "no default namespace" in result["content"][0]["text"]
    assert "`loom_search` again with a `namespace`" in result["content"][0]["text"]

    # The retry it asks for validates.
    op = OPERATIONS_BY_NAME[mcp_call.SEARCH_OP]
    args: dict[str, Any] = {"query": "what did we decide", "namespace": "qa-clickhouse"}

    async def _retry() -> Any:
        request_id, _ = await mcp_call._bind_caller_identity(op, args, token, "claude-code")
        return mcp_call._validated_input(1, op, args, request_id)

    assert not isinstance(asyncio.run(_retry()), dict)


def test_a_restricted_namespace_is_off_limits_to_a_token_of_another_org(_wired) -> None:
    # The gate refuses an org-`*` token on a restricted namespace, even named.
    _wired(_SetupTokenCH(saved="secret-team", grants=["secret-team", "alice"]))
    token = _token(("*", "secret-team"), subject="alice", org="*", creator="")
    assert asyncio.run(home.default_namespace(token, "clickhouse")) == "alice"


def test_a_wildcard_setup_token_without_grants_falls_to_its_creators_personal_slug(
    _wired,
) -> None:
    _wired(_SetupTokenCH(saved=None, grants=[]))
    assert _defaults(_setup_token())["namespace"] == "personal-alice"


def test_a_service_account_without_a_creator_keeps_todays_default(_wired) -> None:
    from server.auth.personal_namespace import derive_personal_slug

    _wired(_SetupTokenCH(saved=None, grants=["alice"]))
    assert _defaults(_setup_token(creator=""))["namespace"] == derive_personal_slug(
        "sa:cursor-ab12cd"
    )


def test_the_away_note_compares_with_the_creators_login() -> None:
    mi.note_default_namespace(True, "alice", _setup_token())
    assert mi.default_namespace_note() == {}
    mi.note_default_namespace(True, "agent-box-claude-code", _setup_token())
    assert mi.default_namespace_note() == {"namespace_defaulted": "agent-box-claude-code"}


def test_a_creator_without_grants_is_refused_in_the_unprovisioned_default(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No grants anywhere: the default stays `personal-<creator>`, which nobody
    provisioned, and the unaddressed write is refused there by name."""
    from server.auth import authz, delegation
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    _wired(_SetupTokenCH(saved=None, grants=[]))

    async def _yes(*_a: Any, **_k: Any) -> bool:
        return True

    async def _no(*_a: Any, **_k: Any) -> bool:
        return False

    async def _no_grant(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(delegation, "creator_account_active", _yes)
    monkeypatch.setattr(authz, "_is_restricted", _no)
    monkeypatch.setattr(authz, "grant_role_for", _no_grant)
    token = AuthInfo(
        subject="sa:cursor-ab12cd",
        org="clickhouse",
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=True,
        creator="alice",
    )
    ns = _defaults(token)["namespace"]
    assert ns == "personal-alice"
    with pytest.raises(PermissionError) as refused:
        asyncio.run(authz.check_authorized(token, Role.WRITER, "clickhouse", ns))
    op = OPERATIONS_BY_NAME["memory.set"]
    body = mcp_call._forbidden_result(op, refused.value, token, "clickhouse", ns)
    assert body["structuredContent"]["code"] == "AUTH_INSUFFICIENT"
    assert ns in body["structuredContent"]["error"]
