"""The signing keyring: the root secret signs as kid `default` until a key in
loom.signing_keys activates; every loaded key verifies until it is retired."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest

from server.auth import keyring
from server.auth.jwt import TokenError, mint_token, verify_token
from server.core import leases
from server.core.secrets_at_rest import seal
from server.settings import settings


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.inserted: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return self.rows

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if "now64" in sql:
            return {"now": keyring._now()}
        return next((r for r in self.rows if r["kid"] == params["kid"]), None)

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        assert table == "loom.signing_keys"
        self.inserted += rows


def _row(kid: str, *, secret: str = "s" * 40, at: datetime, retired: bool = False) -> dict:
    return {
        "kid": kid,
        "algorithm": "HS256",
        "sealed_key": seal("signing_key", secret),
        "public_pem": "",
        "activate_at": at,
        "retired": retired,
    }


class _HeldLease:
    held = True

    @asynccontextmanager
    async def renewed(self) -> AsyncIterator[_HeldLease]:
        yield self


async def _always_held(*_: Any, **__: Any) -> _HeldLease:
    return _HeldLease()


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "jwt_secret", "r" * 40)
    # The retire claim is cross_replica_fake's to test; these fakes answer no claim.
    monkeypatch.setattr(leases, "take", _always_held)
    keyring._reset_for_tests()
    yield
    keyring._reset_for_tests()


def _load(rows: list[dict[str, Any]]) -> None:
    asyncio.run(keyring.refresh(ch=_CH(rows)))


def _now() -> datetime:
    return keyring._now()


def test_an_empty_keyring_signs_and_verifies_with_the_root_secret() -> None:
    token = mint_token("s", "o", ["ns"], ["writer"])
    assert pyjwt.get_unverified_header(token)["kid"] == "default"
    assert verify_token(token).subject == "s"


def test_the_newest_active_key_signs_and_a_pending_one_only_verifies() -> None:
    _load(
        [_row("k1", at=_now() - timedelta(minutes=5)), _row("k2", at=_now() + timedelta(hours=1))]
    )
    token = mint_token("s", "o", ["ns"], ["writer"])
    assert pyjwt.get_unverified_header(token)["kid"] == "k1"
    pending = pyjwt.encode(
        {"sub": "s", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": 1},
        "s" * 40,
        algorithm="HS256",
        headers={"kid": "k2"},
    )
    assert verify_token(pending).subject == "s"


def test_a_retired_kid_no_longer_verifies_root_included() -> None:
    old = mint_token("s", "o", ["ns"], ["writer"])
    _load([_row("default", at=datetime(1970, 1, 1), retired=True)])
    with pytest.raises(TokenError, match="no HS256 key"):
        verify_token(old)
    with pytest.raises(keyring.NoSigningKey, match="retired") as e:
        mint_token("s", "o", ["ns"], ["writer"])
    assert e.value.status_code == 503


def test_a_lagging_read_never_brings_a_retired_key_back() -> None:
    """A replica that has not seen the retirement answers the next refresh: the
    kid stays retired, and the read itself was sequentially consistent."""
    from server.core.ch import sequential_reads_active

    k1 = _row("k1", at=_now() - timedelta(minutes=5))
    _load([k1, {**k1, "kid": "k2"}])
    _load([{**k1, "retired": True}, {**k1, "kid": "k2"}])
    assert keyring.signing_key().kid == "k2"
    seen: list[bool] = []

    class _Lagging(_CH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            seen.append(sequential_reads_active())
            return await super().query_all(sql, params)

    asyncio.run(keyring.refresh(ch=_Lagging([k1, {**k1, "kid": "k2"}])))
    assert seen == [True]
    assert keyring.verification_keys("k1", "HS256") == []
    assert keyring.signing_key().kid == "k2"


def test_a_key_sealed_under_another_root_is_skipped() -> None:
    row = _row("k1", at=_now() - timedelta(minutes=1))
    settings.jwt_secret = "x" * 40
    _load([row])
    assert keyring.signing_key().kid == "default"


def test_an_hs256_kid_never_verifies_an_rs256_header() -> None:
    _load([_row("k1", at=_now())])
    assert keyring.verification_keys("k1", "RS256") == []
    assert keyring.verification_keys("k1", "HS256") == ["s" * 40]


def test_rotate_writes_a_sealed_key_that_activates_later() -> None:
    ch = _CH()
    before = _now()
    key = asyncio.run(keyring.rotate("RS256", activate_in_s=60, ch=ch))
    row = ch.inserted[-1]
    assert row["kid"] == key.kid and row["algorithm"] == "RS256"
    assert "PRIVATE KEY" not in row["sealed_key"] and "PUBLIC KEY" in row["public_pem"]
    assert key.activate_at - before >= timedelta(seconds=60)


def test_retiring_default_writes_its_row_once_another_key_signs() -> None:
    with pytest.raises(ValueError, match="no key to sign with"):
        asyncio.run(keyring.retire("default", ch=_CH()))
    ch = _CH([_row("k1", at=_now() - timedelta(minutes=1))])
    assert asyncio.run(keyring.retire("default", ch=ch))
    retired = ch.inserted[-1]
    assert retired["kid"] == "default" and retired["retired_at"] is not None
    assert keyring._is_this_root(retired["sealed_key"])
    assert not asyncio.run(keyring.retire("nope", ch=_CH()))


def test_nothing_is_signed_or_verified_before_the_keyring_loads() -> None:
    """A retired root secret is not known yet: a retryable refusal, not a 401,
    and no token a loaded verifier would refuse."""
    from server.auth.jwt import AuthUnavailable
    from server.core.errors import BackendUnavailableError

    token = mint_token("s", "o", ["ns"], ["writer"])
    keyring._reset_for_tests(loaded=False)
    with pytest.raises(AuthUnavailable, match="keyring is not loaded"):
        verify_token(token)
    with pytest.raises(BackendUnavailableError):
        mint_token("s", "o", ["ns"], ["writer"])
    _load([])
    assert verify_token(token).subject == "s"


def test_a_key_that_does_not_unseal_is_warned_about_once(caplog: pytest.LogCaptureFixture) -> None:
    row = _row("k1", at=_now())
    settings.jwt_secret = "x" * 40
    _load([row])
    _load([row])
    assert sum("does not unseal" in r.message for r in caplog.records) == 1


def test_a_failed_read_keeps_the_last_keyring() -> None:
    _load([_row("k1", at=_now())])

    class _Down:
        async def query_all(self, *_: Any) -> list:
            raise ConnectionError("down")

    asyncio.run(keyring.refresh(ch=_Down()))
    assert keyring.signing_key().kid == "k1"


async def test_the_refresh_loop_retries_fast_until_loaded_then_keeps_its_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_keyring_refresh_seconds", 30.0)
    keyring._reset_for_tests(loaded=False)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) == 2:
            keyring._reset_for_tests(loaded=True)
        await real_sleep(0)

    async def _refresh(**_: Any) -> None:
        return None

    monkeypatch.setattr(keyring.asyncio, "sleep", _sleep)
    monkeypatch.setattr(keyring, "refresh", _refresh)
    monkeypatch.setattr(keyring, "_announce_refresh", _refresh)
    task = keyring.start_refresh_loop()
    while len(sleeps) < 3:
        await real_sleep(0)
    task.cancel()
    assert sleeps[:3] == [2.0, 2.0, 30.0]


def _root_row(secret: str, *, retired: bool = False) -> dict[str, Any]:
    """The `default` row as `record_root` writes it for root secret `secret`."""
    mac = hmac.new(secret.encode(), keyring._ROOT_LABEL, hashlib.sha256).hexdigest()
    row = _row("default", at=datetime(1970, 1, 1), retired=retired)
    return {**row, "sealed_key": keyring._ROOT_PREFIX + mac}


def test_a_default_row_never_replaces_the_root_secret() -> None:
    """Kid `default` signs with LOOM_JWT_SECRET, so it must verify with it too,
    whatever a `default` row in the table holds."""
    _load([_root_row("i" * 40), _row("k1", at=_now() + timedelta(days=1))])
    token = mint_token("s", "o", ["ns"], ["writer"])
    assert pyjwt.get_unverified_header(token)["kid"] == "default"
    assert verify_token(token).subject == "s"


def test_a_change_under_another_root_secret_is_refused() -> None:
    """A CLI holding a different (non-dev) root would seal keys the servers
    cannot open, and judge which keys sign from its own view."""
    ch = _CH([_root_row("x" * 40), _row("k1", secret="t" * 40, at=_now())])
    for change in (
        keyring.rotate("HS256", activate_in_s=60, ch=ch),
        keyring.retire("k1", ch=ch),
    ):
        with pytest.raises(ValueError, match="not the servers' root secret"):
            asyncio.run(change)
    assert ch.inserted == []


def test_the_first_change_records_the_root_secret() -> None:
    ch = _CH()
    asyncio.run(keyring.rotate("HS256", activate_in_s=60, ch=ch))
    root = ch.inserted[0]
    assert root["kid"] == "default" and root["retired_at"] is None
    assert keyring._is_this_root(root["sealed_key"])
    _load([{**root, "retired": False}])
    assert keyring.status()["root_row"] == "match"


def test_retire_refuses_when_the_keyring_cannot_be_read() -> None:
    """An unreadable keyring looks empty: it must not approve retiring the last signer."""

    class _Down(_CH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            raise ConnectionError("down")

    ch = _Down([_row("k1", at=_now())])
    with pytest.raises(ValueError, match="could not be read"):
        asyncio.run(keyring.retire("k1", ch=ch))
    assert ch.inserted == []


def test_activation_is_judged_on_the_clickhouse_clock() -> None:
    """A host whose clock runs 10 minutes behind ClickHouse still signs with a
    key ClickHouse says is active."""
    server_now = _now() + timedelta(minutes=10)
    row = {**_row("k1", at=server_now - timedelta(minutes=1)), "server_now": server_now}
    _load([row])
    assert keyring.signing_key().kid == "k1"


def test_status_names_the_signer_and_the_kids_that_do_not_unseal() -> None:
    rows = [_root_row(settings.jwt_secret), _row("k1", at=_now()), _row("k2", at=_now())]
    _load(rows)
    assert keyring.status()["signing_kid"] == "k2" and keyring.status()["root_row"] == "match"
    settings.jwt_secret = "x" * 40
    _load(rows)
    assert keyring.status() == {
        "loaded": True,
        "signing_kid": "default",
        "unsealable": ["k1", "k2"],
        "root_row": "other",
    }


def test_an_offline_mint_signs_as_default_when_the_read_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    keyring._reset_for_tests(loaded=False)

    def _down(_: Any) -> bool:
        raise ConnectionError("no clickhouse")

    assert not keyring.load_for_offline_mint(run=_down)
    assert "signing as kid 'default'" in capsys.readouterr().err
    assert keyring.signing_key().kid == "default"


def test_health_flags_another_root_unsealable_kids_and_no_signer() -> None:
    from server.observability.problems import HealthState, collect_problems

    def codes(state: dict[str, Any]) -> set[str]:
        problems = collect_problems(settings, HealthState(keyring=state))
        assert all(p.fix[0].isupper() and p.fix.endswith(".") for p in problems)
        return {p.code for p in problems} & {
            "keyring_not_loaded",
            "keyring_root_mismatch",
            "signing_keys_unsealable",
            "no_signing_key",
        }

    ok = {"loaded": True, "signing_kid": "k1", "unsealable": [], "root_row": "match"}
    assert codes(ok) == set()
    # A keyring never read (no SELECT grant) refuses every token: health says so.
    assert codes({**ok, "loaded": False, "signing_kid": ""}) == {"keyring_not_loaded"}
    assert codes({**ok, "root_row": "other", "unsealable": ["k1"], "signing_kid": ""}) == {
        "keyring_root_mismatch",
        "signing_keys_unsealable",
        "no_signing_key",
    }


def test_the_doctor_line_reads_the_keyring_with_this_hosts_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """How a CLI or host holding another root secret is noticed."""
    from server.cli import doctor

    def as_json(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **row,
            "activate_at": f"{row['activate_at']:%Y-%m-%d %H:%M:%S}.000",
            "retired": int(row["retired"]),
            "server_now": f"{_now():%Y-%m-%d %H:%M:%S}.000",
        }

    rows = [
        as_json(_root_row(settings.jwt_secret)),
        as_json(_row("k1", at=_now() - timedelta(minutes=1))),
    ]
    monkeypatch.setattr(doctor, "_doctor_ch_query", lambda *_: [dict(r) for r in rows])
    assert doctor._doctor_keyring_check(settings) == (
        "signing_keyring",
        doctor.PASS,
        "signs as k1; 2 row(s)",
    )
    settings.jwt_secret = "x" * 40
    name, grade, detail = doctor._doctor_keyring_check(settings)
    assert grade == doctor.WARN
    assert "not the root" in detail and "k1 do not unseal" in detail
    assert keyring.status()["root_row"] is None  # the doctor's read leaves the process's alone


def test_the_doctor_line_says_why_nothing_signs(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import doctor

    def check(rows: list[dict[str, Any]]) -> str:
        def as_json(row: dict[str, Any]) -> dict[str, Any]:
            return {
                **row,
                "activate_at": f"{row['activate_at']:%Y-%m-%d %H:%M:%S}.000",
                "retired": int(row["retired"]),
                "server_now": f"{_now():%Y-%m-%d %H:%M:%S}.000",
            }

        monkeypatch.setattr(doctor, "_doctor_ch_query", lambda *_: [as_json(r) for r in rows])
        return doctor._doctor_keyring_check(settings)[2]

    assert "'default' is retired" in check([_root_row(settings.jwt_secret, retired=True)])
    settings.jwt_secret = "-----BEGIN PUBLIC KEY-----\nMIIB\n-----END PUBLIC KEY-----\n"
    detail = check([_root_row(settings.jwt_secret)])
    assert "neither an HMAC secret" in detail and "retired" not in detail


def test_recording_the_root_never_outvotes_a_retire_written_meanwhile() -> None:
    ch = _CH()
    asyncio.run(keyring.record_root(ch=ch))
    assert ch.inserted[-1]["version"] == 1  # any retire is newer
    ch.rows = [{**_root_row(settings.jwt_secret), "retired_at": None, "version": 7}]
    asyncio.run(keyring.record_root(ch=ch, force=True))
    assert ch.inserted[-1]["version"] == 8 and ch.inserted[-1]["retired_at"] is None


def _rsa() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return pem, public


def test_a_keyring_past_the_staleness_bound_neither_signs_nor_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.jwt import KeyringUnavailable
    from server.core.errors import BackendUnavailableError

    _load([])
    token = mint_token("s", "o", ["n"], ["writer"])
    monkeypatch.setattr(settings, "jwt_revocation_max_stale_seconds", 30)
    keyring._view = dataclasses.replace(keyring._view, read_at=keyring.time.monotonic() - 31)
    assert keyring.stale()
    with pytest.raises(BackendUnavailableError):
        mint_token("s", "o", ["n"], ["writer"])
    with pytest.raises(KeyringUnavailable):
        verify_token(token)
    _load([])  # a read lands: trusted again
    assert verify_token(token).subject == "s"


def test_a_pem_root_signs_rs256_as_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem, public = _rsa()
    monkeypatch.setattr(settings, "jwt_secret", pem)
    keyring._reset_for_tests()
    _load([])
    token = mint_token("s", "o", ["n"], ["writer"])
    assert pyjwt.get_unverified_header(token) == {"alg": "RS256", "kid": "default", "typ": "JWT"}
    assert pyjwt.decode(token, public, algorithms=["RS256"], audience=settings.jwt_audience)
    assert verify_token(token).subject == "s"
    assert keyring.public_keys()["default"] == public
    # A retired `default` takes the PEM root with it.
    _load([{**_row("default", at=_now()), "sealed_key": "", "retired": True}])
    with pytest.raises(keyring.NoSigningKey):
        keyring.signing_key()
    with pytest.raises(TokenError):
        verify_token(token)


def test_a_gateway_default_key_still_verifies_beside_the_root_rs256_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    root_pem, _ = _rsa()
    gateway_pem, gateway_public = _rsa()
    monkeypatch.setattr(settings, "jwt_secret", root_pem)
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"default": gateway_public}))
    keyring._reset_for_tests()
    _load([])
    claims = {"sub": "gw", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": 1}
    gateway_token = pyjwt.encode(claims, gateway_pem, algorithm="RS256", headers={"kid": "default"})
    assert verify_token(gateway_token).subject == "gw"
    assert verify_token(mint_token("s", "o", ["n"], ["writer"])).subject == "s"
    other_pem, _ = _rsa()
    forged = pyjwt.encode(claims, other_pem, algorithm="RS256", headers={"kid": "default"})
    with pytest.raises(TokenError):
        verify_token(forged)


def test_a_retire_whose_claim_lapsed_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Lapsed(_HeldLease):
        held = False

    async def _take(*_: Any, **__: Any) -> _HeldLease:
        return _Lapsed()

    monkeypatch.setattr(leases, "take", _take)
    ch = _CH([_row("k1", at=_now() - timedelta(hours=1))])
    with pytest.raises(ValueError, match="lapsed"):
        asyncio.run(keyring.retire("k1", ch=ch))
    assert not [r for r in ch.inserted if r.get("retired_at")]


def test_the_staleness_limit_outlasts_a_failed_keyring_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pydantic

    from server.settings import Settings

    monkeypatch.setenv("LOOM_JWT_KEYRING_REFRESH_SECONDS", "300")
    monkeypatch.setenv("LOOM_JWT_REVOCATION_MAX_STALE_SECONDS", "600")
    with pytest.raises(pydantic.ValidationError, match="at least three times"):
        Settings()
    monkeypatch.setenv("LOOM_JWT_REVOCATION_MAX_STALE_SECONDS", "900")
    assert Settings().jwt_keyring_refresh_seconds == 300


def test_retiring_default_leaves_the_gateways_default_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    gateway_pem, gateway_public = _rsa()
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"default": gateway_public}))
    _load(
        [
            {**_row("default", at=_now()), "sealed_key": "", "retired": True},
            _row("k1", at=_now() - timedelta(hours=1)),
        ]
    )
    claims = {"sub": "gw", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": 1}
    gateway = pyjwt.encode(claims, gateway_pem, algorithm="RS256", headers={"kid": "default"})
    assert verify_token(gateway).subject == "gw"
    root = pyjwt.encode(claims, "r" * 40, algorithm="HS256", headers={"kid": "default"})
    with pytest.raises(TokenError):
        verify_token(root)


def test_a_retire_cancelled_by_its_lost_claim_is_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Lost(_HeldLease):
        @asynccontextmanager
        async def renewed(self) -> AsyncIterator[_HeldLease]:
            raise leases.LeaseLost("signing-key-retire")
            yield self

    async def _take(*_: Any, **__: Any) -> _HeldLease:
        return _Lost()

    monkeypatch.setattr(leases, "take", _take)
    with pytest.raises(ValueError, match="may have been written"):
        asyncio.run(keyring.retire("k1", ch=_CH([_row("k1", at=_now())])))


def test_a_gateway_entry_of_the_roots_own_key_dies_with_the_retire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gateway map's `default` entry holding the PEM root's public key must
    not keep the root's tokens alive once `default` is retired."""
    import json

    root_pem, root_public = _rsa()
    monkeypatch.setattr(settings, "jwt_secret", root_pem)
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"default": root_public}))
    keyring._reset_for_tests()
    _load([_row("k1", at=_now() - timedelta(hours=1))])
    claims = {"sub": "s", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": 1}
    token = pyjwt.encode(claims, root_pem, algorithm="RS256", headers={"kid": "default"})
    assert verify_token(token).subject == "s"
    _load(
        [
            {**_row("default", at=_now()), "sealed_key": "", "retired": True},
            _row("k1", at=_now() - timedelta(hours=1)),
        ]
    )
    with pytest.raises(TokenError):
        verify_token(token)


def test_retiring_the_imported_root_key_stops_its_default_tokens_in_the_gateway_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upgrade imports the old PEM as `default-rs256` and keeps the gateway
    map that verified its `default` tokens; retiring `default-rs256` must stop
    them under that map's `default` too."""
    import json

    old_pem, old_public = _rsa()
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"default": old_public}))
    retired = {
        **_row(keyring.ROOT_RS256_KID, at=_now() - timedelta(days=1), retired=True),
        "algorithm": "RS256",
        "public_pem": old_public,
    }
    _load([retired, _row("k1", at=_now() - timedelta(hours=1))])
    claims = {"sub": "old", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": 1}
    token = pyjwt.encode(claims, old_pem, algorithm="RS256", headers={"kid": "default"})
    with pytest.raises(TokenError):
        verify_token(token)


@pytest.mark.parametrize(
    "layout",
    [str.strip, lambda pem: pem.replace("\n", "\r\n"), "pkcs1"],
    ids=["no-trailing-newline", "crlf", "pkcs1"],
)
def test_a_retired_key_stays_retired_in_any_pem_layout_of_the_gateway_map(
    monkeypatch: pytest.MonkeyPatch, layout: Any
) -> None:
    import json

    from cryptography.hazmat.primitives import serialization

    old_pem, old_public = _rsa()
    if layout == "pkcs1":
        key = serialization.load_pem_public_key(old_public.encode())
        listed = key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.PKCS1
        ).decode()
    else:
        listed = layout(old_public)
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"default": listed}))
    retired = {
        **_row(keyring.ROOT_RS256_KID, at=_now() - timedelta(days=1), retired=True),
        "algorithm": "RS256",
        "public_pem": old_public,
    }
    _load([retired, _row("k1", at=_now() - timedelta(hours=1))])
    claims = {"sub": "old", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": 1}
    token = pyjwt.encode(claims, old_pem, algorithm="RS256", headers={"kid": "default"})
    with pytest.raises(TokenError):
        verify_token(token)


def test_the_previous_root_never_verifies_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """LOOM_JWT_SECRET_PREVIOUS only opens sealed values: a root replaced because
    it leaked must not keep minting accepted tokens."""
    monkeypatch.setattr(settings, "jwt_secret_previous", "p" * 40)
    assert keyring.verification_keys("default", "HS256") == [str(settings.jwt_secret)]


def test_a_key_resealed_under_a_new_root_keeps_verifying_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During a rotation a process on the old root reads keys `loom secrets
    reseal` sealed under the new one: the key it opened before still verifies,
    rather than its tokens getting 401 until the process is replaced."""
    _load([_row("k1", secret="k" * 40, at=_now())])
    assert keyring.verification_keys("k1", "HS256") == ["k" * 40]
    real = settings.jwt_secret
    monkeypatch.setattr(settings, "jwt_secret", "n" * 40)
    resealed = _row("k1", secret="k" * 40, at=_now())  # sealed under the new root
    monkeypatch.setattr(settings, "jwt_secret", real)
    _load([resealed])
    assert keyring.verification_keys("k1", "HS256") == ["k" * 40]
    assert "k1" not in keyring.status()["unsealable"]
