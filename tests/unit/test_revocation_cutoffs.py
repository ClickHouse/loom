"""An operator's cutoff (adopt, a bot replaced or disabled) is written beside
a subject's own revocation row, never over it: an outright revoke that lands
just before it still stands, in the snapshot and in the read minting uses."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.auth import revocation
from tests.unit.revocation_fakes import revocation_read


class _Table:
    """loom.revoked_tokens: the newest row of each jti wins, as under FINAL."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            self.rows[r["jti"]] = r

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        keys = (params or {}).get("keys")
        return [r for k, r in self.rows.items() if keys is None or k in keys]

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        return revocation_read(list(self.rows.values()), sql, params)


@pytest.fixture
def table(monkeypatch) -> _Table:
    from server.core import ch

    t = _Table()
    monkeypatch.setattr(ch, "get_client", lambda: t)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {})
    monkeypatch.setattr(revocation, "_cut_off", {})
    return t


def test_an_outright_revoke_outlives_a_cutoff_written_after_it(table) -> None:
    asyncio.run(revocation.revoke("eve", reason="offboarded"))
    later = datetime.now(UTC) + timedelta(seconds=1)
    asyncio.run(
        revocation.cut_off("eve", issued_before=later, reason="sso adopt", revoked_by="ops")
    )
    asyncio.run(revocation.refresh())
    tomorrow = int(time.time()) + 86400
    assert revocation.is_revoked("eve", issued_at=tomorrow)
    assert asyncio.run(revocation.subjects_revoked(["eve"], issued_at=tomorrow))


def test_a_cutoff_alone_ends_only_earlier_tokens(table) -> None:
    cutoff = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    asyncio.run(revocation.cut_off("sa:bot", issued_before=cutoff, reason="r", revoked_by="ops"))
    asyncio.run(revocation.refresh())
    before, after = int(cutoff.timestamp()) - 1, int(cutoff.timestamp())
    assert revocation.is_revoked("sa:bot", issued_at=before)
    assert not revocation.is_revoked("sa:bot", issued_at=after)
    assert asyncio.run(revocation.subjects_revoked(["sa:bot"], issued_at=before))
    assert not asyncio.run(revocation.subjects_revoked(["sa:bot"], issued_at=after))


def test_the_cutoffs_read_from_the_store_agree_with_the_token_check(table) -> None:
    # What SCIM decides a listed token's fate by: the same rule as `is_revoked`.
    now = datetime.now(UTC).replace(microsecond=0)
    asyncio.run(revocation.revoke("eve", reason="offboarded"))
    asyncio.run(revocation.revoke("lee", issued_before=now))
    asyncio.run(
        revocation.cut_off(
            "lee", issued_before=now + timedelta(seconds=6), reason="r", revoked_by="ops"
        )
    )
    found = asyncio.run(revocation.cutoffs(["eve", "lee", "kim"]))
    assert found == {"eve": None, "lee": int(now.timestamp()) + 6}
    asyncio.run(revocation.refresh())
    assert revocation.is_revoked("lee", issued_at=int(now.timestamp()) + 5)
    assert not revocation.is_revoked("lee", issued_at=int(now.timestamp()) + 6)


_STATES = (None, "outright", "narrowed")


@pytest.mark.parametrize("own", _STATES)
@pytest.mark.parametrize("scim", _STATES)
def test_scims_revocation_counts_as_the_subjects_own(table, own, scim) -> None:
    """Each of the subject's row and SCIM's: none, outright, or narrowed (to
    different cutoffs). Revoked outright when either is; else up to the later
    cutoff. The token check (before and after a refresh), the read minting
    makes and the read the token list makes all agree."""
    now = int(time.time())
    cutoffs = {"own": now + 10, "scim": now + 20}
    for which, state in (("own", own), ("scim", scim)):
        if state is None:
            continue
        cutoff = None
        if state == "narrowed":
            cutoff = datetime.fromtimestamp(cutoffs[which], UTC)
        write = revocation.revoke if which == "own" else revocation.scim_revoke
        asyncio.run(write("lee", issued_before=cutoff, reason="r", revoked_by="x"))
    narrowed = [cutoffs[w] for w, st in (("own", own), ("scim", scim)) if st == "narrowed"]
    expected: int | None = max(narrowed, default=0)
    if "outright" in (own, scim):
        expected = None
    revoked = own is not None or scim is not None

    assert asyncio.run(revocation.cutoffs(["lee"])) == ({"lee": expected} if revoked else {})
    for refreshed in (False, True):
        if refreshed:
            revocation._recent.clear()
            asyncio.run(revocation.refresh())
        # A parent jti is checked with no `iat`: only an outright row matches.
        assert revocation.is_revoked("lee") is (revoked and expected is None)
        for iat in (now, now + 15, now + 25):
            dead = revoked and (expected is None or iat < expected)
            assert revocation.is_revoked("lee", issued_at=iat) is dead, (refreshed, iat)
            assert asyncio.run(revocation.subjects_revoked(["lee"], issued_at=iat)) is dead


def test_a_subject_that_looks_like_a_cutoff_is_its_own(table) -> None:
    # `loom mint-token --subject cutoff:batch-import` is a subject like any other.
    asyncio.run(revocation.revoke("cutoff:batch-import", reason="leaked"))
    asyncio.run(revocation.refresh())
    assert revocation.is_revoked("cutoff:batch-import", issued_at=int(time.time()))
    assert not revocation.is_revoked("batch-import", issued_at=int(time.time()))


def test_a_later_revoke_does_not_lower_an_operator_cutoff_here(table) -> None:
    now = datetime.now(UTC)
    asyncio.run(
        revocation.cut_off(
            "sa:bot", issued_before=now + timedelta(seconds=6), reason="r", revoked_by="ops"
        )
    )
    asyncio.run(revocation.revoke("sa:bot", issued_before=now - timedelta(seconds=60)))
    issued = int(now.timestamp())
    # This process and ClickHouse agree, before and after a refresh.
    assert revocation.is_revoked("sa:bot", issued_at=issued)
    assert asyncio.run(revocation.subjects_revoked(["sa:bot"], issued_at=issued))
    asyncio.run(revocation.refresh())
    assert revocation.is_revoked("sa:bot", issued_at=issued)


def test_the_list_shows_an_operator_cutoff_and_scims_without_the_nul(capsys, monkeypatch) -> None:
    from server.cli.access import _print_revocations

    class _Rows:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [
                {"jti": revocation.CUTOFF_PREFIX + "sa:bot", "issued_before": "2026-10-16"},
                {"jti": revocation.SCIM_PREFIX + "lee", "issued_before": None},
            ]

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Rows())
    rows = asyncio.run(revocation.list_active())
    assert rows == [
        {"jti": "sa:bot", "issued_before": "2026-10-16", "kind": "cutoff"},
        {"jti": "lee", "issued_before": None, "kind": "scim"},
    ]
    for as_json in (False, True):
        _print_revocations(rows, as_json=as_json)
    out = capsys.readouterr().out
    assert "cutoff→sa:bot" in out and "scim→lee" in out
    assert "\x00" not in out and "\\u0000" not in out


class _StalledTable(_Table):
    """The first read takes what the table holds at once, then answers late."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self._stalled = False

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        rows = await super().query_all(sql, params)
        if params is None and not self._stalled:
            self._stalled = True
            await self.release.wait()
        return rows


@pytest.mark.parametrize("write", ["cut_off", "revoke"])
def test_a_refresh_that_read_before_a_write_does_not_drop_it(monkeypatch, write) -> None:
    from server.core import ch

    table = _StalledTable()
    monkeypatch.setattr(ch, "get_client", lambda: table)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {})
    monkeypatch.setattr(revocation, "_cut_off", {})
    monkeypatch.setattr(revocation, "_loaded", True)

    async def race() -> None:
        stale = asyncio.create_task(revocation.refresh())  # reads the empty table, then waits
        await asyncio.sleep(0)
        if write == "cut_off":
            later = datetime.now(UTC) + timedelta(seconds=6)
            await revocation.cut_off("sa:bot", issued_before=later, reason="r", revoked_by="ops")
        else:
            await revocation.revoke("sa:bot", reason="leaked")
        table.release.set()
        await stale  # returns after the write, with a snapshot that lacks it

    asyncio.run(race())
    assert revocation.is_revoked("sa:bot", issued_at=int(time.time()))


def test_no_subject_can_carry_the_cutoff_marker() -> None:
    from fastapi import HTTPException

    from server.auth.jwt import mint_token
    from server.identity.app.me_service_accounts import token_name

    sneaky = revocation.CUTOFF_PREFIX + "alice"
    # A minted token's subject is `sa:<name>-<hash>`: the name cannot carry the marker.
    with pytest.raises(HTTPException):
        token_name(sneaky)
    with pytest.raises(ValueError):
        mint_token(subject=sneaky, org="acme", namespaces=["a"], roles=["writer"])
    with pytest.raises(ValueError):
        asyncio.run(revocation.revoke(sneaky))


def test_a_token_whose_sub_carries_a_nul_is_refused() -> None:
    import jwt as pyjwt

    from server.auth.jwt import TokenError, mint_token, verify_token

    good = mint_token(subject="alice", org="acme", namespaces=["a"], roles=["writer"])
    claims = pyjwt.decode(good, options={"verify_signature": False})
    header = pyjwt.get_unverified_header(good)
    from server.settings import settings

    forged = pyjwt.encode(
        {**claims, "sub": "\x00cutoff:alice"},
        settings.jwt_secret,
        algorithm=header["alg"],
        headers={"kid": header.get("kid")},
    )
    with pytest.raises(TokenError, match="NUL"):
        verify_token(forged)


class _LaggingTable(_Table):
    """A replica behind: it takes writes, but its full reads hold none yet."""

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return [] if params is None else await super().query_all(sql, params)


def test_writes_made_here_survive_a_replica_that_has_not_seen_them(monkeypatch) -> None:
    from server.core import ch

    monkeypatch.setattr(ch, "get_client", lambda: _LaggingTable())
    later = datetime.now(UTC) + timedelta(seconds=6)
    for owner in ("owner1", "owner2"):  # an adopt's loop
        asyncio.run(revocation.cut_off(owner, issued_before=later, reason="r", revoked_by="ops"))
    for jti in ("t1", "t2"):  # two logouts
        asyncio.run(revocation.revoke(jti, reason="logout"))
    asyncio.run(revocation.refresh())  # the next tick, from that replica
    now = int(time.time())
    assert all(revocation.is_revoked(k, issued_at=now) for k in ("owner1", "owner2", "t1", "t2"))


def test_a_full_revoke_read_from_elsewhere_stands_over_a_cutoff_made_here(table) -> None:
    asyncio.run(revocation.revoke("sub1", issued_before=datetime.now(UTC) - timedelta(hours=1)))
    # Another replica, later.
    table.rows["sub1"] = {"jti": "sub1", "issued_before": None, "version": time.time_ns()}
    asyncio.run(revocation.refresh())
    assert revocation.is_revoked("sub1", issued_at=int(time.time()) + 3600)


def test_a_write_older_than_the_while_is_no_longer_laid_back(monkeypatch) -> None:
    from server.core import ch

    monkeypatch.setattr(ch, "get_client", lambda: _LaggingTable())
    old = time.monotonic() - revocation._RECENT_S - 1
    revocation._recent.append(("revoke", "t-old", None, old, time.time_ns()))
    asyncio.run(revocation.refresh())
    assert not revocation.is_revoked("t-old", issued_at=int(time.time())) and not revocation._recent


def test_a_narrowing_elsewhere_wins_over_the_outright_revoke_made_here(monkeypatch) -> None:
    """A deactivation on one process, the reactivation on another: the first
    must not lay its outright revoke back over the newer narrowing."""
    from server.core import ch

    shared = _Table()
    monkeypatch.setattr(ch, "get_client", lambda: shared)
    asyncio.run(revocation.scim_revoke("lee", reason="r", revoked_by="scim"))  # process A
    on_a = list(revocation._recent)
    revocation._recent.clear()  # process B
    cutoff = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
    asyncio.run(revocation.scim_revoke("lee", issued_before=cutoff, reason="r", revoked_by="scim"))
    on_b = list(revocation._recent)
    old, new = int(cutoff.timestamp()) - 1, int(cutoff.timestamp())

    revocation._recent[:] = on_a  # A's next refresh reads the narrowing
    asyncio.run(revocation.refresh())
    assert revocation.is_revoked("lee", issued_at=old)
    assert not revocation.is_revoked("lee", issued_at=new)

    # B reading a replica that holds only A's row still keeps its narrowing.
    behind = _Table()
    key = revocation.SCIM_PREFIX + "lee"
    behind.rows[key] = {"jti": key, "issued_before": None, "version": on_a[-1][4]}
    monkeypatch.setattr(ch, "get_client", lambda: behind)
    revocation._recent[:] = on_b
    asyncio.run(revocation.refresh())
    assert revocation.is_revoked("lee", issued_at=old)
    assert not revocation.is_revoked("lee", issued_at=new)


def test_a_revoke_writes_past_a_row_from_a_clock_ahead(table) -> None:
    """Under FINAL the highest version wins: a narrowing written 10 s ahead of
    the clock (a host running fast) must not outvote a later deactivation."""
    fast = time.time_ns() + 10 * 10**9
    table.rows["lee"] = {"jti": "lee", "issued_before": datetime.now(UTC), "version": fast}
    asyncio.run(revocation.revoke("lee", reason="deactivated"))
    assert table.rows["lee"]["version"] > fast
    asyncio.run(revocation.refresh())
    assert revocation.is_revoked("lee", issued_at=int(time.time()) + 3600)
