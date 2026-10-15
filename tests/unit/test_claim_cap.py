"""A token's claim on a namespace reaches no further than the grant its subject
holds there. A revoked or lowered grant ends every older token's claim, a
pending request caps it at nothing, and a namespace the subject never held a
grant on (an operator's `loom mint-token`) keeps its claim."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

import server.auth.context as ctx
import server.auth.revocation as rev
from server.auth import aliases, authz
from server.auth.authz import check_authorized
from server.auth.jwt import AuthInfo
from server.core import grant_cache
from server.core.errors import BackendUnavailableError
from server.identity.store import grants as grants_repo
from server.operations.base import Role, grant_rank
from server.settings import settings

pytestmark = pytest.mark.claim_cap

ORG = "acme"


class _CH:
    """The subject's grant rows (namespace -> (role, status, revoked)), the
    directory's spellings, and `loom.revoked_tokens`."""

    def __init__(self) -> None:
        self.grants: dict[str, tuple[str, str, bool]] = {}
        self.accounts: list[dict[str, str]] = []
        self.revoked: dict[str, dict[str, Any]] = {}
        self.grant_reads = 0

    def _live(self, ns: str) -> str | None:
        role, status, revoked = self.grants.get(ns, ("", "", True))
        return role if status == "active" and not revoked else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "revoked_tokens" in sql:
            keys = (params or {}).get("keys")
            return [r for k, r in self.revoked.items() if keys is None or k in keys]
        if "GROUP BY namespace" in sql:
            self.grant_reads += 1
            return [{"namespace": ns, "rank": grant_rank(self._live(ns))} for ns in self.grants]
        if "owner_email, contact_email" in sql:
            return self.accounts
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict | None:
        if "namespace_grants" in sql:
            live = self._live((params or {}).get("ns", ""))
            return {"role": live} if live else None
        return None

    async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
        for r in rows:
            status = r.get("status") or "active"
            self.grants[r["namespace"]] = (r["role"], status, r["revoked_at"] is not None)


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> Iterator[_CH]:
    client = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(rev, "_revoked_jtis", frozenset())
    monkeypatch.setattr(rev, "_revoked_before", {})
    aliases._CACHE.clear()
    yield client
    aliases._CACHE.clear()
    ctx.set_current_auth(None)


def _flat(*namespaces: str, role: Role = Role.WRITER) -> AuthInfo:
    return AuthInfo(
        subject="alice", org=ORG, namespaces=namespaces, roles=(role,), is_service_account=False
    )


def _scoped(namespace: str, role: str = "writer") -> AuthInfo:
    return AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=(namespace,),
        roles=(),
        is_service_account=False,
        ns_roles=((namespace, role),),
    )


def _admits(auth: AuthInfo, namespace: str, role: Role = Role.VIEWER) -> bool:
    try:
        asyncio.run(check_authorized(auth, role, ORG, namespace))
    except PermissionError:
        return False
    return True


def test_a_claim_never_backed_by_a_grant_stands(ch: _CH) -> None:
    assert _admits(_flat("ops"), "ops", Role.WRITER)


@pytest.mark.parametrize(
    ("grant", "reads", "writes"),
    [
        (("viewer", "active", False), True, False),
        (("writer", "active", True), False, False),  # revoked
        (("admin", "pending", False), False, False),  # a request grants nothing
        (("admin", "active", False), True, True),
    ],
)
def test_a_flat_claim_reaches_no_further_than_the_grant(
    ch: _CH, grant: tuple[str, str, bool], reads: bool, writes: bool
) -> None:
    ch.grants["team"] = grant
    assert _admits(_flat("team"), "team") is reads
    assert _admits(_flat("team"), "team", Role.WRITER) is writes


def test_a_scoped_claim_is_capped_at_the_live_grant(ch: _CH) -> None:
    ch.grants["team"] = ("viewer", "active", False)
    assert _admits(_scoped("team"), "team")
    assert not _admits(_scoped("team"), "team", Role.WRITER)


def test_a_refusal_says_the_claim_goes_no_further_than_the_grant(ch: _CH) -> None:
    ch.grants["team"] = ("writer", "active", True)
    with pytest.raises(PermissionError, match="goes no further than the grant"):
        asyncio.run(check_authorized(_flat("team"), Role.VIEWER, ORG, "team"))


def test_a_grant_change_in_this_process_applies_at_once(ch: _CH) -> None:
    ch.grants["team"] = ("writer", "active", False)
    assert _admits(_flat("team"), "team", Role.WRITER)
    revoked = {
        "org": ORG,
        "namespace": "team",
        "subject": "alice",
        "role": "writer",
        "revoked_at": datetime(2026, 1, 1),
    }
    asyncio.run(grants_repo.insert_row(revoked))
    assert not _admits(_flat("team"), "team")


def test_a_first_grant_written_here_caps_a_claim_at_once(ch: _CH) -> None:
    assert _admits(_flat("team"), "team", Role.WRITER)
    viewer = {"org": ORG, "namespace": "team", "subject": "alice", "role": "viewer"}
    asyncio.run(grants_repo.insert_row({**viewer, "status": "active", "revoked_at": None}))
    assert not _admits(_flat("team"), "team", Role.WRITER)


def test_another_process_sees_a_grant_change_within_the_ttl(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.grants["team"] = ("writer", "active", False)
    assert asyncio.run(authz.live_claims(_flat("team"), ORG, ["team"])) == []
    ch.grants.pop("team")  # another process's write; this cache has not seen it
    assert asyncio.run(authz.live_claims(_flat("team"), ORG, ["team"])) == []
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 0.0)
    assert asyncio.run(authz.live_claims(_flat("team"), ORG, ["team"])) == ["team"]


def test_an_unread_spelling_caps(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down(*_a: Any, **_k: Any) -> list[str]:
        raise RuntimeError("replica away")

    monkeypatch.setattr(aliases, "subject_aliases", _down)
    assert not _admits(_flat("ops"), "ops", Role.WRITER)


def test_more_spellings_than_one_lookup_reads_cap(ch: _CH) -> None:
    ch.accounts = [
        {"owner_email": f"u-{i}", "contact_email": "alice"} for i in range(aliases.LOOKUP_ROWS)
    ]
    assert not _admits(_flat("ops"), "ops", Role.WRITER)
    ch.accounts = ch.accounts[:-1]
    aliases._CACHE.clear()
    assert _admits(_flat("ops"), "ops", Role.WRITER)


def test_a_grant_on_another_spelling_caps(ch: _CH) -> None:
    ch.accounts = [{"owner_email": "alice", "contact_email": "alice@x.io"}]
    ch.grants["team"] = ("viewer", "active", False)  # the fake answers for every spelling
    assert not _admits(_flat("team"), "team", Role.WRITER)


def test_a_failed_grant_read_serves_the_last_answer_then_refuses(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.grants["team"] = ("viewer", "active", False)
    assert not asyncio.run(authz.live_claims(_flat("team"), ORG, ["team"]))
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 0.0)

    async def _down(*_a: Any, **_k: Any) -> list[dict]:
        raise RuntimeError("replica away")

    monkeypatch.setattr(ch, "query_all", _down)
    monkeypatch.setattr(aliases, "subject_aliases", _just_alice)
    assert asyncio.run(grant_cache.grant_map(ORG, ["alice"])).has_row("team"), "stale, kept"
    monkeypatch.setattr(settings, "jwt_revocation_max_stale_seconds", 0.0)
    with pytest.raises(BackendUnavailableError):
        asyncio.run(grant_cache.grant_map(ORG, ["alice"]))


async def _just_alice(*_a: Any, **_k: Any) -> list[str]:
    return ["alice"]


def test_concurrent_misses_read_the_grants_once(ch: _CH) -> None:
    async def _many() -> None:
        await asyncio.gather(*(grant_cache.grant_map(ORG, ["alice"]) for _ in range(5)))

    asyncio.run(_many())
    assert ch.grant_reads == 1


def test_a_scoped_token_lists_a_capped_claim_while_its_grant_is_live(ch: _CH) -> None:
    ch.grants["team"] = ("viewer", "active", False)
    ch.grants["gone"] = ("writer", "active", True)
    auth = AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=("team", "gone", "ops"),
        roles=(),
        is_service_account=False,
        ns_roles=(("team", "writer"), ("gone", "writer"), ("ops", "writer")),
    )
    assert asyncio.run(authz.scoped_claims(auth, ORG)) == ["team", "ops"]


def test_a_subject_revocation_is_read_from_clickhouse(ch: _CH) -> None:
    now = int(datetime.now(UTC).timestamp())
    ch.revoked["alice"] = {"jti": "alice", "issued_before": now}
    assert asyncio.run(rev.subjects_revoked(["alice"], issued_at=now - 10))
    assert not asyncio.run(rev.subjects_revoked(["alice"], issued_at=now + 10))
    ch.revoked["alice"] = {"jti": "alice", "issued_before": None}
    assert asyncio.run(rev.subjects_revoked(["alice"], issued_at=now + 10)), "revoked outright"
    assert not asyncio.run(rev.subjects_revoked(["bob"], issued_at=now))


def test_the_mint_time_revocation_read_is_sequentially_consistent(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FINAL resolves the versions a replica holds, not ones it has not fetched.
    from server.core.ch import sequential_reads_active

    consistent: list[bool] = []
    read = ch.query_all

    async def _recording(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        consistent.append(sequential_reads_active())
        return await read(sql, params)

    monkeypatch.setattr(ch, "query_all", _recording)
    asyncio.run(rev.subjects_revoked(["alice"], issued_at=0))
    assert consistent == [True]


def test_several_subjects_are_checked_for_a_revocation_in_one_read(ch: _CH) -> None:
    reads: list[list[str]] = []
    read = ch.query_all

    async def _counting(sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        reads.append(list((params or {}).get("keys") or []))
        return await read(sql, params)

    ch.query_all = _counting  # type: ignore[method-assign]
    now = int(datetime.now(UTC).timestamp())
    ch.revoked["bob"] = {"jti": "bob", "issued_before": None}
    assert asyncio.run(rev.subjects_revoked(["u-bob", "bob", "u-bob"], issued_at=now))
    assert not asyncio.run(rev.subjects_revoked(["u-bob"], issued_at=now))
    assert not asyncio.run(rev.subjects_revoked([], issued_at=now))
    assert reads == [["bob", "u-bob"], ["u-bob"]]
