"""A password / OIDC login token names every *restricted*
namespace the subject holds an active direct grant on, after the namespaces
the person owns; standard namespaces are never added; a failed lookup leaves
the owned set untouched."""

from __future__ import annotations

from typing import Any

import pytest

import server.auth.login as login

pytestmark = pytest.mark.usefixtures("cutoffs_read_at_mint")


class _FakeCH:
    """Answers the owned-namespace read with `ns_rows` and the grant join
    (recognised by its table) with `grant_rows`; records every SQL."""

    def __init__(self, ns_rows: list[dict[str, Any]], grant_rows: list[dict[str, Any]] | Exception):
        self.ns_rows = ns_rows
        self.grant_rows = grant_rows
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "namespace_grants" in sql:
            if isinstance(self.grant_rows, Exception):
                raise self.grant_rows
            return self.grant_rows
        return self.ns_rows


def _own(*names: str) -> list[dict[str, Any]]:
    return [
        {
            "namespace": n,
            "owner_email": "jdoe",
            "org": "clickhouse",
            "password_hash": "$argon2id$x",
            "contact_email": "avery@example.com",
        }
        for n in names
    ]


async def test_widening_appends_restricted_grants_after_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCH(
        _own("jdoe"),
        [{"namespace": "code-example-private"}, {"namespace": "code-private-26-4"}],
    )
    monkeypatch.setattr(login, "get_client", lambda: fake)
    out = await login.widen_with_restricted_grants("jdoe", "clickhouse", ["jdoe"])
    assert out == ["jdoe", "code-example-private", "code-private-26-4"]
    # the query itself carries the three filters the record promises
    q = fake.sql[-1]
    assert "classification) = 'restricted'" in q and "revoked_at IS NULL" in q and "'active'" in q
    assert "lower(g.subject) IN {subs:Array(String)}" in q  # The alias pair


async def test_widening_dedupes_and_keeps_owned_order(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH([], [{"namespace": "b-team"}, {"namespace": "a-own"}, {"namespace": ""}])
    monkeypatch.setattr(login, "get_client", lambda: fake)
    out = await login.widen_with_restricted_grants("me", "dev", ["z-first", "a-own"])
    assert out == ["z-first", "a-own", "b-team"]


async def test_widening_failure_leaves_owned_set(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH([], RuntimeError("clickhouse down"))
    monkeypatch.setattr(login, "get_client", lambda: fake)
    out = await login.widen_with_restricted_grants("me", "dev", ["mine"])
    assert out == ["mine"]


async def test_password_login_token_names_restricted_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCH(_own("jdoe"), [{"namespace": "code-example-private"}])
    monkeypatch.setattr(login, "get_client", lambda: fake)

    async def _ok(_h: str, _p: str) -> bool:
        return True

    monkeypatch.setattr(login, "verify_password_off_loop", _ok)
    out = await login.resolve_user_credentials("avery@example.com", "pw")
    assert out is not None
    subject, org, namespaces, roles = out
    assert (subject, org) == ("jdoe", "clickhouse")
    assert namespaces == ["jdoe", "code-example-private"]
    assert roles == ["writer"]
    # the grant read is scoped to the login's own subject and org
    assert any("namespace_grants" in q for q in fake.sql)


async def test_oidc_identity_names_restricted_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH(_own("jdoe"), [{"namespace": "tracker-private"}])
    monkeypatch.setattr(login, "get_client", lambda: fake)
    out = await login.resolve_identity_for_email("Avery@Example.com")
    assert out is not None
    assert out[2] == ["jdoe", "tracker-private"]


async def test_bad_password_never_reads_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH(_own("jdoe"), [{"namespace": "code-example-private"}])
    monkeypatch.setattr(login, "get_client", lambda: fake)

    async def _no(_h: str, _p: str) -> bool:
        return False

    monkeypatch.setattr(login, "verify_password_off_loop", _no)
    assert await login.resolve_user_credentials("avery@example.com", "nope") is None
    assert not any("namespace_grants" in q for q in fake.sql)


@pytest.mark.asyncio
async def test_widening_reads_the_gates_live_grant_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.authz import LIVE_GRANT

    ch = _FakeCH([], [{"namespace": "secret-repo"}])
    monkeypatch.setattr(login, "get_client", lambda: ch)
    await login.widen_with_restricted_grants("jdoe", "clickhouse", ["own"])
    grant_sql = [s for s in ch.sql if "namespace_grants" in s]
    assert grant_sql and LIVE_GRANT in grant_sql[0]


@pytest.mark.asyncio
async def test_a_login_leaves_out_an_owned_namespace_whose_grant_was_taken_away(
    cutoffs_read_at_mint: dict[str, int],
) -> None:
    """Owning the registry row is not access once a co-admin revoked or
    lowered the grant; whatever grant is left applies through the grant itself."""
    cutoffs_read_at_mint["team"] = 1
    assert await login._without_cut_namespaces("jdoe", "clickhouse", ["home", "team"]) == ["home"]


@pytest.mark.asyncio
async def test_an_unreadable_cutoff_store_refuses_the_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # As an OAuth exchange answers 503: a token minted without the cutoffs
    # would miss the person's own namespaces for its whole life.
    import server.auth.revocation as revocation

    async def _down(*_a: Any, **_k: Any) -> set[str]:
        raise RuntimeError("replica away")

    monkeypatch.setattr(revocation, "read_grant_cutoffs", _down)
    with pytest.raises(login.SignInUnavailable):
        await login._without_cut_namespaces("jdoe", "clickhouse", ["home", "team"])


@pytest.mark.asyncio
async def test_the_restricted_grant_read_raises_for_a_caller_that_must_not_narrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An OAuth refresh: a failed read must not mint a narrowed session.
    ch = _FakeCH(_own("home"), RuntimeError("replica away"))
    monkeypatch.setattr(login, "get_client", lambda: ch)
    with pytest.raises(RuntimeError):
        await login.restricted_grant_namespaces("jdoe", "clickhouse", strict=True)
