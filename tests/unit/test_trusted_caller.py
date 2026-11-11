"""LOOM_TRUSTED_CALLERS + X-Loom-On-Behalf-Of impersonation.

The mechanism: when an authenticated request's JWT subject is on the
operator-curated LOOM_TRUSTED_CALLERS allowlist AND the request carries
an `X-Loom-On-Behalf-Of` header, the HTTP transport substitutes the
effective subject before handler dispatch. Grants resolution + memory
writes downstream see the impersonated identity; the audit trail
records the actual caller as `impersonated_by`.

These tests lock in the contract without needing a live server.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo

import server.auth.bearer as bearer_mod
from server.core.errors import BackendTimeoutError, BackendUnavailableError, LoomServerError
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


class _FakeRequest:
    """Minimal duck-type for the bits of Request _extract_auth touches."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _auth(subject: str = "sa:gateway"):
    from server.auth.jwt import AuthInfo, Role

    return AuthInfo(
        subject=subject,
        org="clickhouse",
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )


def test_extract_auth_honors_obo_header_for_trusted_caller() -> None:
    """sa:gateway is on LOOM_TRUSTED_CALLERS → header replaces subject."""
    from server.transport import http as http_mod

    req = _FakeRequest(
        {
            "Authorization": "Bearer fake-token",
            "X-Loom-On-Behalf-Of": "bob@example.com",
        }
    )

    with (
        patch.object(bearer_mod, "verify_token", return_value=_auth("sa:gateway")),
        patch.object(http_mod.settings, "trusted_callers", "sa:gateway,sa:other"),
    ):
        out = http_mod._extract_auth(req)  # type: ignore[arg-type]

    assert out is not None
    assert out.subject == "bob@example.com"
    assert out.impersonated_by == "sa:gateway"


def test_extract_auth_ignores_obo_when_caller_not_trusted() -> None:
    """sa:imposter not in LOOM_TRUSTED_CALLERS → header is ignored."""
    from server.transport import http as http_mod

    req = _FakeRequest(
        {
            "Authorization": "Bearer fake-token",
            "X-Loom-On-Behalf-Of": "bob@example.com",
        }
    )

    with (
        patch.object(bearer_mod, "verify_token", return_value=_auth("sa:imposter")),
        patch.object(http_mod.settings, "trusted_callers", "sa:gateway"),
    ):
        out = http_mod._extract_auth(req)  # type: ignore[arg-type]

    assert out is not None
    assert out.subject == "sa:imposter"  # unchanged
    assert out.impersonated_by == ""


def test_extract_auth_ignores_obo_when_no_trusted_callers_configured() -> None:
    """Empty LOOM_TRUSTED_CALLERS → impersonation feature is off entirely."""
    from server.transport import http as http_mod

    req = _FakeRequest(
        {
            "Authorization": "Bearer fake-token",
            "X-Loom-On-Behalf-Of": "bob@example.com",
        }
    )

    with (
        patch.object(bearer_mod, "verify_token", return_value=_auth("sa:gateway")),
        patch.object(http_mod.settings, "trusted_callers", ""),
    ):
        out = http_mod._extract_auth(req)  # type: ignore[arg-type]

    assert out is not None
    assert out.subject == "sa:gateway"
    assert out.impersonated_by == ""


def test_extract_auth_trusted_caller_without_obo_header_is_noop() -> None:
    """Trusted caller making a normal request (no impersonation) — works as
    today; subject stays the JWT's actual sub."""
    from server.transport import http as http_mod

    req = _FakeRequest({"Authorization": "Bearer fake-token"})

    with (
        patch.object(bearer_mod, "verify_token", return_value=_auth("sa:gateway")),
        patch.object(http_mod.settings, "trusted_callers", "sa:gateway"),
    ):
        out = http_mod._extract_auth(req)  # type: ignore[arg-type]

    assert out is not None
    assert out.subject == "sa:gateway"
    assert out.impersonated_by == ""


def test_agent_event_carries_impersonated_by() -> None:
    """The audit row serializes the field for the agent_events column."""
    from datetime import UTC, datetime

    from server.observability.events import AgentEvent

    e = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="bob@example.com",
        session_id="-",
        operation="memory.set",
        request_id="r",
        latency_ms=10,
        success=True,
        impersonated_by="sa:gateway",
    )
    row = e.to_row()
    assert row["impersonated_by"] == "sa:gateway"
    assert row["user_id"] == "bob@example.com"


# ── the subject a trusted caller acts for ─────────────────────────────────


def _acting(subject: str, org: str = "clickhouse") -> AuthInfo:
    from server.auth.jwt import AuthInfo, Role

    return AuthInfo(
        subject=subject,
        org=org,
        namespaces=("*",),
        roles=(Role.WRITER,),
        is_service_account=True,
        impersonated_by="sa:gateway",
    )


@pytest.mark.parametrize(
    ("revoked", "header", "org"),
    [
        ("bob", "bob", "clickhouse"),
        ("\x00scim:bob", "bob", "clickhouse"),  # deprovisioned through SCIM
        ("bob", "Bob", "clickhouse"),  # grants are read case-folded
        ("Bob", "bob", "clickhouse"),  # a revocation written in another case
        ("bob", "bob@corp.com", "clickhouse"),  # the verified email tied to the handle
        ("bob", "bob@corp.com", "*"),  # an org-wide caller: spellings of the target org
    ],
)
def test_a_revoked_subject_is_not_acted_for(
    monkeypatch: pytest.MonkeyPatch, revoked: str, header: str, org: str
) -> None:
    """The gate refuses a request a trusted caller makes for a subject revoked or
    deprovisioned under any spelling its grants in the target org answer to."""
    from server.auth import authz, revocation
    from server.core import grant_cache

    async def grant_map(org_: str, subject: str) -> grant_cache.GrantMap:
        tied = {"bob@corp.com": {"bob@corp.com", "bob"}}.get(subject, {subject.lower()})
        return grant_cache.GrantMap(ranks={}, held=tuple(tied))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset({revoked.lower()}))
    with pytest.raises(PermissionError, match="acts for is revoked"):
        asyncio.run(authz.check_acted_for(_acting(header, org), "acme"))


def test_an_unreadable_grant_map_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import authz
    from server.core import grant_cache
    from server.core.errors import BackendUnavailableError

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        raise BackendUnavailableError("grants unreadable")

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    with pytest.raises(BackendUnavailableError):
        asyncio.run(authz.check_acted_for(_acting("bob"), "acme"))


def test_a_subject_not_revoked_is_acted_for(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import authz, revocation
    from server.core import grant_cache

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject.lower(),))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset({"carol"}))
    asyncio.run(authz.check_acted_for(_acting("bob"), "acme"))  # admitted: no raise


def test_the_gate_refuses_before_provisioning_for_the_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request acting for a revoked subject is refused before the gate provisions
    a personal namespace for that subject."""
    from server.auth import personal_namespace, revocation
    from server.core import grant_cache
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import gates

    provisioned: list[str] = []

    async def ensure(**k: object) -> None:
        provisioned.append(str(k["subject"]))

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject.lower(),))

    monkeypatch.setattr(personal_namespace, "ensure_personal_namespace_cached", ensure)
    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset({"bob"}))
    target = gates.Target(org="acme", namespace="ns", named_namespace="ns", register_namespace="ns")
    with pytest.raises(PermissionError, match="acts for is revoked"):
        asyncio.run(
            gates.authorize(
                OPERATIONS_BY_NAME["memory.get"], _acting("Bob"), target, transport="http"
            )
        )
    assert provisioned == []


def test_a_revocation_written_in_another_case_refuses_through_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lower-cased set the gate reads is rebuilt from what a revocation writes:
    `Bob` revoked here refuses acting for `bob`, with nothing patched between."""
    from server.auth import authz, revocation
    from server.core import grant_cache

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject.lower(),))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    revocation._apply_locally("Bob", None)
    with pytest.raises(PermissionError, match="acts for is revoked"):
        asyncio.run(authz.check_acted_for(_acting("bob"), "acme"))


def test_a_cutoff_revocation_does_not_refuse_acting_for_the_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cutoff ends tokens issued before it; an impersonated request carries none
    of the subject's, so the subject is still acted for."""
    from server.auth import authz, revocation
    from server.core import grant_cache

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject,))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    revocation._apply_locally("bob", 2_000_000_000)
    asyncio.run(authz.check_acted_for(_acting("bob"), "acme"))


def test_the_tokens_own_org_is_checked_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A personal namespace is provisioned in the token's org: a spelling tied to a
    revoked subject there refuses, though the target org ties nothing."""
    from server.auth import authz, revocation
    from server.core import grant_cache

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        tied = {"bob@corp.com", "bob"} if org == "clickhouse" else {subject}
        return grant_cache.GrantMap(ranks={}, held=tuple(tied))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset({"bob"}))
    # its own refusal, so the HTTP 403 adds no code-index reason to it
    with pytest.raises(authz.ActedForRevoked, match="acts for is revoked"):
        asyncio.run(authz.check_acted_for(_acting("bob@corp.com", "clickhouse"), "acme"))


def test_an_end_user_token_is_not_checked_as_acted_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """An end user's issuer is recorded in `impersonated_by`, but the token names the
    user itself and was checked at verify: a revoked spelling of it does not refuse it
    as acted for, while the same revocation refuses a subject a trusted caller acts for."""
    from server.auth import authz, revocation
    from server.core import grant_cache

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject,))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset({"user:u-1", "bob"}))

    asyncio.run(authz.check_acted_for(_acting("user:u-1", "acme"), "acme"))  # admitted
    with pytest.raises(PermissionError, match="acts for is revoked"):
        asyncio.run(authz.check_acted_for(_acting("bob", "acme"), "acme"))


@pytest.mark.parametrize(
    ("outright", "cutoff_on", "refused"),
    [
        ({"Bob"}, "Bob", False),  # the outright revocation became a cutoff: bob is free
        ({"Bob", "BOB"}, "Bob", True),  # another spelling stays revoked outright
    ],
)
def test_a_cutoff_on_a_revoked_key_refolds_the_snapshot(
    monkeypatch: pytest.MonkeyPatch, outright: set[str], cutoff_on: str, refused: bool
) -> None:
    from server.auth import revocation

    monkeypatch.setattr(revocation, "_revoked_jtis", frozenset(outright))
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset(k.lower() for k in outright))
    monkeypatch.setattr(revocation, "_revoked_before", {})
    monkeypatch.setattr(revocation, "_recent", [])
    revocation._apply_locally(cutoff_on, 2_000_000_000)
    assert revocation.revoked_outright_any_case("bob") is refused


@pytest.fixture
def _scim(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """SCIM's deactivated emails; the per-process memo starts empty."""
    from server.auth import authz, scim

    gone: set[str] = set()

    async def deactivated(email: str) -> bool:
        return email in gone

    monkeypatch.setattr(scim, "deactivated", deactivated)
    monkeypatch.setattr(authz, "_ENDED", {})
    return gone


def test_an_email_scim_deactivated_is_not_acted_for_where_it_has_no_account(
    monkeypatch: pytest.MonkeyPatch, _scim: set[str]
) -> None:
    """An email granted in an org where no account of it is has no revocation of its
    own; SCIM's deactivation of it refuses acting for it there."""
    from server.auth import authz
    from server.core import grant_cache

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject.lower(),))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    asyncio.run(authz.check_acted_for(_acting("alice@corp.com", "*"), "acme"))  # admitted
    _scim.add("alice@corp.com")
    monkeypatch.setattr(authz, "_ENDED", {})
    with pytest.raises(PermissionError, match="acts for is revoked"):
        asyncio.run(authz.check_acted_for(_acting("Alice@corp.com", "*"), "acme"))


@pytest.mark.parametrize(
    ("cause", "kept"),
    [(BackendTimeoutError("scim users timed out"), True), (RuntimeError("Code: 60"), False)],
    ids=["loom-error-kept", "server-error-wrapped"],
)
def test_an_unread_scim_deactivation_refuses(
    monkeypatch: pytest.MonkeyPatch, cause: Exception, kept: bool
) -> None:
    from server.auth import authz, login, scim
    from server.core import grant_cache

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject,))

    async def unreadable(email: str) -> bool:
        raise login.SignInUnavailable from cause

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(scim, "deactivated", unreadable)
    monkeypatch.setattr(authz, "_ENDED", {})
    with pytest.raises(LoomServerError) as refused:
        asyncio.run(authz.check_acted_for(_acting("alice@corp.com"), "acme"))
    if kept:
        assert refused.value is cause
    else:
        assert type(refused.value) is BackendUnavailableError


def test_a_refreshed_snapshot_folds_a_revocation_written_in_another_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer replica's `Bob` revocation, read by this one's refresh, refuses `bob`."""
    from server.auth import revocation

    class _CH:
        async def query_all(self, sql: str, **k: object) -> list[dict[str, object]]:
            return [{"jti": "Bob", "issued_before": None, "version": 1}]

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    asyncio.run(revocation.refresh())
    assert revocation.revoked_outright_any_case("bob")
    assert not revocation.revoked_outright_any_case("carol")


# ── the routes outside the op gate, and the gate end to end ─────────────────


@pytest.fixture
def _bob_revoked(monkeypatch: pytest.MonkeyPatch) -> object:
    """A trusted caller's token, `bob` revoked outright, and the app's client."""
    from fastapi.testclient import TestClient

    from server.auth import revocation
    from server.core import grant_cache
    from server.main import app
    from server.transport import http as http_mod

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject.lower(),))

    monkeypatch.setattr(bearer_mod, "verify_token", lambda *a, **k: _auth("sa:gateway"))
    monkeypatch.setattr(http_mod.settings, "trusted_callers", "sa:gateway")
    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset({"bob"}))
    return TestClient(app)


_AS_BOB = {"Authorization": "Bearer fake-token", "X-Loom-On-Behalf-Of": "bob"}


def test_knowledge_upload_refuses_a_revoked_acted_for_subject(_bob_revoked: Any) -> None:
    r = _bob_revoked.post(
        "/v1/knowledge.upload",
        params={"org": "clickhouse", "namespace": "ns", "filename": "a.txt"},
        content=b"hello",
        headers=_AS_BOB,
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
    assert "acts for is revoked" in r.json()["detail"]["message"]


def test_knowledge_download_refuses_a_revoked_acted_for_subject(_bob_revoked: Any) -> None:
    r = _bob_revoked.get(
        "/v1/knowledge.download",
        params={"org": "clickhouse", "namespace": "ns", "source_uri": "a.txt"},
        headers=_AS_BOB,
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
    assert "acts for is revoked" in r.json()["detail"]["message"]


def test_an_op_refuses_a_revoked_acted_for_subject_with_no_index_reason(
    monkeypatch: pytest.MonkeyPatch, _bob_revoked: Any
) -> None:
    """POST /v1/{op} with X-Loom-On-Behalf-Of: a code op the index would refuse with a
    reason answers the revoked subject's 403 alone."""
    from server.transport import access_refusal as ar

    monkeypatch.setattr(ar, "reason_fields", lambda ns: {"reason": "NO_REPO_ACCESS"})
    r = _bob_revoked.post(
        "/v1/code.search",
        json={"org": "clickhouse", "namespace": "code-x", "query": "q"},
        headers=_AS_BOB,
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == {
        "code": "AUTH_INSUFFICIENT",
        "message": "the subject this request acts for is revoked, deprovisioned or disabled",
        "category": "auth",
        "retryable": False,
    }


@pytest.mark.claim_cap
@pytest.mark.parametrize(("verified", "refused"), [(False, False), (True, True)])
def test_the_acted_for_check_reads_the_held_spellings_not_every_tied_one(
    monkeypatch: pytest.MonkeyPatch, verified: bool, refused: bool
) -> None:
    """A revoked `mallory` in the grant read's `spellings` but not in its `held` (what
    `_read` answers for an unverified email; tests/unit/test_aliases_verified_email_sql.py
    runs it) does not refuse acting for carol@corp.com; in `held` too, it does."""
    from server.auth import authz, revocation
    from server.core import grant_cache

    class _CH:
        async def query_all(self, sql: str, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return []

        async def query_one(self, sql: str, params: dict[str, Any], **k: Any) -> dict[str, Any]:
            tied = [params["sub"], "mallory", "carol@corp.com"]
            return {"grants": [], "spellings": tied, "held": tied if verified else tied[:1]}

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset({"mallory"}))
    grant_cache.clear()
    acting = authz.check_acted_for(_acting("carol@corp.com", "acme"), "acme")
    if refused:
        with pytest.raises(authz.ActedForRevoked):
            asyncio.run(acting)
    else:
        asyncio.run(acting)


@pytest.mark.parametrize(
    ("subject", "disabled", "refused"),
    [
        ("sa:ci", {"ci"}, True),  # the bot itself
        ("sa:ci:agent-1", {"ci"}, True),  # an agent of it, which keeps its grants
        ("SA:ci:agent-1", {"ci"}, True),  # grants are read case-folded, and so is this
        ("sa:CI:agent-1", {"ci"}, True),
        ("Sa:Ci", {"ci"}, True),
        ("sa:ci:agent-1", set(), False),  # its bot is active
        ("sa:ci", set(), False),
        # A token's own subject, which a revoke by jti or a cutoff ends without its row
        # or its grants saying so: never acted for, live or dead.
        ("sa:minted", set(), True),  # `loom mint-token --subject sa:minted`: no bot row
        ("sa:token-2026-01-02-0a1b2c3d4e5f-9f8e7d", set(), True),  # a dashboard token
        ("sa:helper-1a2b3c", set(), True),  # an agent sign-up token
        ("sa:", set(), True),
        ("sa:ghost:agent-1", set(), True),  # an agent of no bot
    ],
)
def test_a_trusted_caller_acts_only_for_an_active_bot_or_its_agents(
    monkeypatch: pytest.MonkeyPatch, subject: str, disabled: set[str], refused: bool
) -> None:
    """Of the `sa:` subjects, a trusted caller acts for an active bot and its agents only.
    A disable ends the bot's tokens with a cutoff, which does not refuse acting for a
    subject, and leaves its agents' grants: the bot's own state refuses. Any other `sa:`
    subject is a token's own, whose grants must end with the token however it ends."""
    from server.auth import authz, machines
    from server.core import grant_cache

    async def grant_map(org: str, subject_: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={"ci-agent-1": 3}, held=(subject_,))

    async def is_active(name: str, **_: Any) -> bool:
        return name == "ci" and name not in disabled

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(machines, "active", is_active)
    acting = authz.check_acted_for(_acting(subject, "acme"), "acme")
    if refused:
        with pytest.raises(authz.ActedForRevoked):
            asyncio.run(acting)
    else:
        asyncio.run(acting)


@pytest.mark.parametrize(
    ("error", "kept"),
    [
        (BackendTimeoutError("machine identities timed out"), True),
        (RuntimeError("Code: 60"), False),
    ],
    ids=["loom-error-kept", "server-error-wrapped"],
)
def test_an_unreadable_bot_refuses(
    monkeypatch: pytest.MonkeyPatch, error: Exception, kept: bool
) -> None:
    from server.auth import authz, machines
    from server.core import grant_cache

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject,))

    async def unreadable(name: str, **_: Any) -> bool:
        raise error

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(machines, "active", unreadable)
    with pytest.raises(LoomServerError) as refused:
        asyncio.run(authz.check_acted_for(_acting("sa:ci", "acme"), "acme"))
    if kept:
        assert refused.value is error  # its own code and retry hint
    else:
        assert type(refused.value) is BackendUnavailableError


@pytest.mark.parametrize(
    ("rows", "active"),
    [([{"active": 0}], False), ([{"active": 1}], True), ([], False)],
)
def test_a_bot_is_active_only_when_its_row_says_so(
    rows: list[dict[str, Any]], active: bool
) -> None:
    from server.auth import machines

    class _CH:
        async def query_one(self, sql: str, params: dict[str, Any], **_: Any) -> Any:
            assert params == {"name": "ci"}
            return rows[0] if rows else None

    assert asyncio.run(machines.active("ci", ch=_CH())) is active


def test_a_remembered_answer_is_reused_until_the_grant_cache_ttl_and_read_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import authz, machines
    from server.core import grant_cache
    from server.settings import settings
    from tests.unit.clock_fake import ManualClock

    clock = ManualClock(t=100.0)
    reads: list[str] = []
    disabled: set[str] = set()

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject,))

    async def is_active(name: str, **_: Any) -> bool:
        reads.append(name)
        return name not in disabled

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(machines, "active", is_active)
    monkeypatch.setattr(time, "monotonic", clock.monotonic)  # the process clock
    monkeypatch.setattr(authz, "_ENDED", {})
    monkeypatch.setattr(settings, "grant_cache_ttl_seconds", 10.0)
    asyncio.run(authz.check_acted_for(_acting("sa:ci", "acme"), "acme"))
    disabled.add("ci")  # written by another process
    clock.advance(9.9)
    asyncio.run(authz.check_acted_for(_acting("sa:ci", "acme"), "acme"))  # the memo answers
    assert reads == ["ci"]
    clock.advance(0.1)
    with pytest.raises(authz.ActedForRevoked):
        asyncio.run(authz.check_acted_for(_acting("sa:ci", "acme"), "acme"))
    assert reads == ["ci", "ci"]


def test_the_memo_is_emptied_past_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.auth import authz

    async def ended(key: str) -> bool:
        return False

    monkeypatch.setattr(authz, "_ENDED", {})
    monkeypatch.setattr(authz, "_ENDED_MAX", 2)
    for name in ("a", "b", "c"):
        asyncio.run(authz._remembered(("bot", name), ended))
    assert list(authz._ENDED) == [("bot", "c")]


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_a_bot_disabled_on_this_process_is_refused_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The disabling process drops its memo of the bot, and `machines.active` reads the
    real table."""
    from server.auth import authz, machines
    from server.core import grant_cache

    class _Kept(KeptCH):
        async def insert(
            self, table: str, rows: list[dict[str, Any]], settings: Any = None
        ) -> None:
            # KeptCH's JSONEachRow insert cannot fill the Map column, so the row's
            # keys and state go in as values.
            for r in rows:
                await self.execute(
                    f"INSERT INTO {table} (name, org, active, version) VALUES"
                    " ({n:String}, {o:String}, {a:UInt8}, {v:UInt64})",
                    {"n": r["name"], "o": r["org"], "a": r["active"], "v": r["version"]},
                )

    kept = _Kept(tmp_path, ddl("machine_identities"))

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject,))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(machines, "get_client", lambda: kept)
    monkeypatch.setattr(authz, "_ENDED", {})
    bot: machines.Machine = {
        "name": "ci",
        "org": "acme",
        "ns_roles": [("ci", "writer")],
        "public_keys": "",
        "issuer": "",
        "audience": "",
        "claims": {},
        "agent_claim": "",
        "jwks_uri": "",
        "max_token_s": 3600,
        "max_agents": 1000,
        "max_access_token_s": 86400,
        "platform_admin": False,
    }
    asyncio.run(machines.save(bot, by="cli:root"))
    asyncio.run(authz.check_acted_for(_acting("sa:ci:agent-1", "acme"), "acme"))  # remembered
    asyncio.run(machines.save(bot, by="cli:root", active=False))
    with pytest.raises(authz.ActedForRevoked):
        asyncio.run(authz.check_acted_for(_acting("sa:ci:agent-1", "acme"), "acme"))


def test_a_scim_deactivation_on_this_process_refuses_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next acted-for check reads the deactivation afresh, not the remembered answer."""
    from server.auth import authz, scim
    from server.core import grant_cache

    gone: set[str] = set()

    async def nothing(*_: Any) -> None:
        return None

    async def deactivated(email: str) -> bool:
        return email in gone

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject.lower(),))

    async def write(new: dict[str, Any]) -> None:
        gone.add(new["email"].lower())

    monkeypatch.setattr(scim, "_write", write)
    monkeypatch.setattr(scim, "_audit_transition", nothing)
    monkeypatch.setattr(scim, "deactivated", deactivated)
    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(authz, "_ENDED", {("scim", "alice@corp.com"): (time.monotonic(), False)})
    row = {"id": "u1", "email": "Alice@corp.com", "active": 1}
    asyncio.run(scim._write_audited(row, {**row, "active": 0}))
    with pytest.raises(authz.ActedForRevoked):
        asyncio.run(authz.check_acted_for(_acting("alice@corp.com", "acme"), "acme"))


def test_a_read_that_started_before_a_forget_is_not_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disable written while a read of the bot is out: the read's older answer is
    returned to its caller but not kept, so the next check reads afresh."""
    from server.auth import authz

    async def read_across_a_disable(name: str) -> bool:
        authz.forget_ended("bot", name)  # the disable lands while this read is out
        return False

    monkeypatch.setattr(authz, "_ENDED", {})
    assert asyncio.run(authz._remembered(("bot", "ci"), read_across_a_disable)) is False
    assert ("bot", "ci") not in authz._ENDED


def test_a_bot_save_that_raised_still_refuses_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """An insert whose reply was lost may have landed, so the next check reads afresh."""
    from server.auth import authz, machines
    from server.core import grant_cache

    class _Lost:
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
            return {"v": 1}  # next_version's read

        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            raise BackendTimeoutError("machine identities timed out")

    async def landed(name: str, **_: Any) -> bool:
        return False  # the disable landed

    async def grant_map(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject.lower(),))

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(authz, "_ENDED", {("bot", "ci"): (time.monotonic(), False)})
    with pytest.raises(BackendTimeoutError):
        asyncio.run(machines.save({"name": "ci"}, by="cli:root", active=False, ch=_Lost()))  # type: ignore[typeddict-item]
    monkeypatch.setattr(machines, "active", landed)
    with pytest.raises(authz.ActedForRevoked):
        asyncio.run(authz.check_acted_for(_acting("sa:ci", "acme"), "acme"))


@pytest.mark.parametrize("subject", ["sa:ci:agent-1", "SA:CI:agent-1", "sa:ci", "sa:ci:"])
def test_a_bot_revoked_outright_refuses_acting_for_its_agents(
    monkeypatch: pytest.MonkeyPatch, subject: str
) -> None:
    """An agent's token needs its bot unrevoked (`oauth`'s `subjects_revoked([trust,
    subject])`); acting for the agent does too, with the bot's row still active."""
    from server.auth import authz, machines, revocation
    from server.core import grant_cache

    async def grant_map(org: str, subject_: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={}, held=(subject_.lower(),))

    async def still_active(name: str, **_: Any) -> bool:
        return True

    monkeypatch.setattr(grant_cache, "grant_map", grant_map)
    monkeypatch.setattr(machines, "active", still_active)
    monkeypatch.setattr(authz, "_ENDED", {})
    monkeypatch.setattr(revocation, "_revoked_folded", frozenset({"sa:ci"}))
    with pytest.raises(authz.ActedForRevoked):
        asyncio.run(authz.check_acted_for(_acting(subject, "acme"), "acme"))
