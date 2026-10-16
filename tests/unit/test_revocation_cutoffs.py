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


def test_the_list_shows_an_operator_cutoff_without_its_nul(capsys) -> None:
    from server.cli.access import _print_revocations

    row = {"jti": revocation.CUTOFF_PREFIX + "sa:bot", "issued_before": "2026-10-16 08:00:00"}
    _print_revocations([row], as_json=False)
    out = capsys.readouterr().out
    assert "cutoff→sa:bot" in out and "\x00" not in out


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
    from pydantic import ValidationError

    from server.auth.jwt import mint_token
    from server.insights.app.admin_models import TokenMintInput

    sneaky = revocation.CUTOFF_PREFIX + "alice"
    with pytest.raises(ValidationError):
        TokenMintInput(subject=sneaky, org="acme", namespaces=["a"])
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
