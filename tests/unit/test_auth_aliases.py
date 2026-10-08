"""One person is one principal — a login handle and its
contact email read as one subject in the grant reads, fold to one reader
row, and a new human grant is written under the login handle."""

from __future__ import annotations

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


def test_fold_by_alias_makes_one_row_per_person_with_the_highest_role() -> None:
    rows = [
        {
            "subject": "avery@example.com",
            "role": "admin",
            "status": "active",
            "granted_by": "bootstrap",
        },
        {"subject": "jdoe", "role": "viewer", "status": "active", "granted_by": "registrar"},
        {"subject": "sa:clickgap", "role": "viewer", "status": "active", "granted_by": "ops"},
    ]
    aliases = {
        "avery@example.com": ["avery@example.com", "jdoe"],
        "jdoe": ["jdoe", "avery@example.com"],
        "sa:clickgap": ["sa:clickgap"],
    }
    out = al.fold_by_alias(rows, aliases)
    assert [r["subject"] for r in out] == ["avery@example.com", "sa:clickgap"]
    assert out[0]["role"] == "admin" and out[0]["also"] == ["jdoe"]
    assert out[1]["also"] == []


@pytest.mark.asyncio
async def test_restricted_gate_reads_the_pair(monkeypatch) -> None:
    from server.auth import authz

    async def fake_aliases(org, subject, ch=None):
        return [subject, "avery@example.com"]

    monkeypatch.setattr("server.auth.aliases.subject_aliases", fake_aliases)
    client = AsyncMock()
    client.query_one.return_value = {"role": "admin"}
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    role = await authz._grant_role_for("acme", "code-x", "jdoe")
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
async def test_mcp_default_namespace_validates_the_saved_pick_against_the_pair(monkeypatch) -> None:
    from types import SimpleNamespace

    import server.transport.mcp_identity as mi
    from server.settings import settings

    monkeypatch.setattr("server.auth.aliases.subject_aliases", _pair)
    monkeypatch.setattr(type(settings), "effective_identity_provider", lambda self: "password")
    client = AsyncMock()
    client.query_one.side_effect = [{"namespace": "code-x"}, {"role": "writer"}]
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    auth = SimpleNamespace(subject="jdoe", org="acme", namespaces=("personal-jdoe",))
    out = await mi._caller_defaults(auth, "claude-code")
    assert out is not None and out["namespace"] == "code-x" and out["org"] == "acme"
    validation = client.query_one.await_args_list[1]
    sql = validation.args[0]
    params = validation.kwargs.get("params") or validation.args[1]
    assert "g.subject IN {subs:Array(String)}" in sql
    assert params["subs"] == ["jdoe", "avery@example.com"] and params["ns"] == "code-x"
