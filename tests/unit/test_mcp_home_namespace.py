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
from server.auth.context import readable_named_claims

KINDS = {
    "alice": "memory",
    "qa-clickhouse": "qa",
    "qa-example-private": "qa",
    "code-loom": "code",
    "code-clickhouse": "code",
    "agent-box-claude-code": "memory",
}
RESTRICTED = frozenset({"secret-team"})


def _Auth(
    subject: str,
    namespaces: tuple[str, ...] = (),
    sa: bool = False,
    creator: str = "",
) -> Any:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    return AuthInfo(
        subject=subject,
        org="clickhouse",
        namespaces=namespaces,
        roles=(Role.WRITER,),
        is_service_account=sa,
        creator=creator,
    )


class _CH:
    def __init__(self, saved: str | None, grants: list[str]) -> None:
        self.saved, self.grants = saved, grants
        self.sql: list[str] = []

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

    def grant_ranks(self, subject: str) -> dict[str, int]:
        """What the grant cache reads for `subject`: the admin grants listed."""
        return {n: 3 for n in self.grants}


@pytest.fixture()
def _wired(monkeypatch: pytest.MonkeyPatch):
    def _wire(ch: _CH) -> None:
        import server.auth.aliases as aliases
        import server.core.ch as chmod
        import server.core.grant_cache as grant_cache
        import server.identity.app.namespace_classification as nc
        import server.identity.app.namespace_kinds as nk

        async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
            return [subject]

        async def _kind_map(org: str) -> dict[str, str]:
            return dict(KINDS)

        async def _restricted_set(org: str) -> frozenset[str]:
            return RESTRICTED

        async def _grant_rows(org: str, subject: str) -> Any:
            ch.sql.append(f"grant_map:{subject}")
            return grant_cache.GrantMap(ranks=ch.grant_ranks(subject))

        monkeypatch.setattr(chmod, "get_client", lambda: ch)
        monkeypatch.setattr(grant_cache, "_read", _grant_rows)
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


def test_only_planes_reachable_is_no_default_never_a_plane(_wired) -> None:
    # Grants on planes only: no personal namespace is provisioned, so none is the default.
    _wired(_CH(saved="qa-clickhouse", grants=["qa-clickhouse", "code-loom"]))
    assert _defaults(_Auth("viewer@example.com", ("code-loom",)))["namespace"] == ""


def test_a_person_without_grants_defaults_to_the_personal_namespace_the_gate_provisions(
    _wired,
) -> None:
    _wired(_CH(saved=None, grants=[]))
    ns = _defaults(_Auth("viewer@example.com", ("code-loom",)))["namespace"]
    assert ns == "personal-viewer" and home.is_memory_home(KINDS, ns)


def test_a_person_whose_personal_name_is_taken_defaults_to_the_fallback_name(_wired) -> None:
    from server.auth.personal_namespace import personal_candidates

    class _Taken(_CH):
        async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            if "owner_email" in sql and (params or {}).get("ns") == "personal-viewer":
                return {"owner": "viewer@other.example"}
            return await super().query_one(sql, params)

    _wired(_Taken(saved=None, grants=[]))
    ns = _defaults(_Auth("viewer@example.com", ("code-loom",)))["namespace"]
    assert ns == personal_candidates("viewer@example.com")[1] != "personal-viewer"


def test_a_person_this_process_already_resolved_is_not_provisioned_again(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate provisions a person once per process; a person resolved while
    # still holding grants is skipped after losing them, so it is no default.
    import server.auth.personal_namespace as pn

    _wired(_CH(saved=None, grants=[]))
    monkeypatch.setattr(pn, "_seen", {("clickhouse", "viewer@example.com")})
    assert _defaults(_Auth("viewer@example.com", ("code-loom",)))["namespace"] == ""


def test_memory_home_helpers() -> None:
    assert home.home_namespace(_OWNER_GRANTS, "alice") == "alice"
    assert home.home_namespace(_OWNER_GRANTS[:4], "alice", "code-loom") == "code-loom"
    assert home.is_memory_home(KINDS, "unknown-ns") is True  # directory default is memory
    assert home.is_memory_home(KINDS, "code-loom") is False


class _StarTokenCH(_CH):
    """A `*` token from the token mint: only its owner (`alice` unless named) holds
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
        self.sql.append(sql)
        if self.owner not in (params or {}).get("subs", []):
            return []
        return [{"namespace": n, "role": "writer"} for n in self.grants]

    def grant_ranks(self, subject: str) -> dict[str, int]:
        return {n: 2 for n in self.grants} if subject == self.owner else {}


def _star_token(creator: str = "alice") -> Any:
    return _token(("*",), creator=creator)


def test_a_wildcard_token_defaults_to_its_creators_home(_wired) -> None:
    ch = _StarTokenCH(saved=None, grants=["code-loom", "alice"])
    _wired(ch)
    assert _defaults(_star_token())["namespace"] == "alice"
    assert not any("service_accounts" in q for q in ch.sql), "the signed claim, no row read"


def test_a_wildcard_token_follows_its_creators_saved_default(_wired) -> None:
    _wired(_StarTokenCH(saved="agent-box-claude-code", grants=["alice", "agent-box-claude-code"]))
    assert _defaults(_star_token())["namespace"] == "agent-box-claude-code"


def test_a_token_minted_with_a_namespace_before_star_saves_there_not_at_the_pick(_wired) -> None:
    # The mint's `[home, "*"]`, and `admin/token.mint` asked for `namespaces: [team-x, *]`.
    _wired(_StarTokenCH(saved="agent-box-claude-code", grants=["alice", "team-x"]))
    assert _defaults(_token(("team-x", "*"), creator="alice"))["namespace"] == "team-x"


def test_a_listed_token_keeps_following_the_pick_and_the_owners_namespace(_wired) -> None:
    # A list names no save target: the first entry is search order only.
    _wired(_StarTokenCH(saved="alice", grants=["alice", "team-x"]))
    assert _defaults(_token(("team-x", "alice"), creator="alice"))["namespace"] == "alice"


def test_a_namespace_named_first_that_it_cannot_write_falls_back_to_the_pick(_wired) -> None:
    _wired(_StarTokenCH(saved="alice", grants=["alice"]))
    assert _defaults(_token(("team-x", "*"), creator="alice"))["namespace"] == "alice"


def test_a_token_minted_by_another_service_account_follows_the_signed_creator(_wired) -> None:
    # The row says another service account made it; the gate checks alice's grants.
    _wired(_StarTokenCH(saved="agent-box-claude-code", grants=["agent-box-claude-code"]))
    assert _defaults(_star_token())["namespace"] == "agent-box-claude-code"


def test_a_self_signup_agent_token_keeps_its_namespace_not_its_claimers_pick(_wired) -> None:
    # Self-signup: the row's created_by is the agent account, whose claimer
    # saved a team pick there. The token carries no creator and claims its own.
    ch = _StarTokenCH(
        saved="team-x",
        grants=["team-x"],
        owner="agent-box-claude-code",
        row_creator="agent-box-claude-code",
    )
    _wired(ch)
    agent = _Auth("sa:agent-box-claude-code-1a2b3c", ("agent-box-claude-code",), sa=True)
    assert _defaults(agent)["namespace"] == "agent-box-claude-code"


def test_a_saved_restricted_namespace_is_no_default_for_a_wildcard_token(_wired) -> None:
    _wired(_StarTokenCH(saved="secret-team", grants=["secret-team", "alice"]))
    assert _defaults(_star_token())["namespace"] == "alice"


def test_the_ranking_skips_a_restricted_namespace_the_token_does_not_name(_wired) -> None:
    _wired(_StarTokenCH(saved=None, grants=["secret-team", "agent-box-claude-code"]))
    assert _defaults(_star_token())["namespace"] == "agent-box-claude-code"


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
    # A token for `qa-clickhouse` only: the creator's memory grant and
    # personal namespace are outside the claim, so neither is its default.
    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    assert _defaults(_token(("qa-clickhouse",)))["namespace"] == ""


def test_an_unaddressed_write_from_a_qa_only_token_asks_for_a_namespace(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live shape: no namespace is filled in, so the call fails validation
    naming `namespace`, not AUTH_INSUFFICIENT on a personal namespace."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
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
    assert "name one of `qa-clickhouse`" in asyncio.run(mi._caller_default_context(token))


def _primer_text(token: Any) -> str:
    from server.transport import mcp_primer

    return mcp_primer._loom_rules(None, asyncio.run(mcp_primer._caller_defaults(token)))


def _coding_line(token: Any) -> str:
    from server.transport import mcp_primer

    return mcp_primer._coding_workspace_line(asyncio.run(mcp_primer._caller_defaults(token)))


@pytest.mark.parametrize(
    "render",
    [lambda t: asyncio.run(mi._caller_default_context(t)), _coding_line, _primer_text],
    ids=["default_context", "coding_workspace_line", "loom_primer_text"],
)
def test_no_default_namespace_is_never_rendered_as_an_empty_one(_wired, render) -> None:
    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    text = render(_token(("qa-clickhouse",)))
    assert "namespace" in text
    for phrase in ("``", "filled in when a call names none", "trust the default"):
        assert phrase not in text


def _initialize_text(token: Any, toolset: str | None = None) -> str:
    from server.transport import mcp_primer

    return asyncio.run(mcp_primer._initialize_instructions(token, toolset=toolset))


@pytest.mark.parametrize("toolset", ["coding", None])
def test_with_no_default_namespace_the_instructions_name_the_readable_claims(
    _wired, toolset: str | None
) -> None:
    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    text = _initialize_text(_token(("qa-clickhouse",)), toolset)
    assert "name one of `qa-clickhouse` on each memory or tracker call" in text
    assert "`loom_search` once" in text  # it takes no namespace, and needs none


def test_a_token_that_can_read_no_claim_is_told_so(_wired) -> None:
    # `secret-team` is restricted and an org-`*` token is refused there even named.
    _wired(_StarTokenCH(saved=None, grants=["secret-team"]))
    text = _initialize_text(_token(("secret-team",), org="*"), "coding")
    assert "can read no namespace" in text
    assert "`secret-team`" not in text


def test_the_coding_instructions_never_tell_code_calls_to_name_a_namespace(_wired) -> None:
    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    text = _initialize_text(_token(("qa-clickhouse",)), "coding")
    assert "every call" not in text
    assert "loom_code_* calls use the code scope" in text


@pytest.mark.parametrize("toolset", ["coding", None])
def test_one_initialize_resolves_the_caller_defaults_once(
    _wired, monkeypatch: pytest.MonkeyPatch, toolset: str | None
) -> None:
    from server.transport import mcp_primer

    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    calls: list[Any] = []
    real = mcp_primer._caller_defaults

    async def _counted(auth: Any, client_name: str | None = None) -> Any:
        calls.append(auth)
        return await real(auth, client_name)

    monkeypatch.setattr(mcp_primer, "_caller_defaults", _counted)
    monkeypatch.setattr(mi, "_caller_defaults", _counted)  # the workspace block's own
    _initialize_text(_token(("qa-clickhouse",)), toolset)
    assert len(calls) == 1


@pytest.mark.parametrize("named", [None, "", "-"])
def test_loom_search_from_a_qa_only_token_searches_its_claim(
    _wired, monkeypatch: pytest.MonkeyPatch, named: str | None
) -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates, mcp_call

    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    op = OPERATIONS_BY_NAME[mcp_call.SEARCH_OP]
    args: dict[str, Any] = {"query": "what did we decide"}
    if named is not None:
        args["namespace"] = named
    token = _token(("qa-clickhouse",))

    async def _call() -> Any:
        request_id, _ = await mcp_call._bind_caller_identity(op, args, token, "claude-code")
        input_obj = mcp_call._validated_input(1, op, args, request_id)
        assert not isinstance(input_obj, dict), input_obj
        target = gates.authz_target(
            op, org=input_obj.org, namespace=input_obj.namespace, auth=token
        )
        await gates.authorize(op, token, target, transport="mcp")
        return target

    target = asyncio.run(_call())
    assert args["namespace"] == "qa-clickhouse"
    assert target.namespace == "qa-clickhouse"


def test_a_restricted_namespace_is_off_limits_to_a_token_of_another_org(_wired) -> None:
    # The gate refuses an org-`*` token on a restricted namespace, even named.
    _wired(_StarTokenCH(saved="secret-team", grants=["secret-team", "alice"]))
    token = _token(("*", "secret-team"), subject="alice", org="*", creator="")
    assert asyncio.run(home.default_namespace(token, "clickhouse")) == "alice"


def test_a_wildcard_token_whose_creator_holds_no_grant_has_no_default(_wired) -> None:
    # `personal-alice` was never provisioned: the gate refuses it, so no default.
    _wired(_StarTokenCH(saved=None, grants=[]))
    assert _defaults(_star_token())["namespace"] == ""


def test_a_service_account_without_a_creator_keeps_todays_default(_wired) -> None:
    from server.auth.personal_namespace import derive_personal_slug

    _wired(_StarTokenCH(saved=None, grants=["alice"]))
    assert _defaults(_star_token(creator=""))["namespace"] == derive_personal_slug(
        "sa:cursor-ab12cd"
    )


def test_the_away_note_compares_with_the_creators_login() -> None:
    mi.note_default_namespace(True, "alice", _star_token())
    assert mi.default_namespace_note() == {}
    mi.note_default_namespace(True, "agent-box-claude-code", _star_token())
    assert mi.default_namespace_note() == {"namespace_defaulted": "agent-box-claude-code"}


def test_an_unaddressed_write_from_a_creator_without_grants_asks_for_a_namespace(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No grants anywhere: no namespace is filled in, so the write fails
    validation naming `namespace`, not AUTH_INSUFFICIENT on an unprovisioned
    `personal-<creator>`."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    _wired(_StarTokenCH(saved=None, grants=[]))
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    op = OPERATIONS_BY_NAME["memory.set"]
    args: dict[str, Any] = {"memory_key": "k", "value": "v"}
    token = _star_token()

    async def _call() -> Any:
        request_id, _ = await mcp_call._bind_caller_identity(op, args, token, "claude-code")
        return mcp_call._validated_input(1, op, args, request_id)

    out = asyncio.run(_call())["result"]["structuredContent"]
    assert "namespace" not in args
    assert out["code"] == "VALIDATION_FAILED"
    assert ["namespace"] in [list(e["loc"]) for e in out["errors"]]


def _revoke_subject_grant(
    monkeypatch: pytest.MonkeyPatch, namespace: str, subject: str = "sa:cursor-ab12cd"
) -> None:
    """The token's own `subject` held a grant on `namespace`, now revoked: its
    claim there is capped at nothing (`authz.claim_cap` 0). Other subjects'
    grants are read as before."""
    import server.core.grant_cache as grant_cache

    real = grant_cache.grant_map

    async def _grant_map(org: str, who: str) -> Any:
        if who == subject:
            return grant_cache.GrantMap(ranks={namespace: 0})
        return await real(org, who)

    monkeypatch.setattr(grant_cache, "grant_map", _grant_map)


def test_a_delegated_claim_its_creator_holds_stays_named_after_its_own_grant_goes(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate decides a delegated token on its claim and its creator's grant,
    # never on its own subject's: the revoked grant takes nothing away, from
    # the names or from the default.
    from server.auth import authz
    from server.operations.base import Role

    _revoke_subject_grant(monkeypatch, "agent-box-claude-code")
    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse", "alice"]))
    token = _token(("agent-box-claude-code", "alice"))
    readable = asyncio.run(readable_named_claims(token, "clickhouse"))
    assert readable == ["agent-box-claude-code", "alice"]
    # Both stay writable through the creator; the creator's own namespace
    # ranks first, as for any token (`home_namespace`).
    default = _defaults(token)["namespace"]
    assert default == "alice"
    for ns in (default, "agent-box-claude-code"):
        asyncio.run(authz.check_authorized(token, Role.WRITER, "clickhouse", ns))


def test_a_delegated_default_follows_the_creators_write_grant_not_the_subjects(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The subject's own revoked grant used to drop the first claim as a write
    # home, so the default moved to `team-notes`; the gate still admits the write.
    from server.auth import authz
    from server.operations.base import Role

    _revoke_subject_grant(monkeypatch, "agent-box-claude-code")
    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "team-notes"]))
    token = _token(("agent-box-claude-code", "team-notes"))
    assert _defaults(token)["namespace"] == "agent-box-claude-code"
    asyncio.run(authz.check_authorized(token, Role.WRITER, "clickhouse", "agent-box-claude-code"))


def test_a_claim_the_creator_no_longer_holds_is_not_named(_wired) -> None:
    # alice left the team behind `gone-team`; the token still claims it first.
    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    token = _token(("gone-team", "qa-clickhouse"))
    assert asyncio.run(readable_named_claims(token, "clickhouse")) == ["qa-clickhouse"]
    text = _initialize_text(token, "coding")
    assert "name one of `qa-clickhouse` on" in text
    assert "`gone-team`" not in text


def test_a_creator_who_lost_every_claimed_grant_has_no_default(_wired) -> None:
    _wired(_StarTokenCH(saved=None, grants=["alice"]))
    token = _token(("agent-box-claude-code", "qa-clickhouse"))
    assert _defaults(token)["namespace"] == ""
    assert "can read no namespace" in _initialize_text(token, "coding")


def test_the_agent_box_claim_is_no_default_once_its_creator_lost_it(_wired) -> None:
    _wired(_StarTokenCH(saved=None, grants=["qa-clickhouse"]))
    token = _token(("agent-box-claude-code", "qa-clickhouse"))
    assert _defaults(token)["namespace"] == ""
    text = _initialize_text(token, "coding")
    assert "name one of `qa-clickhouse` on" in text
    assert "`agent-box-claude-code`" not in text


def test_the_no_default_line_names_what_loom_search_reads(
    _wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two tracker claims: the unaddressed search reads only the first, as the line says.
    from server.auth.context import set_current_auth
    from server.auth.fanout import namespaces_to_search
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    _wired(_StarTokenCH(saved=None, grants=["qa-clickhouse", "qa-example-private"]))
    monkeypatch.setattr(mcp_call, "_emit_mcp_event", lambda *a, **k: None)
    token = _token(("qa-clickhouse", "qa-example-private"))
    line = mi.no_default_namespace_line(_defaults(token))
    assert "`loom_search` searches `qa-clickhouse` (name another on the call" in line
    args: dict[str, Any] = {"query": "what did we decide"}

    async def _searched() -> list[str]:
        op = OPERATIONS_BY_NAME[mcp_call.SEARCH_OP]
        set_current_auth(token)
        await mcp_call._bind_caller_identity(op, args, token, "claude-code")
        scope = args.get("namespace_scope") or "all_mine"
        return (await namespaces_to_search("clickhouse", args["namespace"], scope)).read

    assert asyncio.run(_searched()) == ["qa-clickhouse"]
    one = mi.no_default_namespace_line({"readable": ["qa-clickhouse"]})
    assert "`loom_search` searches `qa-clickhouse`." in one


@pytest.mark.parametrize("named", ["", "-", " "])
def test_a_blank_or_dash_namespace_gets_the_default(
    _wired, monkeypatch: pytest.MonkeyPatch, named: str
) -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    _wired(_StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"]))
    token = _token(("agent-box-claude-code", "qa-clickhouse"))
    for op_name in ("memory.set", mcp_call.SEARCH_OP):
        args: dict[str, Any] = {"memory_key": "a/b/c", "value": "v", "namespace": named}
        op = OPERATIONS_BY_NAME[op_name]
        asyncio.run(mcp_call._bind_caller_identity(op, args, token, "claude-code"))
        assert args["namespace"] == "agent-box-claude-code", op_name


def test_a_steady_state_unaddressed_write_reads_no_grants(_wired) -> None:
    """Once the caches are warm, binding a delegated token's `memory.set`
    reads only its saved default: the claims it can name come from the
    cached grant map, not a scan of the grants table."""
    from server.auth import delegation
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    delegation._account_active.clear()
    ch = _StarTokenCH(saved=None, grants=["agent-box-claude-code", "qa-clickhouse"])
    _wired(ch)
    token = _token(("agent-box-claude-code", "qa-clickhouse"))
    op = OPERATIONS_BY_NAME["memory.set"]

    def _bind() -> list[str]:
        ch.sql.clear()
        args: dict[str, Any] = {"memory_key": "a/b/c", "value": "v"}
        asyncio.run(mcp_call._bind_caller_identity(op, args, token, "claude-code"))
        assert args["namespace"] == "agent-box-claude-code"
        return list(ch.sql)

    _bind()
    steady = _bind()
    assert len(steady) == 1 and "user_state" in steady[0], steady


class _CappedSACH(_StarTokenCH):
    """A flat service account whose own grants on its claims are all revoked:
    every claim is capped at nothing, so its row is read for inheritance."""

    def grant_ranks(self, subject: str) -> dict[str, int]:
        if subject.startswith("sa:"):
            return {n: 0 for n in ("agent-box-claude-code", "team-notes", "qa-clickhouse")}
        return super().grant_ranks(subject)


def _steady_bind(ch: _CH, token: Any) -> list[str]:
    """The statements a second, warm-cache bind of an unaddressed `memory.set` issues."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_call

    op = OPERATIONS_BY_NAME["memory.set"]
    for _ in range(2):
        ch.sql.clear()
        args: dict[str, Any] = {"memory_key": "a/b/c", "value": "v"}
        asyncio.run(mcp_call._bind_caller_identity(op, args, token, "claude-code"))
    return list(ch.sql)


def test_a_wildcard_delegated_bind_reads_the_saved_pick_and_the_ranking_only(_wired) -> None:
    ch = _StarTokenCH(saved=None, grants=["code-loom", "alice"])
    _wired(ch)
    steady = _steady_bind(ch, _star_token())
    assert len(steady) == 2, steady
    assert "user_state" in steady[0] and "namespace_grants" in steady[1]


def test_a_flat_service_account_with_capped_claims_reads_its_row_once(_wired) -> None:
    ch = _CappedSACH(saved=None, grants=["alice"])
    _wired(ch)
    token = _token(("agent-box-claude-code", "team-notes", "qa-clickhouse"), creator="")
    first = [q for q in _steady_bind(ch, token) if "service_accounts" in q]
    assert first == [], "a warm bind reads no service_accounts row"
