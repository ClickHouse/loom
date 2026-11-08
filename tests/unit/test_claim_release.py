"""`claim_release` retires a claim: grants revoked, the credential cleared, the row kept (and the name reserved) with every other column carried forward.

Only an org/platform admin of the namespace's org may release it; a
restricted namespace needs the caller's own direct admin grant and is
otherwise answered like a missing name; the directory row keeps every column
but the claim. Hermetic: ClickHouse and the classification are faked.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import revocation
from server.auth.jwt import mint_token
from server.identity.app import account_admin
from server.identity.app import claim_release as cr
from server.identity.app import namespace_classification as nc
from server.memory.domain.trust import actor_for
from server.settings import settings

_REAL_IS_RESTRICTED = nc.is_restricted

_ROW: dict[str, Any] = {
    "org": "acme",
    "namespace": "repo-x",
    "owner_email": "alice",
    "contact_email": "alice@acme.io",
    "password_hash": "$argon2id$…",
    "kind": "code",
    "classification": "standard",
    "join_policy": "approval",
    "retention_days": 90,
    "active": True,
    "created_at": datetime(2026, 1, 1),
    "updated_at": datetime(2026, 1, 2),
}


class _CH:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.inserts: dict[str, list[dict[str, Any]]] = {}

    async def query_one(self, sql: str, params: Any = None) -> Any:
        if "max(version)" in sql:  # `next_version`'s read for the sso_links row
            return {"v": time.time_ns()}
        return dict(self.row) if self.row else None

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "sso_links" in sql:
            return [{"email": "alice@example.com"}]  # linked for SSO before the release
        if "machine_identities" in sql:
            return []  # no bot names the namespace
        return [{"subject": "alice", "role": "admin", "granted_by": "alice"}]

    async def insert(self, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
        self.inserts.setdefault(table, []).extend(rows)


class _Req:
    def __init__(self, token: str = "") -> None:
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "identity_provider", "gateway")
    monkeypatch.setattr(revocation, "_loaded", True)
    state: dict[str, Any] = {"restricted": False, "allows": False}

    async def is_restricted(org: str, ns: str) -> bool:
        return bool(state["restricted"])

    async def allows(*a: Any) -> bool:
        return bool(state["allows"])

    monkeypatch.setattr(nc, "is_restricted", is_restricted)
    monkeypatch.setattr("server.auth.authz.restricted_allows", allows)
    waited: list[datetime] = []

    async def wait(cutoff: datetime) -> None:
        waited.append(cutoff)

    monkeypatch.setattr(revocation, "wait_out", wait)
    state["waited"] = waited

    def run(
        token: str, row: dict[str, Any] | None = _ROW, org: str = "acme", namespace: str = "repo-x"
    ) -> tuple[Any, _CH]:
        ch = _CH(row)
        monkeypatch.setattr(cr, "get_client", lambda: ch)
        inp = cr.ClaimReleaseInput(org=org, namespace=namespace)
        return asyncio.run(cr.claim_release(inp, _Req(token))), ch  # type: ignore[arg-type]

    return run, state


def _tok(roles: list[str], org: str = "acme") -> str:
    return mint_token("op", org, ["*"], roles)


def _status(run: Any, *a: Any, **k: Any) -> int:
    with pytest.raises(HTTPException) as ei:
        run(*a, **k)
    return ei.value.status_code


def test_the_release_keeps_every_column_but_the_claim(env: Any) -> None:
    run, _ = env
    out, ch = run(_tok(["org_admin"]))
    assert out.released and out.previous_owner == "alice" and out.grants_revoked == 1
    # Its SSO link goes too: a next holder of the name is not who linked it.
    (link,) = ch.inserts["loom.sso_links"]
    assert (link["email"], link["linked"]) == ("alice@example.com", 0)
    (row,) = ch.inserts["loom.namespaces"]
    assert {k: row[k] for k in ("owner_email", "contact_email", "password_hash", "active")} == {
        "owner_email": "",
        "contact_email": "",
        "password_hash": "",
        "active": False,
    }
    for kept in ("kind", "classification", "join_policy", "retention_days", "created_at"):
        assert row[kept] == _ROW[kept], kept
    assert ch.inserts["loom.namespace_grants"][0]["revoked_at"] is not None


@pytest.mark.parametrize(
    ("token", "status"),
    [("", 401), ("not-a-jwt", 401), (None, 403), ("admin", 403)],
    ids=["no-bearer", "garbage", "writer", "namespace-admin"],
)
def test_only_an_org_admin_may_release(env: Any, token: str | None, status: int) -> None:
    run, _ = env
    tok = {None: _tok(["writer"]), "admin": _tok(["admin"])}.get(token, token)
    assert _status(run, tok) == status


def test_an_org_admin_of_another_org_is_refused(env: Any) -> None:
    run, _ = env
    assert _status(run, _tok(["org_admin"], org="other")) == 403


def test_a_restricted_namespace_is_answered_like_a_missing_one(env: Any) -> None:
    run, state = env
    state["restricted"] = True
    assert _status(run, _tok(["platform_admin"])) == 404
    assert _status(run, _tok(["platform_admin"]), row=None) == 404
    state["allows"] = True  # a direct admin grant + a claim naming it
    assert run(_tok(["org_admin"]))[0].released


def test_a_release_of_a_restricted_namespace_writes_the_access_audit_row(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, state = env
    rows: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, **kw: Any) -> str:
        rows.append((event_type, kw))
        return "hash"

    monkeypatch.setattr("server.observability.audit.emit", emit)
    monkeypatch.setattr(settings, "restricted_audit_inline", True)

    class _Directory:  # the classification read, which fills the snapshot the row reads
        _client = object()

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, str]]:
            return [{"namespace": "repo-x"}]

    monkeypatch.setattr(nc, "is_restricted", _REAL_IS_RESTRICTED)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Directory())
    monkeypatch.setattr(nc, "_cache", {})  # cold, and restored after the test
    assert _status(run, _tok(["org_admin"])) == 404
    assert rows == []  # refused: nothing was reached
    state["allows"] = True
    assert run(_tok(["org_admin"]))[0].released
    ((event, kw),) = rows
    assert event == "namespace.restricted_access"
    assert (kw["org"], kw["namespace"]) == ("acme", "repo-x")
    assert kw["subject"]["op"] == "admin.claim_release"
    assert (kw["subject"]["transport"], kw["subject"]["roles"]) == ("http", ["org_admin"])
    assert kw["actor"] == actor_for("op", is_service_account=True)  # `_tok` mints a service account


def test_an_unreadable_classification_is_answered_like_a_missing_namespace(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth.directory import ClassificationUnavailable

    async def unreadable(org: str, ns: str) -> bool:
        raise ClassificationUnavailable(org)

    monkeypatch.setattr(nc, "is_restricted", unreadable)
    run, _ = env
    assert _status(run, _tok(["org_admin"])) == 404


def test_a_collection_is_answered_like_a_missing_namespace(env: Any) -> None:
    """No org role reaches a collection, and its name may carry an end user's id."""
    run, _ = env
    assert _status(run, _tok(["org_admin"]), namespace="col-u-alice") == 404


def test_a_row_restricted_after_the_snapshot_is_answered_like_a_missing_one(env: Any) -> None:
    run, _ = env
    fresh = dict(_ROW, classification="restricted")
    assert _status(run, _tok(["org_admin"]), row=fresh) == 404


def test_auth_off_releases_as_local(env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    run, _ = env
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    _, ch = run("")
    assert ch.inserts["loom.namespaces"][0]["description"].startswith("Released by local")


def test_a_release_names_the_agent_owning_the_namespace() -> None:
    row = {"tags": ["machine:fleet"], "owner_email": "sa:fleet:a1"}
    assert cr._agent_owner(row) == "sa:fleet:a1"
    forged = {"tags": ["machine:fleet"], "owner_email": "lee"}  # a person's, marked by hand
    # Never the person's own sessions.
    assert cr._agent_owner(forged) == ""


def test_the_cutoff_is_waited_out_after_the_claim_is_let_go(env: Any, monkeypatch) -> None:
    # Held through the wait, the claim would make a password set on the
    # namespace wait and fail busy, and eat the release's own claim budget.
    from contextlib import asynccontextmanager

    from server.auth import grant_claims, machines

    run, _ = env
    events: list[str] = []

    @asynccontextmanager
    async def claim(org: str, ns: str) -> Any:
        events.append("claimed")
        yield
        events.append("let go")

    async def naming(org: str, ns: str, *, ch: Any) -> list[str]:
        return ["triage"]

    async def forget(org: str, ns: str, by: str, *, ch: Any) -> list[str]:
        return ["triage"]

    async def end_tokens(subject: str, **k: Any) -> datetime:
        return datetime(2026, 1, 3)

    async def wait(cutoff: datetime) -> None:
        events.append("waited")

    monkeypatch.setattr(grant_claims, "account_change", claim)
    monkeypatch.setattr(machines, "naming", naming)
    monkeypatch.setattr(machines, "forget_namespace", forget)
    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    monkeypatch.setattr(revocation, "wait_out", wait)
    assert run(_tok(["org_admin"]))[0].released
    assert events == ["claimed", "let go", "waited"]


_AGENT_ROW = {**_ROW, "tags": ["machine:fleet"], "owner_email": "sa:fleet:a1"}


def _release(ch: _CH) -> Any:
    return cr.release_claim(org="acme", namespace="repo-x", reason="", actor_subject="op", ch=ch)


def test_a_release_whose_bot_cutoff_fails_keeps_what_a_retry_needs(env: Any, monkeypatch) -> None:
    # The bots still name the namespace after the row is rewritten: a retry
    # reading the released row must still find them, and cut them off.
    from server.auth import machines

    forgotten: list[str] = []
    ended: list[str] = []

    async def naming(org: str, ns: str, *, ch: Any) -> list[str]:
        return [] if forgotten else ["triage"]

    async def forget(org: str, ns: str, by: str, *, ch: Any) -> list[str]:
        forgotten.append(ns)
        return ["triage"]

    async def down(subject: str, **k: Any) -> datetime:
        if subject == "sa:triage":
            raise RuntimeError("ClickHouse away")
        ended.append(subject)
        return datetime(2026, 1, 3)

    monkeypatch.setattr(machines, "naming", naming)
    monkeypatch.setattr(machines, "forget_namespace", forget)
    monkeypatch.setattr(account_admin, "end_tokens", down)
    ch = _CH(_AGENT_ROW)
    with pytest.raises(RuntimeError):
        asyncio.run(_release(ch))
    assert ended == ["sa:fleet:a1"] and forgotten == []
    (released,) = ch.inserts["loom.namespaces"]

    async def end_tokens(subject: str, **k: Any) -> datetime:
        ended.append(subject)
        return datetime(2026, 1, 3)

    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    ended.clear()
    out = asyncio.run(_release(_CH(released)))  # the retry reads the rewritten row
    assert out is not None
    assert out.released and forgotten == ["repo-x"]
    assert ended == ["sa:triage"]  # the agent was cut off by the first pass


def test_the_bots_cutoff_is_taken_after_the_row_insert_returns(
    env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A bot's exchange reads its live namespaces sequentially: one that read
    # before the insert returned dates its token earlier, and one after gets
    # none of this namespace. The agent is cut off before its row changes.
    from server.auth import machines

    run, _ = env
    start = datetime(2026, 1, 3, 12, 0, 0, tzinfo=UTC)
    clock = [start]
    events: list[tuple[str, datetime]] = []

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return clock[0]

    async def naming(org: str, ns: str, *, ch: Any) -> list[str]:
        return ["triage"]

    async def forget(org: str, ns: str, by: str, *, ch: Any) -> list[str]:
        events.append(("forget", clock[0]))
        return ["triage"]

    async def cut_off(subject: str, *, issued_before: datetime, **k: Any) -> None:
        events.append((subject, issued_before))

    plain_insert = _CH.insert

    async def slow_insert(self: _CH, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
        await plain_insert(self, table, rows, **k)
        if table == "loom.namespaces":
            clock[0] += timedelta(seconds=8)  # a slow insert
            events.append(("row", clock[0]))

    monkeypatch.setattr(revocation, "datetime", _Clock)
    monkeypatch.setattr(machines, "naming", naming)
    monkeypatch.setattr(machines, "forget_namespace", forget)
    monkeypatch.setattr(revocation, "cut_off", cut_off)
    monkeypatch.setattr(_CH, "insert", slow_insert)
    assert run(_tok(["org_admin"]), row=_AGENT_ROW)[0].released
    assert [e for e, _ in events] == ["sa:fleet:a1", "row", "sa:triage", "forget"]
    returned, bots_cutoff = events[1][1], events[2][1]
    # Minted the second the insert returned, on a replica ahead by the skew.
    assert returned + timedelta(seconds=revocation.CLOCK_SKEW_S) < bots_cutoff
    # And the wait is on the last cutoff, a few seconds out, not the claim's deadline.
    (waited,) = env[1]["waited"]
    assert waited == bots_cutoff
    assert bots_cutoff - returned <= timedelta(seconds=1 + revocation.CLOCK_SKEW_S)


def test_a_release_failing_after_its_row_is_rewritten_says_to_retry(
    env: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Cut off at the claim's deadline before the bots' cutoff: their tokens
    # keep the namespace until a retry ends them.
    from server.auth import machines
    from server.core.errors import BackendUnavailableError

    async def naming(org: str, ns: str, *, ch: Any) -> list[str]:
        return ["triage"]

    async def deadline(subject: str, **k: Any) -> datetime:
        raise BackendUnavailableError("the change did not finish in time", operation="grant_change")

    monkeypatch.setattr(machines, "naming", naming)
    monkeypatch.setattr(account_admin, "end_tokens", deadline)
    ch = _CH(_ROW)
    with caplog.at_level("WARNING"), pytest.raises(BackendUnavailableError) as failed:
        asyncio.run(_release(ch))
    assert "loom.namespaces" in ch.inserts
    assert "retry it" in failed.value.message and "bots' tokens" in failed.value.message
    assert "retry it to end its bots' tokens" in caplog.text


def test_a_release_failing_before_its_row_is_rewritten_warns_of_nothing(
    env: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from server.auth import machines

    async def down(org: str, ns: str, *, ch: Any) -> list[str]:
        raise RuntimeError("ClickHouse away")

    monkeypatch.setattr(machines, "naming", down)
    ch = _CH(_ROW)
    with caplog.at_level("WARNING"), pytest.raises(RuntimeError):
        asyncio.run(_release(ch))
    assert "loom.namespaces" not in ch.inserts and "claim_release" not in caplog.text
