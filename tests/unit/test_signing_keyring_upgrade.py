"""Upgrading to the keyring: an RS256 deployment of the build before 033, a
gateway's RS256 tokens, an old-build read of the root record, and the
keyring's guards (another root, the leader-only root record, the rotate
floor from every process's refresh interval)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest

from server.auth import keyring
from server.auth.jwt import (
    KeyMapEnvRefused,
    TokenError,
    mint_token,
    require_no_key_map_env,
    verify_token,
)
from server.core import leases
from server.core.secrets_at_rest import seal
from server.settings import settings


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.inserted: list[dict[str, Any]] = []
        self.tables: list[str] = []
        self.server_now = keyring._now()

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return [{**r, "server_now": self.server_now} for r in self.rows]

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        if sql.startswith("SELECT now64"):
            return {"now": self.server_now}
        return next((r for r in self.rows if r["kid"] == params["kid"]), None)

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.tables.append(table)
        self.inserted += rows


def _claims() -> dict[str, Any]:
    return {"sub": "s", "iss": settings.jwt_issuer, "aud": settings.jwt_audience, "iat": 1}


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
    keyring._hmac_usable.cache_clear()
    yield
    keyring._reset_for_tests()


@pytest.fixture(scope="module")
def rsa_pair() -> tuple[str, str]:
    return keyring._new_key("RS256")


def _loaded_from(ch: _CH) -> None:
    ch.rows = [{**r, "retired": r.get("retired_at") is not None} for r in ch.inserted]
    asyncio.run(keyring.refresh(ch=ch))


# -- gateway RS256 tokens of kid `default` ------------------------------------


def test_a_gateway_rs256_token_without_a_kid_verifies(
    monkeypatch: pytest.MonkeyPatch, rsa_pair: tuple[str, str]
) -> None:
    """A missing kid means `default`, which is the HS256 root: an RS256 token
    used to be refused before the gateway map was read."""
    private, public = rsa_pair
    monkeypatch.setattr(settings, "jwt_public_keys_json", json.dumps({"default": public}))
    token = pyjwt.encode(_claims(), private, algorithm="RS256")
    assert "kid" not in pyjwt.get_unverified_header(token)
    assert verify_token(token).subject == "s"
    as_default = pyjwt.encode(_claims(), private, algorithm="RS256", headers={"kid": "default"})
    assert verify_token(as_default).subject == "s"


def test_a_pem_root_signs_rs256_and_any_other_pem_answers_no_signing_key(
    monkeypatch: pytest.MonkeyPatch, rsa_pair: tuple[str, str]
) -> None:
    """The build before 033 signed RS256 with a PEM LOOM_JWT_SECRET, and this
    one does too, as kid `default`. Never HS256: PyJWT raised InvalidKeyError,
    a 500 on every sign-in."""
    monkeypatch.setattr(settings, "jwt_secret", rsa_pair[0])
    keyring._reset_for_tests()
    token = mint_token("s", "o", ["ns"], ["writer"])
    assert pyjwt.get_unverified_header(token)["alg"] == "RS256"
    assert verify_token(token).subject == "s"
    forged = pyjwt.encode(_claims(), "x" * 40, algorithm="HS256", headers={"kid": "default"})
    with pytest.raises(TokenError, match="no HS256 key"):
        verify_token(forged)
    monkeypatch.setattr(settings, "jwt_secret", rsa_pair[1])  # a public key signs nothing
    keyring._reset_for_tests()
    with pytest.raises(keyring.NoSigningKey, match="neither an HMAC secret") as e:
        mint_token("s", "o", ["ns"], ["writer"])
    assert e.value.code == "NO_SIGNING_KEY" and e.value.status_code == 503


def test_an_rs256_deployment_imports_its_default_key_and_keeps_its_tokens(
    monkeypatch: pytest.MonkeyPatch, rsa_pair: tuple[str, str]
) -> None:
    """The migration path: `import --algorithm RS256 --active default` with
    the PEM root stores it as `default-rs256`, which signs from now and
    verifies the old `default` tokens; the PEM stays the sealing root."""
    private, public = rsa_pair
    monkeypatch.setattr(settings, "jwt_secret", private)
    old = pyjwt.encode(_claims(), private, algorithm="RS256", headers={"kid": "default"})
    ch = _CH()
    done = asyncio.run(keyring.import_keys({"default": private}, "default", "RS256", ch=ch))
    assert done == [keyring.ROOT_RS256_KID]
    _loaded_from(ch)
    assert verify_token(old).subject == "s"
    token = mint_token("s", "o", ["ns"], ["writer"])
    # Signed as `default`: a process of the earlier build, still serving during the
    # rollout, verifies it with its LOOM_JWT_PUBLIC_KEYS_JSON["default"].
    assert pyjwt.get_unverified_header(token) == {"alg": "RS256", "kid": "default", "typ": "JWT"}
    assert verify_token(token).subject == "s"
    assert pyjwt.decode(token, public, algorithms=["RS256"], audience=settings.jwt_audience)
    assert keyring.public_keys() == {keyring.ROOT_RS256_KID: public, "default": public}
    # Retiring the imported key stops the old `default` tokens too.
    ch.inserted = [{**r, "retired_at": keyring._now()} for r in ch.inserted]
    _loaded_from(ch)
    with pytest.raises(TokenError, match="no RS256 key"):
        verify_token(old)


def test_import_refuses_a_map_with_default_and_its_alias(rsa_pair: tuple[str, str]) -> None:
    with pytest.raises(ValueError, match="both"):
        asyncio.run(
            keyring.import_keys(
                {"default": rsa_pair[0], keyring.ROOT_RS256_KID: rsa_pair[0]},
                "default",
                "RS256",
                ch=_CH(),
            )
        )


@pytest.mark.parametrize(
    ("env", "refused"),
    [
        ({"LOOM_JWT_ALGORITHM": "RS256"}, False),
        ({"LOOM_JWT_ALGORITHM": "RS256", "LOOM_JWT_SECRETS_JSON": '{"k1": "x"}'}, True),
        ({"LOOM_JWT_ALGORITHM": "HS256"}, False),
        ({"LOOM_JWT_ALGORITHM": "hs256", "LOOM_JWT_ACTIVE_KID": "default"}, False),
    ],
)
def test_boot_refuses_the_former_rs256_mode(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], refused: bool
) -> None:
    for name in ("LOOM_JWT_SECRETS_JSON", "LOOM_JWT_ACTIVE_KID", "LOOM_JWT_ALGORITHM"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    if not refused:
        require_no_key_map_env(None)
        return
    with pytest.raises(KeyMapEnvRefused, match="import --algorithm RS256"):
        require_no_key_map_env(None)


def test_the_cli_import_falls_back_to_the_root_as_the_active_kid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The build before 033 read an empty LOOM_JWT_SECRETS_JSON as
    {LOOM_JWT_ACTIVE_KID: LOOM_JWT_SECRET}."""
    import argparse

    from server.cli import portability

    monkeypatch.delenv("LOOM_JWT_SECRETS_JSON", raising=False)
    args = argparse.Namespace(map_env="LOOM_JWT_SECRETS_JSON", active="default")
    assert portability._import_map(args) == {"default": settings.jwt_secret}
    monkeypatch.setenv("LOOM_JWT_SECRETS_JSON", json.dumps({"k1": "o" * 40}))
    assert portability._import_map(args) == {"k1": "o" * 40}


# -- the root record ------------------------------------------------------------


def test_the_root_record_is_an_hmac() -> None:
    record = keyring._root_record()
    assert record.startswith("hmac-sha256:") and settings.jwt_secret not in record
    assert keyring._is_this_root(record)
    settings.jwt_secret = "x" * 40
    assert not keyring._is_this_root(record)


def test_an_offline_mint_under_another_root_is_refused() -> None:
    """It signed with a key no server opens and exited 0."""
    ch = _CH(
        [
            {
                "kid": "default",
                "algorithm": "HS256",
                "sealed_key": keyring._root_record(),
                "public_pem": "",
                "activate_at": datetime(1970, 1, 1),
                "retired": False,
            }
        ]
    )
    settings.jwt_secret = "x" * 40
    keyring._reset_for_tests(loaded=False)
    with pytest.raises(ValueError, match="not the servers' root secret"):
        keyring.load_for_offline_mint(run=lambda go: asyncio.run(go(ch)))


def test_mint_token_cli_exits_1_under_another_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server import cli
    from server.cli import access

    row = {
        "kid": "default",
        "algorithm": "HS256",
        "sealed_key": keyring._root_record(),
        "public_pem": "",
        "activate_at": datetime(1970, 1, 1),
        "retired": False,
    }
    monkeypatch.setattr(settings, "jwt_secret", "y" * 40)
    monkeypatch.setattr(access, "run_connected", lambda go: asyncio.run(go(_CH([row]))))
    rc = cli.main(["mint-token", "--subject", "a@x", "--org", "o", "--namespaces", "n"])
    out, err = capsys.readouterr()
    assert rc == 1 and out == "" and "not the servers' root secret" in err


def test_only_the_workers_leader_records_the_root_and_it_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replica's first boot with a mistyped secret must not become the
    deployment's root; a refused write is not retried every refresh."""
    from server.core import ch as ch_module

    class _Refusing(_CH):
        async def insert(self, *a: Any, **k: Any) -> None:
            self.tables.append("refused")
            raise PermissionError("ACCESS_DENIED")

    client = _Refusing()
    monkeypatch.setattr(ch_module, "get_client", lambda: client)
    keyring._view = keyring._View(root_row="absent")
    leader = {"held": False}
    monkeypatch.setattr(keyring, "_is_leader", lambda: leader["held"])
    asyncio.run(keyring._record_root_if_absent())
    assert client.tables == []
    leader["held"] = True
    for _ in range(3):
        asyncio.run(keyring._record_root_if_absent())
    assert client.tables == ["refused"]  # one try, then the backoff holds
    keyring._record_retry_at = 0.0
    ok = _CH()
    monkeypatch.setattr(ch_module, "get_client", lambda: ok)
    asyncio.run(keyring._record_root_if_absent())
    assert (
        keyring._is_this_root(ok.inserted[-1]["sealed_key"]) and keyring._view.root_row == "match"
    )


def test_retire_stamps_the_clickhouse_clock() -> None:
    ch = _CH(
        [
            {
                "kid": "k1",
                "algorithm": "HS256",
                "sealed_key": seal("signing_key", "k" * 40),
                "public_pem": "",
                "activate_at": keyring._now() - timedelta(hours=1),
                "retired": False,
                "created_at": keyring._now(),
                "retired_at": None,
            }
        ]
    )
    ch.server_now = keyring._now() + timedelta(minutes=10)
    assert asyncio.run(keyring.retire("default", ch=ch))
    assert ch.inserted[-1]["retired_at"] == ch.server_now


# -- the rotate floor -----------------------------------------------------------


def test_each_process_records_its_refresh_interval_hourly(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import ch as ch_module

    client = _CH()
    monkeypatch.setattr(ch_module, "get_client", lambda: client)
    monkeypatch.setattr(settings, "jwt_keyring_refresh_seconds", 300.0)
    asyncio.run(keyring._announce_refresh())
    asyncio.run(keyring._announce_refresh())
    assert client.tables == [keyring.READERS_TABLE]
    assert client.inserted[0]["refresh_seconds"] == 300.0


def test_rotate_waits_out_the_slowest_servers_refresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The floor came from the CLI's own setting: servers refreshing every
    300 s accepted `--activate-in 60` and answered 401 for minutes."""
    import argparse

    from server.cli import portability

    class _Readers(_CH):
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            if "keyring_readers" in sql:
                return {"s": 300.0}
            return await super().query_one(sql, params)

    monkeypatch.setattr(settings, "jwt_keyring_refresh_seconds", 30.0)
    args = argparse.Namespace(signing_cmd="rotate", algorithm="HS256", activate_in=60.0)
    ch = _Readers()
    assert asyncio.run(portability._signing_key_cmd(args, keyring, ch)) == 1
    assert "at least 600 s (twice the servers' slowest" in capsys.readouterr().err
    assert ch.inserted == []
    args.activate_in = 600.0
    assert asyncio.run(portability._signing_key_cmd(args, keyring, ch)) == 0
    # Servers refreshing faster than this host set the floor, not this host.
    monkeypatch.setattr(settings, "jwt_keyring_refresh_seconds", 900.0)
    assert asyncio.run(portability._signing_key_cmd(args, keyring, ch)) == 0


def test_a_late_import_waits_out_the_servers_refresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no process of this build serving the import activates at once;
    once one serves it would sign with a kid that process has not loaded."""
    import argparse

    from server.cli import portability

    readers: dict[str, Any] = {"s": 0}

    class _Readers(_CH):
        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            if "keyring_readers" in sql:
                return readers
            return await super().query_one(sql, params)

    monkeypatch.delenv("LOOM_JWT_SECRETS_JSON", raising=False)
    args = argparse.Namespace(
        signing_cmd="import",
        algorithm="HS256",
        activate_in=0.0,
        active="k1",
        map_env="LOOM_JWT_SECRETS_JSON",
    )
    monkeypatch.setenv("LOOM_JWT_SECRETS_JSON", json.dumps({"k1": "o" * 40}))
    assert asyncio.run(portability._signing_key_cmd(args, keyring, _Readers())) == 0
    readers["s"] = 300.0
    ch = _Readers()
    assert asyncio.run(portability._signing_key_cmd(args, keyring, ch)) == 1
    assert "at least 600 s" in capsys.readouterr().err and ch.inserted == []


def test_an_unloadable_keyring_is_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import portability
    from server.core.errors import BackendUnavailableError

    async def down(*_: Any, **__: Any) -> int:
        raise BackendUnavailableError("the signing keyring is not loaded yet; retry shortly")

    monkeypatch.setattr(portability, "_signing_key_cmd", down)
    monkeypatch.setattr(portability, "run_connected", lambda run: asyncio.run(run(object())))
    assert portability._cmd_signing_key(argparse.Namespace(signing_cmd="list")) == 1
    assert "not loaded yet" in capsys.readouterr().err


def test_the_refresh_interval_is_at_least_one_second() -> None:
    from pydantic import ValidationError

    from server.settings import Settings

    with pytest.raises(ValidationError, match="jwt_keyring_refresh_seconds"):
        Settings(_env_file=None, jwt_keyring_refresh_seconds=0)  # type: ignore[call-arg]


def test_only_the_deployments_leader_counts_as_the_root_recorder(monkeypatch) -> None:
    from server.workers import leader

    class _Held:
        held = True

    monkeypatch.setattr(leader, "current", lambda: _Held())
    monkeypatch.setattr(settings, "deployment", False)
    assert keyring._is_leader() is False  # a test or iso server's own lease
    monkeypatch.setattr(settings, "deployment", True)
    assert keyring._is_leader() is True
