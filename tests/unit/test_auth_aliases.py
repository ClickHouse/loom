"""One person is one principal — a login handle and its
contact email read as one subject in the grant reads, and a new human grant
is written under the login handle. `grants.list`'s one row per person is in
test_grants_list_pages.py."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.auth import aliases as al


def _ch(rows):
    ch = AsyncMock()
    ch.query_all.return_value = rows
    return ch


@pytest.fixture(autouse=True)
def _clear_cache():
    al._CACHE.clear()
    yield
    al._CACHE.clear()


@pytest.mark.asyncio
async def test_aliases_pair_the_login_handle_with_its_contact_email() -> None:
    ch = _ch([{"owner_email": "jdoe", "contact_email": "avery@example.com"}])
    assert await al.subject_aliases("acme", "jdoe", ch=ch) == ["jdoe", "avery@example.com"]
    al._CACHE.clear()
    assert await al.subject_aliases("acme", "Avery@Example.com", ch=ch) == [
        "Avery@Example.com",
        "jdoe",
    ]


@pytest.mark.asyncio
async def test_service_accounts_wildcards_and_untied_subjects_stand_alone() -> None:
    ch = _ch([])
    assert await al.subject_aliases("acme", "sa:clickgap-1", ch=ch) == ["sa:clickgap-1"]
    assert await al.subject_aliases("acme", "*", ch=ch) == ["*"]
    assert await al.subject_aliases("acme", "nobody@example.com", ch=ch) == ["nobody@example.com"]
    ch.query_all.assert_awaited_once()  # the two non-human subjects never read


@pytest.mark.asyncio
async def test_a_failed_read_leaves_the_subject_alone_and_is_not_cached() -> None:
    ch = AsyncMock()
    ch.query_all.side_effect = RuntimeError("down")
    assert await al.subject_aliases("acme", "jdoe", ch=ch) == ["jdoe"]
    assert ("acme", "jdoe") not in al._CACHE


@pytest.mark.asyncio
async def test_canonical_subject_is_the_login_handle() -> None:
    ch = _ch([{"owner_email": "jdoe", "contact_email": "avery@example.com"}])
    assert await al.canonical_subject("acme", "avery@example.com", ch=ch) == "jdoe"
    al._CACHE.clear()
    assert await al.canonical_subject("acme", "sa:x", ch=_ch([])) == "sa:x"
    assert (
        await al.canonical_subject("acme", "orphan@example.com", ch=_ch([])) == "orphan@example.com"
    )


@pytest.mark.asyncio
async def test_canonical_subject_reads_past_a_stale_cache() -> None:
    # Another process linked or released the account: this one's cache still
    # holds the old binding, which a grant must not be written under.
    al._CACHE[("acme", "alice@acme.io")] = (time.monotonic(), ("alice@acme.io", "alice"))
    al._CACHE[("acme", "new@acme.io")] = (time.monotonic(), ("new@acme.io",))
    assert await al.canonical_subject("acme", "alice@acme.io", ch=_ch([])) == "alice@acme.io"
    linked = _ch([{"owner_email": "alice", "contact_email": "new@acme.io"}])
    assert await al.canonical_subject("acme", "new@acme.io", ch=linked) == "alice"


@pytest.mark.asyncio
async def test_a_grant_after_a_release_lands_under_the_address(monkeypatch) -> None:
    from server.identity.api.models_grants import GrantsCreateInput
    from server.identity.app import grants as grants_mod
    from server.identity.store import grants as grants_repo

    rows: list[dict] = []

    async def _none(*_a, **_k) -> None:
        return None

    async def _insert(row: dict, **_k) -> None:
        rows.append(row)

    monkeypatch.setattr(grants_repo, "find_active_grant", _none)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(grants_mod, "_enforce_code_ceiling", _none)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _ch([]))  # released: nothing tied
    al._CACHE[("acme", "alice@acme.io")] = (time.monotonic(), ("alice@acme.io", "alice"))
    await grants_mod.handle_grants_create(
        GrantsCreateInput(org="acme", namespace="ns", subject="alice@acme.io", role="writer")
    )
    assert [r["subject"] for r in rows] == ["alice@acme.io"]


@pytest.mark.asyncio
async def test_invalidate_drops_every_read_naming_the_spellings() -> None:
    ch = _ch([{"owner_email": "jdoe", "contact_email": "avery@example.com"}])
    await al.subject_aliases("acme", "jdoe", ch=ch)
    await al.subject_aliases("acme", "new@example.com", ch=_ch([]))
    await al.subject_aliases("other", "jdoe", ch=ch)
    al.invalidate("acme", ["Avery@Example.com", "new@example.com"])
    assert set(al._CACHE) == {("other", "jdoe")}  # found by its spelling, or asked under it


@pytest.mark.asyncio
async def test_a_read_in_flight_across_an_unlink_is_not_kept() -> None:
    # The read saw the old link; the unlink's invalidation ran before it returned,
    # so keeping it would admit under the old tie until the TTL.
    class _Slow:
        async def query_all(self, *_a: Any, **_k: Any) -> list[dict[str, str]]:
            al.invalidate_org("acme")  # an unlink lands while the read runs
            return [{"owner_email": "jdoe", "contact_email": "avery@example.com"}]

    assert await al.subject_aliases("acme", "jdoe", ch=_Slow()) == ["jdoe", "avery@example.com"]
    assert ("acme", "jdoe") not in al._CACHE


@pytest.mark.asyncio
async def test_restricted_gate_reads_the_pair(monkeypatch) -> None:
    from server.auth import authz

    async def fake_aliases(org, subject, ch=None):
        return [subject, "avery@example.com"]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", fake_aliases)
    client = AsyncMock()
    client.query_one.return_value = {"role": "admin"}
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    role = await authz.grant_role_for("acme", "code-x", "jdoe")
    assert role is not None and role.name.lower() == "admin"
    params = client.query_one.await_args.kwargs["params"]
    assert params["subs"] == ["jdoe", "avery@example.com"]


# ---- every grant read the dashboard and the MCP identity make ----------------


async def _pair(org, subject, ch=None):
    return [subject, "avery@example.com"]


@pytest.mark.asyncio
async def test_tracker_sources_caller_grants_read_the_pair(monkeypatch) -> None:
    import server.tracker.app.sources as qs

    monkeypatch.setattr("server.auth.aliases.subject_aliases", _pair)
    client = AsyncMock()
    client.query_all.return_value = [
        {"namespace": "tracker-x", "role": "viewer", "status": "active", "pending_role": ""}
    ]
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    out = await qs._caller_grants("acme", "jdoe", ["tracker-x"])
    assert out["tracker-x"]["role"] == "viewer"
    sql = client.query_all.await_args.args[0]
    params = client.query_all.await_args.kwargs.get("params") or client.query_all.await_args.args[1]
    assert "subject IN {subs:Array(String)}" in sql
    assert params["subs"] == ["jdoe", "avery@example.com"]


@pytest.mark.asyncio
async def test_me_helper_pairs_only_a_named_caller(monkeypatch) -> None:
    import server.identity.app.me_callers as me

    monkeypatch.setattr("server.auth.aliases.subject_aliases", _pair)
    assert await me.subject_aliases_of("acme", "jdoe") == ["jdoe", "avery@example.com"]
    assert await me.subject_aliases_of("", "jdoe") == ["jdoe"]
    assert await me.subject_aliases_of("acme", "") == [""]


@pytest.mark.asyncio
async def test_mcp_default_namespace_checks_the_saved_pick_under_every_spelling(
    monkeypatch,
) -> None:
    # The saved pick is checked against the caller's grant map, whose read folds
    # every spelling the directory ties to the login (`grant_cache._read`).
    import server.core.grant_cache as grant_cache
    import server.identity.app.namespace_classification as nc
    import server.identity.app.namespace_kinds as nk
    import server.transport.mcp_identity as mi
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role
    from server.settings import settings

    async def _none(org: str) -> frozenset[str]:
        return frozenset()

    async def _no_kinds(org: str) -> dict[str, str]:
        return {}

    async def _folded(org: str, subject: str) -> grant_cache.GrantMap:
        # The writer grant is held under the contact email, read for the login.
        spellings = frozenset(await _pair(org, subject))
        return grant_cache.GrantMap(ranks={"team-x": 2}, spellings=spellings)

    async def _query_one(sql: str, params: Any = None, **_: Any) -> Any:
        return {"namespace": "team-x"} if "user_state" in sql else None

    monkeypatch.setattr("server.auth.aliases.subject_aliases", _pair)
    monkeypatch.setattr(type(settings), "effective_identity_provider", lambda self: "password")
    monkeypatch.setattr(nc, "restricted_set", _none)
    monkeypatch.setattr(nk, "kind_map", _no_kinds)
    monkeypatch.setattr(grant_cache, "_read", _folded)
    client = AsyncMock()
    client.query_one.side_effect = _query_one
    client.query_all.return_value = []
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    auth = AuthInfo(
        subject="jdoe",
        org="acme",
        namespaces=("personal-jdoe",),
        roles=(Role.WRITER,),
        is_service_account=False,
    )
    out = await mi._caller_defaults(auth, "claude-code")
    assert out is not None and out["namespace"] == "team-x" and out["org"] == "acme"


@pytest.mark.asyncio
async def test_the_gate_matches_a_grant_whatever_the_case_of_its_subject(monkeypatch) -> None:
    # The cached grant maps match `lower(subject)`; the gate's own read must too.
    from server.auth import authz

    async def fake_aliases(org, subject, ch=None):
        return [subject, "Avery@Example.com"]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", fake_aliases)
    client = AsyncMock()
    client.query_one.return_value = {"role": "writer"}
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    assert await authz.grant_role_for("acme", "team", "JDoe") is authz.Role.WRITER
    sql = client.query_one.await_args.args[0]
    assert "lower(subject) IN {subs:Array(String)}" in sql
    assert client.query_one.await_args.kwargs["params"]["subs"] == ["jdoe", "avery@example.com"]
