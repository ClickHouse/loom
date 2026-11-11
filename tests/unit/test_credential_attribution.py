"""Attribution is bound to the credential. A `consumer` token
claim overrides the request's own field (the way the traffic_class claim
does); rows say whether their tag was `claimed`, `self-reported` or the
subject `class`; `token_id` is the jti or the token's sha256 prefix, so the
exposure and audit ledgers gain a credential axis. CH-free."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo, TokenError, mint_token, token_id_of, verify_token
from server.observability import consumer as cons
from server.observability import ledger
from server.settings import settings

_SECRET = "s3" * 16


def _auth(**kw: Any) -> AuthInfo:
    base: dict[str, Any] = dict(
        subject="sa:shop-bot",
        org="acme",
        namespaces=("shop",),
        roles=(),
        is_service_account=True,
    )
    base.update(kw)
    return AuthInfo(**base)


@pytest.fixture(autouse=True)
def _clean_context():
    cons.set_current_consumer("")
    set_current_auth(None)
    yield
    cons.set_current_consumer("")
    set_current_auth(None)


# ---------------------------------------------------------------- the claim


def test_consumer_claim_round_trips_and_an_unusable_value_is_refused_at_mint() -> None:
    tok = mint_token("sa:qa", "o", ["*"], ["writer"], consumer=" qa-agent ", secret=_SECRET)
    assert verify_token(tok, secret=_SECRET).consumer == "qa-agent"
    plain = mint_token("sa:x", "o", ["*"], ["writer"], secret=_SECRET)
    assert verify_token(plain, secret=_SECRET).consumer == ""
    with pytest.raises(TokenError):
        mint_token("sa:x", "o", ["*"], ["writer"], consumer="   ", secret=_SECRET)
    with pytest.raises(TokenError):
        mint_token("sa:x", "o", ["*"], ["writer"], consumer="-", secret=_SECRET)


def test_a_forged_claim_is_normalised_like_the_request_field() -> None:
    import jwt as pyjwt

    tok = mint_token("sa:qa", "o", ["*"], ["writer"], secret=_SECRET)
    decoded = pyjwt.decode(tok, _SECRET, algorithms=["HS256"], options={"verify_aud": False})
    decoded["consumer"] = "x" * 200 + "\n\x00"
    forged = pyjwt.encode(decoded, _SECRET, algorithm="HS256", headers={"kid": "override"})
    assert verify_token(forged, secret=_SECRET).consumer == "x" * cons.CONSUMER_MAX_CHARS
    decoded["consumer"] = {"nested": True}
    forged = pyjwt.encode(decoded, _SECRET, algorithm="HS256", headers={"kid": "override"})
    assert verify_token(forged, secret=_SECRET).consumer == ""


def test_resolution_is_claim_then_request_then_class_and_names_its_source() -> None:
    claimed = _auth(consumer="qa-agent")
    assert cons.resolve_consumer_and_source({"consumer": "impostor"}, claimed) == (
        "qa-agent",
        "claimed",
    )
    assert cons.resolve_consumer_and_source({}, claimed) == ("qa-agent", "claimed")
    assert cons.resolve_consumer_and_source({"consumer": "plugin"}, _auth()) == (
        "plugin",
        "self-reported",
    )
    assert cons.resolve_consumer_and_source({}, _auth()) == ("service-account", "class")
    assert cons.resolve_consumer_and_source({}, None) == ("anonymous", "class")
    # the claim outranks the request's own field
    assert cons.resolve_consumer_and_source({"consumer": "impostor"}, claimed)[0] == "qa-agent"
    assert cons.resolve_consumer_and_source({"consumer": "plugin"}, None)[0] == "plugin"
    cons.set_current_consumer(*cons.resolve_consumer_and_source({"consumer": "x"}, claimed))
    assert cons.current_consumer() == "qa-agent" and cons.current_consumer_source() == "claimed"
    cons.set_current_consumer("")
    assert cons.current_consumer_source() == ""


def test_emit_agent_event_stamps_the_source_beside_the_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.observability import events as ev

    captured: list[Any] = []
    monkeypatch.setattr(ev, "get_emitter", lambda: SimpleNamespace(emit=captured.append))
    monkeypatch.setattr(ledger, "get_emitter", lambda: SimpleNamespace(emit=captured.append))
    ident = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    common = dict(
        op_name="memory.get",
        identity=ident,
        request_id="r",
        latency_ms=3,
        success=True,
        error_code="",
    )
    # a claim on the verified token wins over the body's own tag
    set_current_auth(_auth(consumer="qa-agent"))
    cons.set_current_consumer("qa-agent", "claimed")
    ledger.emit_agent_event(raw_args={"consumer": "impostor", "memory_key": "k"}, **common)
    # the body's tag alone is self-reported
    set_current_auth(_auth())
    cons.set_current_consumer("plugin", "self-reported")
    ledger.emit_agent_event(raw_args={"consumer": "plugin", "memory_key": "k"}, **common)
    # neither: the subject class
    cons.set_current_consumer("")
    ledger.emit_agent_event(raw_args={"memory_key": "k"}, **common)
    # a caller that passes the consumer explicitly (a worker) says nothing about its source
    ledger.emit_agent_event(raw_args=None, consumer="explicit", **common)
    assert [e.consumer for e in captured] == ["qa-agent", "plugin", "service-account", "explicit"]
    assert [e.args.get("consumer_source") for e in captured] == [
        "claimed",
        "self-reported",
        "class",
        None,
    ]
    assert captured[0].args.get("consumer") == "impostor"  # the raw request value still rides args


# ---------------------------------------------------------------- token_id


def test_token_id_is_the_jti_or_the_tokens_sha256_prefix() -> None:
    # A minted token carries a jti, and that is its id.
    tok = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET)
    tid = verify_token(tok, secret=_SECRET).token_id
    assert tid == jwt.decode(tok, options={"verify_signature": False})["jti"]
    assert len(tid) == 32 and int(tid, 16) >= 0
    assert verify_token(tok, secret=_SECRET).token_id == tid  # stable per credential
    other = mint_token("sa:bot2", "acme", ["shop"], ["viewer"], secret=_SECRET)
    assert verify_token(other, secret=_SECRET).token_id != tid
    # A token from another issuer without a jti falls back to the hash prefix.
    bare = jwt.encode(
        {
            k: v
            for k, v in jwt.decode(tok, options={"verify_signature": False}).items()
            if k != "jti"
        },
        _SECRET,
        algorithm="HS256",
        headers={"kid": "override"},
    )
    btid = verify_token(bare, secret=_SECRET).token_id
    assert btid == hashlib.sha256(bare.encode()).hexdigest()[:16] and len(btid) == 16
    assert token_id_of("raw", "jti-9") == "jti-9" and token_id_of("raw", None) == token_id_of(
        "raw", ""
    )
    assert tok not in tid  # never the token itself


def test_exposure_row_carries_the_credential_id() -> None:
    from server.memory.app import exposure_ledger as led

    tok = mint_token("sa:bot", "acme", ["shop"], ["viewer"], secret=_SECRET)
    auth = verify_token(tok, secret=_SECRET)
    row = led.build_row(
        operation="memory.search", org="acme", namespace="shop", keys=["k1"], auth=auth
    )
    assert row["token_id"] == auth.token_id != ""


async def test_audit_row_carries_token_id_outside_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import audit

    inserted: list[tuple[str, list[dict[str, Any]]]] = []

    class _CH:
        async def insert(
            self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any]
        ) -> None:
            inserted.append((table, rows))

    monkeypatch.setattr(audit, "get_client", lambda: _CH())
    ts = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)
    set_current_auth(_auth(token_id="abcd1234abcd1234"))
    rh = await audit.emit(
        "memory.set", org="o", namespace="n", actor="a", subject={"k": 1}, request_id="r", ts=ts
    )
    set_current_auth(None)
    await audit.emit(
        "memory.set", org="o", namespace="n", actor="a", subject={"k": 1}, request_id="r", ts=ts
    )
    rows = [r for _, batch in inserted for r in batch]
    assert [r["token_id"] for r in rows] == ["abcd1234abcd1234", ""]
    assert rows[0]["row_hash"] == rh and audit.row_matches_hash(rows[0])
    assert rows[0]["row_hash"] == rows[1]["row_hash"]  # attribution, not payload


# ---------------------------------------------------------------- admin/consumers


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from server.insights.app.admin_routes import admin_router

    monkeypatch.setattr(settings, "auth_required", False)
    app = FastAPI()
    app.include_router(admin_router)
    return TestClient(app)


def test_attribution_labels() -> None:
    from server.insights.app.admin_routes import _attribution_label

    assert _attribution_label("qa-agent", 10, 10) == "claimed"
    assert _attribution_label("qa-agent", 10, 0) == "self-reported"
    assert _attribution_label("qa-agent", 10, 4) == "mixed"
    assert _attribution_label("service-account", 10, 0) == "class"
    assert _attribution_label("qa-agent", 0, 0) == "self-reported"


def test_admin_consumers_reports_claimed_rows_and_the_claim_bound_share_of_tagged_spend(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.insights.store import admin as admin_repo

    async def _consumers(
        since: int, limit: int, *, org: str | None = None, ch: Any = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "consumer_key": "qa-agent",
                "calls": 100,
                "errors": 1,
                "server_errors": 0,
                "error_rate": 0.01,
                "p50_ms": 400.0,
                "p95_ms": 900.0,
                "spend_usd": 3.0,
                "outcome_calls": 0,
                "namespaces": 1,
                "ops_distinct": 3,
                "claimed_calls": 100,
                "claimed_spend_usd": 3.0,
                "last_seen": None,
            },
            {
                "consumer_key": "membench",
                "calls": 50,
                "errors": 0,
                "server_errors": 0,
                "error_rate": 0.0,
                "p50_ms": 500.0,
                "p95_ms": 950.0,
                "spend_usd": 1.0,
                "outcome_calls": 0,
                "namespaces": 1,
                "ops_distinct": 2,
                "claimed_calls": 0,
                "claimed_spend_usd": 0.0,
                "last_seen": None,
            },
            {
                "consumer_key": "service-account",
                "calls": 10,
                "errors": 0,
                "server_errors": 0,
                "error_rate": 0.0,
                "p50_ms": 90.0,
                "p95_ms": 120.0,
                "spend_usd": 5.0,
                "outcome_calls": 0,
                "namespaces": 2,
                "ops_distinct": 2,
                "last_seen": None,
            },
        ]

    async def _outcomes(
        since: int, limit: int, *, org: str | None = None, ch: Any = None
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(admin_repo, "consumers", _consumers)
    monkeypatch.setattr(admin_repo, "consumer_outcomes", _outcomes)
    r = client.post("/v1/admin/consumers", json={"windows_seconds": [3600]})
    assert r.status_code == 200, r.text
    win = r.json()["windows"][0]
    rows = {row["consumer"]: row for row in win["rows"]}
    assert rows["qa-agent"]["attribution"] == "claimed" and rows["qa-agent"]["claimed_calls"] == 100
    assert rows["membench"]["attribution"] == "self-reported"
    assert rows["service-account"]["attribution"] == "class"
    # tagged spend excludes the subject-class buckets; the claim-bound share is what a claim bound
    assert win["tagged_spend_usd"] == 4.0 and win["claim_bound_spend_usd"] == 3.0
    assert win["claim_bound_share"] == 0.75


def test_consumers_aggregate_counts_claimed_rows_from_the_args_source() -> None:
    import asyncio

    from server.insights.store import admin as admin_repo

    class _CH:
        def __init__(self) -> None:
            self.sql = ""

        async def query_one(self, sql, params=None):
            return {"name": "consumer"}

        async def query_all(self, sql, params=None):
            self.sql = " ".join(sql.split())
            return []

    ch = _CH()
    asyncio.run(admin_repo.consumers(3600, 10, ch=ch))  # type: ignore[arg-type]
    assert "countIf(args['consumer_source'] = 'claimed') AS claimed_calls" in ch.sql
    assert "sumIf(upstream_cost_usd, args['consumer_source'] = 'claimed')" in ch.sql


# ---------------------------------------------------------------- CLI


def test_cli_mint_token_takes_a_consumer(monkeypatch, capsys, settings_override) -> None:
    import asyncio

    from server import cli
    from server.cli import access

    class _NoKeys:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(
        access, "run_connected", lambda run, **_declared: asyncio.run(run(_NoKeys()))
    )
    secret = settings.jwt_secret  # an empty keyring signs with the root secret
    rc = cli.main(
        [
            "mint-token",
            "--subject",
            "sa:h",
            "--org",
            "o",
            "--namespaces",
            "n",
            "--consumer",
            "qa-agent",
            "--json",
        ]
    )
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["consumer"] == "qa-agent"
    assert verify_token(summary["token"], secret=secret).consumer == "qa-agent"
