"""Every minted token carries a jti, and a subject
revocation can name an issued-before cutoff.

- `mint_token` stamps a fresh 32-hex `jti` unless the caller passes one; the
  CLI and the admin endpoint pass their own so they can report it.
- `verify_token` refuses a token whose jti is revoked, whose sub is revoked
  outright, or whose sub is revoked for tokens issued before a cutoff and
  whose `iat` is before it — and accepts a re-mint after the cutoff.
- `revoke(..., issued_before=)` writes the column; the refresh sorts rows
  into the full set and the cutoff map; `loom revoke-token --issued-before`
  requires `--subject`.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest

from server import cli
from server.auth import revocation, scim
from server.auth.jwt import TokenError, mint_token, new_jti, verify_token
from server.core import leases
from tests.unit.revocation_fakes import revocation_read

pytestmark = pytest.mark.usefixtures("stub_model_command")

_SECRET = "unit-test-secret-for-revocation-tests"  # >= 32 bytes for HS256


def _claims(tok: str) -> dict[str, Any]:
    return pyjwt.decode(tok, options={"verify_signature": False})


@pytest.fixture(autouse=True)
def _clean_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset())
    monkeypatch.setattr(revocation, "_revoked_before", {})


# ---- jti at mint ------------------------------------------------------------


def test_every_minted_token_carries_a_fresh_hex_jti() -> None:
    a = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET)
    b = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET)
    ja, jb = _claims(a)["jti"], _claims(b)["jti"]
    assert ja != jb and len(ja) == 32 and int(ja, 16) >= 0
    assert verify_token(a, secret=_SECRET).token_id == ja


def test_a_caller_supplied_jti_is_used_verbatim() -> None:
    j = new_jti()
    tok = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET, jti=j)
    assert _claims(tok)["jti"] == j
    assert len(new_jti()) == 32 and new_jti() != new_jti()


def test_revoking_the_jti_kills_exactly_that_token(monkeypatch: pytest.MonkeyPatch) -> None:
    a = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET)
    b = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({_claims(a)["jti"]}))
    with pytest.raises(TokenError, match="jti .* is revoked"):
        verify_token(a, secret=_SECRET)
    assert verify_token(b, secret=_SECRET).subject == "sa:bot"


# ---- issued-before cutoff ----------------------------------------------------


def test_is_revoked_honours_the_cutoff_only_with_an_issued_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(revocation, "_revoked_before", {"sa:bot": 1_000})
    assert revocation.is_revoked("sa:bot", issued_at=999)
    assert not revocation.is_revoked("sa:bot", issued_at=1_000)
    assert not revocation.is_revoked("sa:bot", issued_at=5_000)
    assert not revocation.is_revoked("sa:bot")  # no anchor => the cutoff cannot bite
    assert not revocation.is_revoked("someone-else", issued_at=1)
    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset({"sa:bot"}))
    assert revocation.is_revoked("sa:bot", issued_at=5_000)  # a full row still wins


def test_verify_kills_the_old_mint_and_keeps_the_re_mint(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh = mint_token("sa:clickgap", "acme", ["shop"], ["viewer"], secret=_SECRET)
    cutoff = int(time.time()) - 300  # the leak was noticed five minutes ago
    payload = {**_claims(fresh), "iat": cutoff - 3600, "jti": new_jti()}  # minted an hour before
    old = pyjwt.encode(payload, _SECRET, algorithm="HS256", headers={"kid": "override"})
    monkeypatch.setattr(revocation, "_revoked_before", {"sa:clickgap": cutoff})
    with pytest.raises(TokenError, match="sub 'sa:clickgap' is revoked"):
        verify_token(old, secret=_SECRET)
    assert verify_token(fresh, secret=_SECRET).subject == "sa:clickgap"


# ---- the write path and the refresh -----------------------------------------


class _FakeCH:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.connects = 0
        self.disconnected = 0

    connected = False

    async def connect(self) -> None:
        self.connects += 1

    async def disconnect(self) -> None:
        self.disconnected += 1

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        return revocation_read(self.rows, sql, params)

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, rows))
        by_key = {r["jti"]: r for r in self.rows}
        for r in rows:
            by_key[r["jti"]] = r  # ReplacingMergeTree: newest row per key
        self.rows = list(by_key.values())

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        now = datetime.now(UTC).replace(tzinfo=None)
        keys = (params or {}).get("keys")
        return [
            r for r in self.rows if r["expires_at"] > now and (keys is None or r["jti"] in keys)
        ]


@pytest.fixture
def fake_ch(monkeypatch: pytest.MonkeyPatch) -> _FakeCH:
    from tests.unit.claims_fake import memory_claims

    memory_claims(monkeypatch)
    ch = _FakeCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    return ch


def test_revoke_with_cutoff_writes_the_column_and_the_refresh_sorts_it(fake_ch: _FakeCH) -> None:
    cutoff = datetime(2026, 9, 17, 16, 30, tzinfo=UTC)
    row = asyncio.run(revocation.revoke("sa:clickgap", issued_before=cutoff, reason="handed on"))
    assert row["issued_before"] == datetime(2026, 9, 17, 16, 30)  # naive UTC like expires_at
    asyncio.run(revocation.revoke("gone-for-good"))
    assert revocation._revoked_before == {"sa:clickgap": int(cutoff.timestamp())}
    assert revocation._revoked_jtis == frozenset({"gone-for-good"})
    assert revocation.is_revoked("sa:clickgap", issued_at=int(cutoff.timestamp()) - 1)
    assert not revocation.is_revoked("sa:clickgap", issued_at=int(cutoff.timestamp()) + 1)


def test_a_later_full_row_replaces_the_cutoff_for_the_same_key(fake_ch: _FakeCH) -> None:
    asyncio.run(revocation.revoke("sa:x", issued_before=datetime(2026, 1, 1, tzinfo=UTC)))
    asyncio.run(revocation.revoke("sa:x"))
    assert "sa:x" in revocation._revoked_jtis and "sa:x" not in revocation._revoked_before


def test_cutoff_cells_from_ch_parse_as_datetime_or_epoch() -> None:
    assert revocation._cutoff_seconds(None) is None and revocation._cutoff_seconds("") is None
    assert revocation._cutoff_seconds(datetime(1970, 1, 1, 0, 1)) == 60
    assert revocation._cutoff_seconds(datetime(1970, 1, 1, 0, 1, tzinfo=UTC)) == 60
    assert revocation._cutoff_seconds(1234) == 1234 and revocation._cutoff_seconds("x") is None


# ---- CLI ----------------------------------------------------------------------


def test_cli_issued_before_needs_subject(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["revoke-token", "--jti", "abc", "--issued-before", "2026-09-17T16:30:00Z"])
    assert rc == 2 and "--subject" in capsys.readouterr().err
    assert fake_ch.inserts == [] and fake_ch.connects == 0


def test_cli_issued_before_rejects_a_bad_instant(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["revoke-token", "--subject", "sa:x", "--issued-before", "yesterday"])
    assert rc == 2 and "ISO-8601" in capsys.readouterr().err
    assert fake_ch.connects == 0


@pytest.mark.parametrize(
    "subject",
    ["user:u-1", "Group:finance", "everyone", "acme/User:u-1", "acme/user:U-1", "/user:u-1"],
)
def test_cli_refuses_a_subject_no_token_check_builds(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str], subject: str
) -> None:
    """No token's sub is spelled like a principal, bare or after a `/`, and an
    end user's key is `<org>/user:<id>` with a lowercase id: any other such
    spelling would cut off no one while saying it had."""
    rc = cli.main(["revoke-token", "--subject", subject])
    assert rc == 2 and "<org>/user:<id>" in capsys.readouterr().err, subject
    assert fake_ch.inserts == []


def test_cli_revokes_an_end_user_in_their_org(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    audited: list[str] = []

    async def _emit(event: str, *, org: str, **_k: Any) -> None:
        audited.append(org)

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    rc = cli.main(
        ["revoke-token", "--subject", "acme/user:u-1", "--issued-before", "2026-09-17T16:30:00Z"]
    )
    assert rc == 0
    (row,) = fake_ch.inserts[0][1]
    assert row["jti"] == "acme/user:u-1"
    out = capsys.readouterr().out
    assert "that org's end user; a token minted after that stays valid" in out
    assert "every org" not in out
    assert audited == ["acme"], "the revocation's record is the org's"


def test_cli_subject_with_cutoff_writes_it_and_says_the_re_mint_survives(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(
        ["revoke-token", "--subject", "sa:clickgap", "--issued-before", "2026-09-17T16:30:00Z"]
    )
    assert rc == 0
    (row,) = fake_ch.inserts[0][1]
    assert row["jti"] == "sa:clickgap" and row["issued_before"] == datetime(2026, 9, 17, 16, 30)
    assert timedelta(days=3000) < row["expires_at"] - row["revoked_at"]
    assert row["expires_at"] < datetime(2106, 1, 28)  # inside the TTL's DateTime range
    out = capsys.readouterr().out
    assert "issued before 2026-09-17T16:30:00Z" in out and "stays valid" in out
    assert revocation.is_revoked(
        "sa:clickgap", issued_at=int(datetime(2026, 9, 17, tzinfo=UTC).timestamp())
    )
    assert not revocation.is_revoked(
        "sa:clickgap", issued_at=int(datetime(2026, 9, 18, tzinfo=UTC).timestamp())
    )


def test_cli_list_prints_the_cutoff_column(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(revocation.revoke("sa:a", issued_before=datetime(2026, 9, 17, 16, 30, tzinfo=UTC)))
    asyncio.run(revocation.revoke("sa:b"))
    capsys.readouterr()
    assert cli.main(["revoke-token", "--list"]) == 0
    out = capsys.readouterr().out
    assert "issued_before" in out and "2026-09-17 16:30:00" in out
    line_b = next(line for line in out.splitlines() if line.startswith("sa:b"))
    assert " - " in line_b


def test_cli_mint_reports_the_jti_but_never_beside_a_saved_token(
    tmp_path: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOOM_JWT_SECRET", _SECRET)
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", _SECRET, raising=False)
    from server.cli import access

    # An empty keyring: the mint signs with the root secret, reading no ClickHouse.
    monkeypatch.setattr(access, "run_connected", lambda run, **_declared: None)
    rc = cli.main(
        ["mint-token", "--subject", "sa:x", "--org", "acme", "--namespaces", "shop", "--json"]
    )
    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["jti"] == _claims(summary["token"])["jti"]
    path = tmp_path / "tok"
    rc = cli.main(
        [
            "mint-token",
            "--subject",
            "sa:x",
            "--org",
            "acme",
            "--namespaces",
            "shop",
            "--json",
            "--save",
            str(path),
        ]
    )
    assert rc == 0
    out, err = capsys.readouterr()
    saved = json.loads(out)
    assert "token" not in saved and saved["token_path"] == str(path)
    assert saved["jti"] == _claims(path.read_text().strip())["jti"]
    assert f"--jti {saved['jti']}" in err  # the never-expires hint names the handle


# ---- a landed revoke holds in this process -----------------------------------


class _WriteOnlyCH:
    """The insert lands; the re-read fails (`fail`) or reads a replica the
    insert has not reached yet (returns no rows)."""

    def __init__(self, *, fail: bool) -> None:
        self.fail = fail
        self.inserted: list[dict[str, Any]] = []

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        return revocation_read(self.inserted, sql, params)

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserted.extend(rows)

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("clickhouse unavailable")
        return []


@pytest.mark.parametrize("fail", [True, False], ids=["re-read fails", "lagging replica"])
def test_a_landed_revoke_holds_here_whatever_the_re_read_returns(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    ch = _WriteOnlyCH(fail=fail)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(revocation, "_loaded", True)
    tok = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET)
    jti = _claims(tok)["jti"]
    asyncio.run(revocation.revoke(jti, reason="leaked"))
    assert ch.inserted and ch.inserted[0]["jti"] == jti
    with pytest.raises(TokenError, match="revoked"):
        verify_token(tok, secret=_SECRET)


def test_a_local_cutoff_replaces_a_full_revocation_of_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _WriteOnlyCH(fail=True)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    asyncio.run(revocation.revoke("sa:bot"))
    assert revocation.is_revoked("sa:bot", issued_at=int(time.time()) + 3600)
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    asyncio.run(revocation.revoke("sa:bot", issued_before=cutoff))
    # The newest row governs: tokens minted after the cutoff live again.
    assert not revocation.is_revoked("sa:bot", issued_at=int(time.time()))
    assert revocation.is_revoked("sa:bot", issued_at=int(cutoff.timestamp()) - 60)


def test_a_local_revoke_does_not_pretend_the_list_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(revocation, "_loaded", False)
    ch = _WriteOnlyCH(fail=True)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    asyncio.run(revocation.revoke("sa:bot"))
    assert revocation.is_revoked("sa:bot") and not revocation.loaded()


def test_cli_refuses_to_restore_tokens_revoked_by_another_process(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_ch.rows.append(
        {
            "jti": "alice",
            "issued_before": None,
            "expires_at": revocation.revocation_expiry(None),
            "revoked_at": datetime.now(UTC).replace(tzinfo=None),
            "version": 1,
        }
    )
    rc = cli.main(["revoke-token", "--subject", "alice", "--issued-before", "2026-09-17T16:30:00Z"])
    assert rc == 2 and "cannot expire or restore" in capsys.readouterr().err
    assert len(fake_ch.rows) == 1 and fake_ch.rows[0]["issued_before"] is None
    assert (
        cli.main(["revoke-token", "--subject", "bob", "--issued-before", "2026-09-17T16:30:00Z"])
        == 0
    )


@asynccontextmanager
async def _busy(name: str) -> AsyncIterator[None]:
    raise scim.Busy
    yield  # never reached: it makes this a generator


def test_cli_scim_gives_back_a_subject_a_deactivation_caught(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Someone else's `alice`, revoked by a SCIM deactivation of another person.
    asyncio.run(revocation.scim_revoke("alice", reason="scim: deactivated", revoked_by="scim"))
    written_under_lease: list[int] = []

    @asynccontextmanager
    async def held(name: str) -> AsyncIterator[None]:
        assert name.startswith("scim-subject:")
        before = len(fake_ch.inserts)
        yield
        written_under_lease.append(len(fake_ch.inserts) - before)

    monkeypatch.setattr(scim, "_held", held)
    assert revocation.is_revoked("alice", issued_at=int(time.time()) + 60)
    # Past the revocation's second and the skew: the earliest a give-back may take.
    cutoff = datetime.now(UTC).replace(microsecond=0) + timedelta(
        seconds=1 + revocation.CLOCK_SKEW_S
    )
    when = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    rc = cli.main(["revoke-token", "--subject", "alice", "--issued-before", when, "--scim"])
    assert rc == 0
    assert "narrowed SCIM's revocation of subject 'alice'" in capsys.readouterr().out
    rows = {r["jti"]: r for r in fake_ch.rows}
    key = revocation.SCIM_PREFIX + "alice"
    assert set(rows) == {key} and rows[key]["revoked_by"].startswith(
        "cli:"
    )  # its own row untouched
    assert rows[key]["issued_before"] == cutoff.replace(tzinfo=None)
    assert not revocation.is_revoked("alice", issued_at=int(cutoff.timestamp()))
    assert revocation.is_revoked("alice", issued_at=int(cutoff.timestamp()) - 1)
    assert written_under_lease == [1]  # SCIM's lease: never amid a reactivation's lift

    rc = cli.main(["revoke-token", "--subject", "bob", "--issued-before", when, "--scim"])
    assert rc == 1 and "SCIM has not revoked 'bob'" in capsys.readouterr().err
    assert cli.main(["revoke-token", "--subject", "alice", "--scim"]) == 2

    monkeypatch.setattr(scim, "_held", _busy)
    rc = cli.main(["revoke-token", "--subject", "alice", "--issued-before", when, "--scim"])
    assert rc == 1 and "SCIM is changing 'alice'" in capsys.readouterr().err

    async def away(name: str) -> AsyncIterator[None]:
        raise scim.Busy from leases.LeaseUnavailable("the lease store did not answer")
        yield

    monkeypatch.setattr(scim, "_held", asynccontextmanager(away))
    rc = cli.main(["revoke-token", "--subject", "alice", "--issued-before", when, "--scim"])
    assert rc == 1 and "the lease store did not answer" in capsys.readouterr().err


def test_cli_and_servers_name_a_subjects_lease_alike_whatever_their_secret(
    fake_ch: _FakeCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    asyncio.run(revocation.scim_revoke("alice", reason="scim: deactivated", revoked_by="scim"))
    names: list[str] = []

    @asynccontextmanager
    async def held(name: str) -> AsyncIterator[None]:
        names.append(name)
        yield

    monkeypatch.setattr(scim, "_held", held)
    monkeypatch.setattr(settings, "jwt_secret", "the-servers-secret-" + "s" * 32)

    async def server_side() -> None:
        async with scim.subject_leases(["alice"]):
            pass

    asyncio.run(server_side())
    monkeypatch.setattr(settings, "jwt_secret", "")  # a CLI without the servers' secret
    later = datetime.now(UTC) + timedelta(seconds=1 + revocation.CLOCK_SKEW_S)
    when = later.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cli.main(["revoke-token", "--subject", "alice", "--issued-before", when, "--scim"]) == 0
    assert len(names) == 2 and names[0] == names[1]


def test_a_scim_give_back_starts_past_the_revocations_second_and_the_skew(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A token issued in the deactivation's own second is from before it: it stays dead.
    asyncio.run(revocation.scim_revoke("alice", reason="scim: deactivated", revoked_by="scim"))

    @asynccontextmanager
    async def held(name: str) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(scim, "_held", held)
    [row] = [r for r in fake_ch.rows if r["jti"] == revocation.SCIM_PREFIX + "alice"]
    at = row["revoked_at"].replace(tzinfo=None, microsecond=0)

    def give_back(when: datetime) -> int:
        stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
        return cli.main(["revoke-token", "--subject", "alice", "--issued-before", stamp, "--scim"])

    floor = at + timedelta(seconds=1 + revocation.CLOCK_SKEW_S)  # past a clock ahead too
    assert give_back(datetime(2000, 1, 1)) == 2
    assert give_back(at) == 2
    assert "keeps the tokens issued before that dead" in capsys.readouterr().err
    assert give_back(floor - timedelta(seconds=1)) == 2
    assert give_back(floor) == 0
    # Narrowed now, the floor is that cutoff: a later one may follow, an earlier one not.
    assert give_back(floor - timedelta(seconds=1)) == 2
    assert give_back(floor + timedelta(seconds=5)) == 0


def test_a_plain_narrowing_says_scim_still_revokes_it(
    fake_ch: _FakeCH, capsys: pytest.CaptureFixture[str]
) -> None:
    asyncio.run(revocation.scim_revoke("alice", reason="scim: deactivated", revoked_by="scim"))
    when = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert cli.main(["revoke-token", "--subject", "alice", "--issued-before", when]) == 0
    assert "SCIM also revokes 'alice' outright" in capsys.readouterr().err


@pytest.mark.parametrize("subject", ["acme/User:u-1", "acme/user:u-1", "team/everyone"])
def test_no_loom_token_carries_a_subject_spelled_like_an_end_user_key(subject: str) -> None:
    """Minting and verification refuse every subject the revocation CLI
    reserves, an end user's key `<org>/user:<id>` included, so a Loom token
    never shares one or escapes a subject revocation."""
    with pytest.raises(ValueError, match="grant principal"):
        mint_token(subject, "acme", ["shop"], ["viewer"], secret=_SECRET)
    claims = _claims(mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET))

    def _signed(sub: str) -> str:
        return pyjwt.encode(
            {**claims, "sub": sub}, _SECRET, algorithm="HS256", headers={"kid": "default"}
        )

    # The same claims verify under an ordinary subject: the subject alone is refused.
    assert verify_token(_signed("sa:bot"), secret=_SECRET).subject == "sa:bot"
    forged = _signed(subject)
    with pytest.raises(TokenError, match="grant principal"):
        verify_token(forged, secret=_SECRET)


@pytest.mark.parametrize("subject", ["user:u-1", "team/everyone"])
def test_cli_mint_refuses_a_reserved_subject_without_a_traceback(
    fake_ch: _FakeCH,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    subject: str,
) -> None:
    """Refused before the keyring is read: no such token is minted, connected
    or not."""
    from server.auth import keyring

    def _read(**_k: Any) -> None:
        raise AssertionError("the keyring was read for a subject no token may carry")

    monkeypatch.setattr(keyring, "load_for_offline_mint", _read)
    rc = cli.main(["mint-token", "--subject", subject, "--org", "acme", "--namespaces", "shop"])
    assert rc == 2 and "grant principal" in capsys.readouterr().err
