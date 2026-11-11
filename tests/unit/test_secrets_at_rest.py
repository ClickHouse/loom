"""Stored credentials are sealed under a per-purpose key derived from the JWT
secret; the derivation matches what the code and tracker planes stored before
they shared it, so existing ciphertexts still open."""

from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
from cryptography.fernet import Fernet

from server.core.secrets_at_rest import seal, unseal
from server.settings import settings


def _legacy(salt: bytes, secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(salt + secret.encode()).digest()))


@pytest.mark.parametrize(
    ("purpose", "salt"),
    [
        ("code_repos.token", b"loom.code_repos.token:"),
        ("qa_sources.password", b"loom.qa_sources.password:"),
    ],
)
def test_stored_ciphertexts_still_open(
    monkeypatch: pytest.MonkeyPatch, purpose: str, salt: bytes
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    stored = _legacy(salt, "unit-test-secret").encrypt(b"s3cret").decode()
    assert unseal(purpose, stored) == "s3cret"
    assert unseal(purpose, seal(purpose, "s3cret")) == "s3cret"


@pytest.fixture(autouse=True)
def _no_analysis_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reseal holds the analysis and assistant leases over their rows; these
    fakes model the sealed tables, not the lease store."""
    import contextlib

    from server.insights.app import analysis

    monkeypatch.setattr(analysis, "lease", lambda ch=None: contextlib.nullcontext())
    from server.assistant.app import service

    monkeypatch.setattr(service, "lease", lambda ch=None: contextlib.nullcontext())


def test_purposes_do_not_open_each_other_and_empty_stays_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    enc = seal("code_repos.token", "ghp_x")
    assert "ghp_x" not in enc
    assert unseal("qa_sources.password", enc) == ""
    assert seal("code_repos.token", "") == "" and unseal("code_repos.token", "") == ""


def test_no_secret_refuses_to_seal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "")
    with pytest.raises(RuntimeError):
        seal("code_repos.token", "x")


# -- a root-secret rotation ----------------------------------------------------


def test_the_previous_root_still_opens_and_reseal_moves_values_to_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.secrets_at_rest import resealed

    monkeypatch.setattr(settings, "jwt_secret", "old-root-secret")
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    old = seal("code_repos.token", "s3cret")
    monkeypatch.setattr(settings, "jwt_secret", "new-root-secret")
    assert unseal("code_repos.token", old, warn=False) == ""  # without the previous root: lost
    monkeypatch.setattr(settings, "jwt_secret_previous", "old-root-secret")
    assert unseal("code_repos.token", old) == "s3cret"
    new = resealed("code_repos.token", old)
    assert new is not None and resealed("code_repos.token", new) is None
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    assert unseal("code_repos.token", new) == "s3cret"  # opens with the new root alone
    assert resealed("code_repos.token", "") is None
    assert resealed("code_repos.token", _legacy(b"x", "another").encrypt(b"y").decode()) is None


def test_every_sealing_purpose_is_resealed() -> None:
    """Every purpose the server seals under is in `loom secrets reseal`, or a
    rotation leaves those values opening only with the previous root. The
    purposes are read from the code: each `seal(...)` call's first argument, a
    literal or a module constant."""
    import ast
    from pathlib import Path

    from server.cli.secrets import SEALED

    found: set[str] = set()
    for path in Path("server").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = {
            t.id: n.value.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
            for t in n.targets
            if isinstance(t, ast.Name) and isinstance(n.value.value, str)
        }
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "seal" and n.args:
                a = n.args[0]
                if isinstance(a, ast.Constant):
                    found.add(a.value)
                elif isinstance(a, ast.Name) and a.id in consts:
                    found.add(consts[a.id])
    assert found and found <= {purpose for *_, purpose, _ in SEALED} | set(SHORT_LIVED), found


# Purposes sealed only into values that expire well within a rotation (a cookie,
# a Keeper claim with a TTL): nothing stored to reseal. Each with its reason.
SHORT_LIVED: dict[str, str] = {
    "oidc sign-in in flight": "the state cookie of an SSO round trip, refused after ten minutes",
}


@pytest.mark.asyncio
async def test_reseal_rewrites_only_what_the_previous_root_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import secrets as cli

    monkeypatch.setattr(settings, "jwt_secret", "old-root-secret")
    stale = seal("qa_sources.password", "pw")
    monkeypatch.setattr(settings, "jwt_secret", "new-root-secret")
    monkeypatch.setattr(settings, "jwt_secret_previous", "old-root-secret")
    current = seal("qa_sources.password", "pw2")

    class Ch:
        inserted: list = []
        rebuilding: list = []

        async def query_all(self, sql: str, params: dict | None = None) -> list[dict]:
            if "state IN" in sql:
                return self.rebuilding
            if "qa_sources" not in sql:
                return []
            return [
                {"org": "o", "name": "a", "password_enc": stale, "version": 4},
                {"org": "o", "name": "b", "password_enc": current, "version": 9},
            ]

        async def insert(self, table: str, rows: list[dict], settings: dict) -> None:
            self.inserted.append((table, rows))

        async def query_one(self, sql: str) -> dict | None:
            return None

    ch = Ch()
    counts, lost, _ = await cli.reseal_all(ch, dry_run=True)
    assert counts["loom.qa_sources.password_enc"] == 1 and lost == {} and ch.inserted == []
    await cli.reseal_all(ch, dry_run=False)
    [(table, rows)] = ch.inserted
    assert table == "loom.qa_sources" and [r["name"] for r in rows] == ["a"]
    assert rows[0]["version"] == 5  # just above the row read: a later edit stays newer
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    assert unseal("qa_sources.password", rows[0]["password_enc"]) == "pw"


@pytest.mark.asyncio
async def test_reseal_reaches_a_rebuild_recipe_and_waits_out_a_running_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from server.cli import secrets as cli

    monkeypatch.setattr(settings, "jwt_secret", "old-root-secret")
    progress = json.dumps({"recipe": {"views": seal("provider-rebuild-views", "[]")}, "n": 1})
    monkeypatch.setattr(settings, "jwt_secret", "new-root-secret")
    monkeypatch.setattr(settings, "jwt_secret_previous", "old-root-secret")

    class Ch:
        inserted: list = []
        rebuilding: list = [{"config_id": "c"}]

        async def query_all(self, sql: str, params: dict | None = None) -> list[dict]:
            if "state IN" in sql:
                return self.rebuilding
            if "provider_configuration" in sql and "progress" in sql:
                return [{"config_id": "c", "envelope": "", "progress": progress, "version": 1}]
            return []

        async def insert(self, table: str, rows: list[dict], settings: dict) -> None:
            self.inserted.append((table, rows))

        async def query_one(self, sql: str) -> dict | None:
            return None

    ch = Ch()
    with pytest.raises(RuntimeError, match="rebuild is under way"):
        await cli.reseal_all(ch, dry_run=False)
    ch.rebuilding = []
    await cli.reseal_all(ch, dry_run=False)
    [(table, [row])] = ch.inserted
    views = json.loads(row["progress"])["recipe"]["views"]
    monkeypatch.setattr(settings, "jwt_secret_previous", "")
    assert unseal("provider-rebuild-views", views) == "[]"
    assert json.loads(row["progress"])["n"] == 1


@pytest.mark.asyncio
async def test_a_wrong_previous_root_is_refused_and_unopened_values_are_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mistyped LOOM_JWT_SECRET_PREVIOUS reseals nothing: refuse it against the
    root the deployment recorded, and count what opens with neither root, so
    the old root is never dropped with values only it opens."""
    from server.auth import keyring
    from server.cli import secrets as cli

    monkeypatch.setattr(settings, "jwt_secret", "old-root-secret")
    stale = seal("qa_sources.password", "pw")
    monkeypatch.setattr(settings, "jwt_secret", "new-root-secret")
    recorded = keyring._root_record("old-root-secret")

    class Ch:
        inserted: list = []

        async def query_one(self, sql: str) -> dict | None:
            return {"sealed_key": recorded}

        async def query_all(self, sql: str, params: dict | None = None) -> list[dict]:
            if "qa_sources" in sql:
                return [{"org": "o", "name": "a", "password_enc": stale, "version": 4}]
            return []

        async def insert(self, table: str, rows: list[dict], settings: dict) -> None:
            self.inserted.append((table, rows))

    monkeypatch.setattr(settings, "jwt_secret_previous", "typo-root-secret")
    with pytest.raises(cli.NotTheRoot):
        await cli.reseal_all(Ch(), dry_run=True)
    recorded = keyring._root_record("new-root-secret")  # after the publish's record-root
    _, lost, checked = await cli.reseal_all(Ch(), dry_run=True)
    # Lost before this rotation: named so it can be entered again, not fatal,
    # since the previous root was checked against the recorded one.
    assert lost == {"loom.qa_sources.password_enc": ["o/a"]} and checked
    monkeypatch.setattr(settings, "jwt_secret_previous", "old-root-secret")
    counts, lost, _ = await cli.reseal_all(Ch(), dry_run=True)
    assert counts["loom.qa_sources.password_enc"] == 1 and lost == {}
    recorded = ""  # no recorded root: a value neither root opens may mean a wrong previous
    monkeypatch.setattr(settings, "jwt_secret_previous", "typo-root-secret")
    _, lost, checked = await cli.reseal_all(Ch(), dry_run=True)
    assert lost and not checked


def test_reseal_check_fails_while_values_need_the_previous_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from server.cli import secrets as cli

    monkeypatch.setattr(settings, "jwt_secret_previous", "old-root-secret")
    results = iter([({"loom.qa_sources.password_enc": 2}, {}, True), ({}, {}, True)])

    async def fake(ch, *, dry_run):
        assert dry_run
        return next(results)

    async def none_in_flight(ch):
        return 0, 0

    monkeypatch.setattr(cli, "reseal_all", fake)
    monkeypatch.setattr(cli, "in_flight", none_in_flight)
    monkeypatch.setattr(cli, "run_connected", lambda go, **_declared: asyncio.run(go(object())))
    assert cli._cmd_reseal(argparse.Namespace(dry_run=False, check=True)) == 1
    assert cli._cmd_reseal(argparse.Namespace(dry_run=False, check=True)) == 0


def test_reseal_check_waits_for_sign_ins_in_flight_under_the_previous_root(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A code, device grant or approval in Keeper carries the IdP's refresh
    token sealed: one only the previous root opens keeps the rotation open
    until it expires, and the check names when the last one does. A spent
    one is no sign-in in flight."""
    import argparse
    import json

    from server.cli import secrets as cli

    monkeypatch.setattr(settings, "jwt_secret", "old-root-secret")
    sealed = seal("oidc refresh token", "rt")
    old = json.dumps({"data": {"kind": "connect", "upstream_refresh": sealed}, "exp": 1})
    # A code already redeemed: its sign-in is done, the record only marks it spent.
    spent = json.dumps({"upstream_refresh": sealed, "spent": True})
    monkeypatch.setattr(settings, "jwt_secret", "new-root-secret")
    monkeypatch.setattr(settings, "jwt_secret_previous", "old-root-secret")
    new = json.dumps({"kind": "connect", "upstream_refresh": seal("oidc refresh token", "rt")})
    keeper = [
        {"value": old, "expires_at": 1_900_000_000},
        {"value": new, "expires_at": 1_900_000_900},
        {"value": "3:rid", "expires_at": 1_900_009_000},  # a session's pointer, nothing sealed
        {"value": spent, "expires_at": 1_900_005_000},
    ]

    class Ch:
        async def query_one(self, sql: str) -> dict | None:
            return None

        async def query_all(self, sql: str, params: dict | None = None) -> list[dict]:
            return list(keeper) if "oauth_signin_state" in sql else []

    monkeypatch.setattr(cli, "run_connected", lambda go, **_declared: asyncio.run(go(Ch())))
    assert cli._cmd_reseal(argparse.Namespace(dry_run=False, check=True)) == 1
    out = capsys.readouterr().out
    assert "1 sign-in in flight" in out and "2030-03-17T17:46:40Z" in out
    keeper.pop(0)  # expired and swept
    assert cli._cmd_reseal(argparse.Namespace(dry_run=False, check=True)) == 0
