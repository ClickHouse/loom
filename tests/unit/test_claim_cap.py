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
from server.auth import authz
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
    """The subject's grant rows (namespace -> (role, status, revoked)) under
    any spelling, and `loom.revoked_tokens`."""

    def __init__(self) -> None:
        self.grants: dict[str, tuple[str, str, bool]] = {}
        self.revoked: dict[str, dict[str, Any]] = {}
        self.spellings: list[str] = []
        self.grant_reads = 0
        self.exact_reads = 0

    def _live(self, ns: str) -> str | None:
        role, status, revoked = self.grants.get(ns, ("", "", True))
        return role if status == "active" and not revoked else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "revoked_tokens" in sql:
            keys = (params or {}).get("keys")
            return [r for k, r in self.revoked.items() if keys is None or k in keys]
        return []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict | None:
        if "groupArray((namespace, rank))" in sql:  # the cap's grant read
            self.grant_reads += 1
            grants = [(ns, grant_rank(self._live(ns))) for ns in self.grants]
            return {"grants": grants, "spellings": self.spellings or [(params or {})["sub"]]}
        # The exact-subject grant read, which misses a grant held under another case.
        self.exact_reads += 1
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
    yield client
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
    ch.grants["team"] = ("writer", "active", True)
    assert asyncio.run(authz.live_claims(_flat("team"), ORG, ["team"])) == []
    ch.grants.pop("team")  # another process's write; this cache has not seen it
    assert asyncio.run(authz.live_claims(_flat("team"), ORG, ["team"])) == []
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 0.0)
    assert asyncio.run(authz.live_claims(_flat("team"), ORG, ["team"])) == ["team"]


def test_the_grant_read_folds_every_spelling_of_the_subject(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    read = ch.query_one

    async def _recording(sql: str, params: dict[str, Any] | None = None) -> dict | None:
        seen.append((sql, params or {}))
        return await read(sql, params)

    monkeypatch.setattr(ch, "query_one", _recording)
    asyncio.run(grant_cache.grant_map(ORG, "Alice@X.io"))
    ((sql, params),) = seen
    assert "loom.namespaces" in sql and "LIMIT" not in sql, "every spelling, none cut off"
    assert "released_aliases" in sql, (
        "released accounts tie too (tests/integration/test_grant_cache_spellings.py)"
    )
    assert params["sub"] == "alice@x.io" and params["human"] is True


def test_a_grant_held_under_another_case_admits_up_to_it(ch: _CH) -> None:
    # The cap folds case; the exact-subject grant read (query_one here) does not.
    ch.grants["team"] = ("admin", "active", False)
    assert _admits(_flat("team"), "team", Role.WRITER)
    assert _admits(_scoped("team"), "team", Role.WRITER)


def test_requests_and_listings_read_the_grants_once_per_ttl(ch: _CH) -> None:
    ch.grants = {f"ns-{i}": ("writer", "active", False) for i in range(32)}
    names = tuple(ch.grants)
    for _ in range(3):
        assert _admits(_flat(*names), "ns-0", Role.WRITER)
    assert len(asyncio.run(authz.live_claims(_flat(*names), ORG, names))) == 32
    assert (ch.grant_reads, ch.exact_reads) == (1, 0)


def test_a_failed_grant_read_serves_the_last_answer_then_refuses(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.grants["team"] = ("viewer", "active", False)
    asyncio.run(grant_cache.grant_map(ORG, "alice"))
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 0.0)

    async def _down(*_a: Any, **_k: Any) -> list[dict]:
        raise RuntimeError("replica away")

    monkeypatch.setattr(ch, "query_one", _down)
    assert asyncio.run(grant_cache.grant_map(ORG, "alice")).rank("team") == 1, "stale, kept"
    monkeypatch.setattr(settings, "jwt_revocation_max_stale_seconds", 0.0)
    with pytest.raises(BackendUnavailableError):
        asyncio.run(grant_cache.grant_map(ORG, "alice"))


def test_concurrent_misses_read_the_grants_once(ch: _CH) -> None:
    async def _many() -> None:
        await asyncio.gather(*(grant_cache.grant_map(ORG, "alice") for _ in range(5)))

    asyncio.run(_many())
    assert ch.grant_reads == 1


def _slow_reads(ch: _CH, monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Grant reads that set the first event once they have read, and answer
    what they read only once the second is set."""
    read_done, go = asyncio.Event(), asyncio.Event()
    read = ch.query_one

    async def _slow(sql: str, params: dict[str, Any] | None = None) -> dict | None:
        row = await read(sql, params)
        read_done.set()
        await go.wait()
        return row

    monkeypatch.setattr(ch, "query_one", _slow)
    return read_done, go


def test_a_read_in_flight_across_a_grant_change_is_not_kept(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.grants["team"] = ("admin", "active", False)
    read_done, go = _slow_reads(ch, monkeypatch)

    async def _run() -> int | None:
        before = asyncio.ensure_future(grant_cache.grant_map(ORG, "alice"))
        await read_done.wait()
        ch.grants["team"] = ("admin", "active", True)
        grant_cache.invalidate(ORG)  # the revoke this process wrote
        go.set()
        await before
        return (await grant_cache.grant_map(ORG, "alice")).rank("team")

    assert asyncio.run(_run()) == 0


def test_a_cancelled_request_does_not_fail_the_ones_sharing_its_read(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.grants["team"] = ("viewer", "active", False)
    _, go = _slow_reads(ch, monkeypatch)

    async def _run() -> int | None:
        leader = asyncio.ensure_future(grant_cache.grant_map(ORG, "alice"))
        waiter = asyncio.ensure_future(grant_cache.grant_map(ORG, "alice"))
        await asyncio.sleep(0)
        leader.cancel()
        go.set()
        return (await waiter).rank("team")

    assert asyncio.run(_run()) == 1


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
    assert asyncio.run(authz.live_claims(auth, ORG, auth.namespaces)) == ["team", "ops"]


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


def test_a_stale_exact_grant_read_does_not_loosen_the_cap(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    ch.grants["team"] = ("writer", "active", True)  # revoked, as the cap reads it

    read = ch.query_one

    async def _stale(sql: str, params: dict[str, Any] | None = None) -> dict | None:
        if "groupArray((namespace, rank))" in sql:
            return await read(sql, params)
        return {"role": "writer"}  # a replica that has not seen the revoke

    monkeypatch.setattr(ch, "query_one", _stale)
    assert not _admits(_flat("team"), "team", Role.WRITER)


def test_a_service_account_capped_to_nothing_reads_through_its_creator(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The listings union the creator's grants in; the gate admits the same.
    ch.grants["team"] = ("writer", "active", True)  # the account's own row, revoked

    async def _creator(org: str, subject: str, **_k: Any) -> str:
        return "alice"

    async def _alice_writes(org: str, ns: str, subject: str) -> Role | None:
        return Role.WRITER if subject == "alice" else None

    monkeypatch.setattr("server.auth.service_accounts.inheriting_creator", _creator)
    monkeypatch.setattr(authz, "grant_role_for", _alice_writes)
    bot = AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=("team",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )
    assert _admits(bot, "team", Role.WRITER)


def test_a_grant_write_whose_reply_is_lost_still_drops_the_cache(ch: _CH) -> None:
    ch.grants["team"] = ("writer", "active", False)
    assert _admits(_flat("team"), "team", Role.WRITER)

    async def _landed_then_lost(table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
        ch.grants["team"] = ("writer", "active", True)
        raise TimeoutError("reply lost")

    ch.insert = _landed_then_lost  # type: ignore[method-assign]
    revoke = {"org": ORG, "namespace": "team", "subject": "alice", "role": "writer"}
    with pytest.raises(TimeoutError):
        asyncio.run(grants_repo.insert_row(revoke))
    assert not _admits(_flat("team"), "team")


def test_a_grant_write_drops_only_the_entries_that_read_its_subject(ch: _CH) -> None:
    ch.grants["team"] = ("writer", "active", False)
    ch.spellings = ["alice", "alice@x.io"]
    asyncio.run(grant_cache.grant_map(ORG, "alice"))
    ch.spellings = ["bob"]
    asyncio.run(grant_cache.grant_map(ORG, "bob"))
    reads = ch.grant_reads
    grant_cache.invalidate(ORG, ["Alice@X.io"])  # written under alice's email
    asyncio.run(grant_cache.grant_map(ORG, "bob"))
    assert ch.grant_reads == reads, "bob's entry is kept"
    asyncio.run(grant_cache.grant_map(ORG, "alice"))
    assert ch.grant_reads == reads + 1, "alice's entry covered that spelling"


def test_another_web_process_s_grant_write_is_seen_at_once(
    ch: _CH, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import os

    marker = tmp_path / "loom-grant-writes"
    monkeypatch.setattr(settings, "web_workers", 2)
    monkeypatch.setattr("server.core.runtime_files.server_file", lambda name: str(marker))
    ch.grants["team"] = ("writer", "active", False)
    assert _admits(_flat("team"), "team", Role.WRITER)
    ch.grants["team"] = ("writer", "active", True)  # revoked by a sibling web process
    assert _admits(_flat("team"), "team", Role.WRITER), "this process has not heard yet"
    marker.write_bytes(b"")
    os.utime(marker)  # the sibling marks its write
    assert not _admits(_flat("team"), "team", Role.WRITER)
