"""A person claims an agent's namespace with the code its sign-up returned: the
code never signs in (dashboard, OAuth authorize form, device grant), it claims
once, and the claim leaves the person's own password as the namespace's only
one. An agent account made before the code was stored prefixed is unclaimed by
its shape alone. The statements run on clickhouse-local against the real table."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server.auth.self_signup as ss
from server.auth import agents, claim_codes, device, login, oauth, passwords, revocation
from server.identity.app import agent_claim
from server.identity.store import grants, namespaces
from tests.unit.clickhouse_local import CLICKHOUSE, KeptCH, ddl
from tests.unit.oauth_store_fake import MemoryOAuthStore, oauth_app

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_NS = "agent-nightly-triage"
_LEGACY = "agent-old-bot"


class _Req:
    headers = {"host": "loom.test:7777"}
    client = type("C", (), {"host": "10.0.0.1"})()
    url = type("U", (), {"scheme": "http"})()


@pytest.fixture(autouse=True)
def _claims_in_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.claims_fake import memory_claims

    memory_claims(monkeypatch)


def _account(namespace: str, owner: str, email: str, password_hash: str, tags: str) -> str:
    return (
        "INSERT INTO loom.namespaces (org, namespace, owner_email, contact_email, password_hash,"
        f" tags, active, created_at, updated_at) VALUES ('dev', '{namespace}', '{owner}',"
        f" '{email}', '{password_hash}', {tags}, 1, now(), now())"
    )


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, auth_attempts: Any, tmp_path: Path) -> dict[str, Any]:
    """An agent signed up through the real route, one signed up before its code was
    stored prefixed (a plain hash), and a person whose account carries the agent
    tag and an email, all in one kept clickhouse-local database."""
    from server.settings import settings

    monkeypatch.setattr(settings, "self_signup", "open")
    monkeypatch.setattr(settings, "oauth_login_provider", "password")
    monkeypatch.setattr(settings, "self_signup_org", "dev")
    ch = KeptCH(
        tmp_path / "ch", ddl("namespaces"), ddl("namespace_grants"), ddl("service_accounts")
    )
    for module in (ss, agent_claim, login):
        monkeypatch.setattr(module, "get_client", lambda: ch)
    import server.core.ch as core_ch

    monkeypatch.setattr(core_ch, "get_client", lambda: ch)  # read lazily by service_accounts
    monkeypatch.setattr(namespaces, "_default_client", lambda: ch)
    monkeypatch.setattr(grants, "_default_client", lambda: ch)
    out = asyncio.run(ss.agent_signup(ss.AgentSignupInput(name="nightly triage"), _Req()))
    legacy_code = "legacy-claim-code"
    ch.create(
        _account(_LEGACY, _LEGACY, "", passwords.hash_password(legacy_code), "['agent']"),
        _account(
            "agent-lee",
            "agent-lee",
            "lee@acme.com",
            passwords.hash_password("lee's password"),
            "['agent']",
        ),
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, **kw: Any) -> str:
        events.append((event_type, kw))
        return ""

    async def no_restricted(subject: str, org: str, names: list[str]) -> list[str]:
        return list(names)

    from contextlib import asynccontextmanager

    from server.auth import attempts

    hashed: list[str] = []
    real_hash, real_slot, in_slot = agent_claim.hash_password_off_loop, attempts.slot, [False]
    hashed_in_slot: list[bool] = []

    @asynccontextmanager
    async def slot() -> Any:
        async with real_slot():
            in_slot[0] = True
            try:
                yield
            finally:
                in_slot[0] = False

    async def counted_hash(password: str) -> str:
        hashed.append(password)
        hashed_in_slot.append(in_slot[0])
        return await real_hash(password)

    skew_waits: list[bool] = []

    async def skew_wait() -> None:
        skew_waits.append(True)

    monkeypatch.setattr(attempts, "slot", slot)
    monkeypatch.setattr(agent_claim, "_skew_wait", skew_wait)

    monkeypatch.setattr(agent_claim.audit, "emit", emit)
    monkeypatch.setattr(agent_claim, "hash_password_off_loop", counted_hash)
    monkeypatch.setattr(login, "widen_with_restricted_grants", no_restricted)
    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    # Subject cutoffs as the revocation table holds them, read by the bearer
    # check (`is_revoked`) and by every OAuth grant (`subjects_revoked`).
    monkeypatch.setattr(revocation, "_revoked_before", {})
    cutoffs: dict[str, int] = {}

    async def cut_off(subject: str, *, issued_before: Any, reason: str, revoked_by: str) -> Any:
        cutoffs[subject] = int(issued_before.timestamp())
        revocation._apply_locally(subject, cutoffs[subject])
        return {}

    async def subjects_revoked(subjects: list[str], *, issued_at: int) -> bool:
        return any(issued_at < cutoffs.get(s, 0) for s in subjects)

    waited: list[Any] = []
    audited_before_wait: list[int] = []

    async def wait_out(cutoff: Any) -> None:
        waited.append(cutoff)
        audited_before_wait.append(len(events))

    async def active(subject: str, org: str, roles: list[str]) -> list[str]:
        return roles

    monkeypatch.setattr(revocation, "cut_off", cut_off)
    monkeypatch.setattr(revocation, "subjects_revoked", subjects_revoked)

    async def stored_cutoffs(subjects: list[str], *, permanent: bool = False) -> Any:
        return {s: cutoffs[s] for s in subjects if s in cutoffs}

    monkeypatch.setattr(revocation, "cutoffs", stored_cutoffs)
    monkeypatch.setattr(revocation, "wait_out", wait_out)
    monkeypatch.setattr(login, "refreshed_roles", active)
    return {
        "cutoffs": cutoffs,
        "waited": waited,
        "audited_before_wait": audited_before_wait,
        "ch": ch,
        "code": out.claim_code,
        "legacy_code": legacy_code,
        "events": events,
        "hashed": hashed,
        "hashed_in_slot": hashed_in_slot,
        "skew_waits": skew_waits,
    }


def _app() -> TestClient:
    from fastapi import FastAPI

    app = FastAPI()
    app.add_exception_handler(oauth.OAuthError, oauth.oauth_error_response)
    app.include_router(agent_claim.router)
    return TestClient(app)


def _claim(
    c: TestClient, code: str, password: str, repeat: str | None = None, namespace: str = _NS
) -> Any:
    return c.post(
        "/oauth/agent-claim",
        data={
            "namespace": namespace,
            "code": code,
            "password": password,
            "repeat": password if repeat is None else repeat,
        },
    )


def _sign_in(password: str, namespace: str = _NS) -> Any:
    return asyncio.run(login.resolve_user_credentials(namespace, password))


def _row(world: dict[str, Any], namespace: str) -> dict[str, Any]:
    row = asyncio.run(namespaces.fetch_row("dev", namespace))
    assert row is not None
    return row


def test_the_code_claims_once_and_the_claimer_signs_in_with_their_password(world) -> None:
    c = _app()
    page = c.get("/oauth/agent-claim", params={"namespace": _NS})
    assert page.status_code == 200 and 'name="code"' in page.text and _NS in page.text
    assert "up to a minute" in page.text

    r = _claim(c, world["code"], "correct horse")
    assert r.status_code == 200 and _NS in r.text
    # The sign-in slot is held for the code's check only, not for the hash.
    assert world["hashed_in_slot"] == [False]
    row = _row(world, _NS)
    assert row["password_hash"].startswith("$argon2")
    assert passwords.verify_password(row["password_hash"], "correct horse")
    assert (row["owner_email"], row["active"]) == (_NS, True)
    assert row["tags"] == ["agent", claim_codes.CLAIMED_TAG]
    [(event, kw)] = world["events"]
    assert (event, kw["namespace"], kw["actor"]) == ("auth.agent_claimed", _NS, _NS)
    # The agent's own grant is not another holder's.
    assert kw["subject"] == {"other_grants": []} and "hold grants" not in r.text

    # The code is spent: neither a second claim nor a sign-in takes it.
    assert _claim(c, world["code"], "attacker pass").status_code == 400
    assert passwords.verify_password(_row(world, _NS)["password_hash"], "correct horse")
    assert _sign_in(world["code"]) is None
    subject, org, names, _ = _sign_in("correct horse")
    assert (subject, org, names) == (_NS, "dev", [_NS])


def test_a_claim_drops_the_unclaimed_default_description(world) -> None:
    row = _row(world, _LEGACY)
    old = {**row, "description": "Created via agent self-signup; unclaimed."}
    asyncio.run(world["ch"].insert("loom.namespaces", [old]))
    assert (
        _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY).status_code == 200
    )
    assert _row(world, _LEGACY)["description"] == ss.AGENT_DESCRIPTION
    assert _claim(_app(), world["code"], "correct horse").status_code == 200
    assert _row(world, _NS)["description"] == ss.AGENT_DESCRIPTION


def test_an_unclaimed_code_never_signs_in(world) -> None:
    assert _sign_in(world["code"]) is None
    assert not passwords.verify_password(_row(world, _NS)["password_hash"], world["code"])


def test_an_agent_made_before_the_prefix_is_claimed_once_by_its_plain_code(world) -> None:
    code = world["legacy_code"]
    assert _sign_in(code, _LEGACY) is None
    c = _app()
    assert _claim(c, code, "correct horse", namespace=_LEGACY).status_code == 200
    assert _sign_in(code, _LEGACY) is None
    assert _sign_in("correct horse", _LEGACY)[0] == _LEGACY
    assert _claim(c, code, "attacker pass", namespace=_LEGACY).status_code == 400


def test_a_person_with_an_email_is_no_unclaimed_agent(world) -> None:
    assert _sign_in("lee's password", "agent-lee")[0] == "agent-lee"
    r = _claim(_app(), "lee's password", "correct horse", namespace="agent-lee")
    assert r.status_code == 400


def test_a_settings_write_keeps_an_old_agent_unclaimed(world) -> None:
    # A form sends tags=[] by default: the `agent` tag goes, the shape stays.
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    form = NamespaceRegisterInput(org="dev", namespace=_LEGACY, owner_email=_LEGACY)
    stored = _row(world, _LEGACY)
    row = registry_row(form, stored, stored["updated_at"] + timedelta(seconds=1))
    assert row["tags"] == [] and row["password_hash"] == _row(world, _LEGACY)["password_hash"]
    asyncio.run(world["ch"].insert("loom.namespaces", [row]))
    assert _sign_in(world["legacy_code"], _LEGACY) is None
    assert (
        _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY).status_code == 200
    )
    assert _sign_in("correct horse", _LEGACY)[0] == _LEGACY


def _code_grant(c: TestClient) -> dict[str, Any]:
    """An OAuth session for the namespace's subject, as a sign-in with the code
    gave one before the code was stored prefixed: its access and refresh token."""
    import secrets
    import time

    verifier = "v" * 43
    code = f"code-{secrets.token_hex(4)}"
    grant = {
        "client_id": "loom-test",
        "redirect_uri": "http://127.0.0.1:9999/cb",
        "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode(),
        "subject": _LEGACY,
        "org": "dev",
        "namespaces": [_LEGACY],
        "roles": ["writer"],
        "exp": int(time.time()) + 60,
        "since": int(time.time()),
        "family": f"f-{secrets.token_hex(4)}",
        "upstream_refresh": "",
    }
    asyncio.run(oauth.store.save_code(code, grant))
    r = c.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": grant["redirect_uri"],
            "client_id": "loom-test",
            "code_verifier": verifier,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _refresh(c: TestClient, refresh_token: str) -> Any:
    return c.post(
        "/oauth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": "loom-test",
        },
    )


def test_a_claim_ends_the_tokens_and_sessions_the_code_started(world) -> None:
    from server.auth.jwt import mint_token, verify_token

    c = TestClient(oauth_app())
    session = _code_grant(c)
    dashboard = mint_token(subject=_LEGACY, org="dev", namespaces=[_LEGACY], roles=["writer"])
    issued = verify_token(dashboard).issued_at
    assert not revocation.is_revoked(_LEGACY, issued_at=issued)
    # The session refreshes before the claim, and the rotated token is the one held.
    refreshed = _refresh(c, session["refresh_token"])
    assert refreshed.status_code == 200, refreshed.text

    assert (
        _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY).status_code == 200
    )
    assert revocation.is_revoked(_LEGACY, issued_at=issued)
    r = _refresh(c, refreshed.json()["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    # The page answers once every replica is past the cutoff, so the claimer's
    # own sign-in that follows is not ended by it.
    assert world["waited"] == [revocation_cutoff(world, _LEGACY)]


def test_the_cutoff_waited_out_follows_the_password_write(world, monkeypatch) -> None:
    # A replica of the build before signs in with a plain-hash code until the
    # write lands: the cutoff the page waits out is taken after it. The cutoff
    # clock is a seam the write moves on.
    from datetime import UTC, datetime

    from server.core.ch import sequential_reads_active

    clock, steps = [datetime(2026, 1, 1, tzinfo=UTC)], []

    def from_now_on() -> datetime:
        steps.append("cutoff")
        return clock[0]

    monkeypatch.setattr(revocation, "from_now_on", from_now_on)
    ch, written = world["ch"], []
    insert, fetch_row = ch.insert, namespaces.fetch_row

    async def slow_insert(table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        await insert(table, rows, settings)
        if table == "loom.namespaces":
            clock[0] += timedelta(seconds=2)
            written.append(clock[0])
            steps.append("write")

    async def read(org: str, namespace: str) -> Any:
        steps.append(("read", sequential_reads_active()))
        return await fetch_row(org, namespace)

    async def skew_wait() -> None:
        steps.append("skew")

    monkeypatch.setattr(agent_claim.namespaces, "fetch_row", read)
    monkeypatch.setattr(agent_claim, "_skew_wait", skew_wait)

    monkeypatch.setattr(ch, "insert", slow_insert)
    assert (
        _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY).status_code == 200
    )
    [write] = written
    [cutoff] = world["waited"]
    assert cutoff >= write
    # The last cutoff follows a sequentially consistent read of the write and a
    # wait of the clock-skew margin, so a replica whose clock leads by up to that
    # margin still has its full CLOCK_SKEW_S + 1 seconds ended.
    assert steps[-4:] == ["write", ("read", True), "skew", "cutoff"]
    # The claim is audited when it lands, not after the wait.
    assert world["audited_before_wait"] == [1]


def test_a_failed_read_back_still_waits_and_cuts_off_again(world, monkeypatch) -> None:
    from server.identity.app import account_admin

    real_end, real_fetch, cutoffs, fetches = account_admin.end_tokens, namespaces.fetch_row, [], [0]

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        cutoffs.append(await real_end(subject, by=by, reason=reason))
        return cutoffs[-1]

    async def fetch_row(org: str, namespace: str) -> Any:
        fetches[0] += 1
        if fetches[0] == 2:  # the read back after the write
            raise RuntimeError("ClickHouse away")
        return await real_fetch(org, namespace)

    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    monkeypatch.setattr(agent_claim.namespaces, "fetch_row", fetch_row)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 200 and "Claimed" in r.text
    assert len(cutoffs) == 2 and world["skew_waits"] == [True]
    assert world["waited"] == cutoffs[1:]
    assert [e for e, _ in world["events"]] == ["auth.agent_claimed"]
    assert passwords.verify_password(_row(world, _LEGACY)["password_hash"], "correct horse")


def test_a_second_cutoff_that_stores_then_fails_is_waited_past(world, monkeypatch) -> None:
    # The failed call may have stored its cutoff: the page waits past any it could
    # have written, so the claimer's next sign-in is not ended by it.
    from datetime import UTC, datetime

    from server.identity.app import account_admin

    real_end, calls = account_admin.end_tokens, []
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]

    def from_now_on() -> datetime:
        clock[0] += timedelta(seconds=1)
        return clock[0]

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        calls.append(await real_end(subject, by=by, reason=reason))
        if len(calls) == 2:
            raise RuntimeError("revocation store away after the write")
        return calls[-1]

    monkeypatch.setattr(revocation, "from_now_on", from_now_on)
    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 200 and "Claimed" in r.text
    [waited] = world["waited"]
    stored = datetime.fromtimestamp(world["cutoffs"][_LEGACY], UTC)
    assert stored == calls[1] and waited > calls[0] and waited >= calls[1]
    assert [e for e, _ in world["events"]] == ["auth.agent_claimed"]


def test_a_second_submit_answers_after_the_first_claims_last_cutoff(world) -> None:
    # Both submits proved the code before either wrote; the second finds the
    # account claimed and waits to a bound past the first's last cutoff, so the
    # sign-in its answer asks for is not ended by that cutoff.
    stored_hash = _row(world, _LEGACY)["password_hash"]

    async def both() -> list[Any]:
        first = asyncio.create_task(
            agent_claim.claim_shielded("dev", _LEGACY, stored_hash, passwords.hash_password("a1"))
        )
        await asyncio.sleep(0)
        second = agent_claim.claim_shielded(
            "dev", _LEGACY, stored_hash, passwords.hash_password("a2")
        )
        return list(await asyncio.gather(first, second))

    (first, c1, _), (second, c2, _) = asyncio.run(both())
    assert (first, second) == ("claimed", "taken")
    assert c2 > c1 and int(c2.timestamp()) > world["cutoffs"][_LEGACY]
    # A sign-in once the answer came is issued at or after the bound it waited
    # out, which the first claim's stored cutoff does not end.
    assert not revocation.is_revoked(_LEGACY, issued_at=int(c2.timestamp()))


def test_the_page_tells_a_second_submit_to_sign_in_once_the_first_is_done(
    world, monkeypatch
) -> None:
    from contextlib import asynccontextmanager
    from datetime import UTC, datetime

    now = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(agent_claim, "_now", lambda: now)

    real_change = agent_claim.account_change

    @asynccontextmanager
    async def claimed_first(org: str, namespace: str) -> Any:
        # The first submit's claim and last cutoff land while this one waits.
        row = await namespaces.fetch_row(org, namespace)
        assert row is not None
        claimed = {
            **row,
            "password_hash": passwords.hash_password("first"),
            "tags": [*row["tags"], claim_codes.CLAIMED_TAG],
        }
        await world["ch"].insert("loom.namespaces", [claimed])
        async with real_change(org, namespace):
            yield

    monkeypatch.setattr(agent_claim, "account_change", claimed_first)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 400 and "claimed a moment ago" in r.text
    # The release and a landed check, its read-back bound, a skew wait, a
    # cutoff's margin and a clock's lead: 5 + 5 + 5 + 5 + 6 + 5 seconds past the
    # moment it found the account claimed.
    assert world["waited"] == [now + timedelta(seconds=31)]
    assert world["events"] == [] and _LEGACY not in world["cutoffs"]


def test_a_busy_account_answers_a_page_to_wait_and_retry(world, monkeypatch) -> None:
    from contextlib import asynccontextmanager

    from server.core.errors import GrantBusyError

    @asynccontextmanager
    async def busy(org: str, namespace: str) -> Any:
        raise GrantBusyError("another change to the account is running")
        yield

    monkeypatch.setattr(agent_claim, "account_change", busy)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 503 and r.headers["Retry-After"] == "60"
    assert r.headers["content-type"].startswith("text/html")
    assert "An earlier submit may still be claiming" in r.text


def test_the_code_is_refused_as_the_new_password(world) -> None:
    r = _claim(_app(), world["code"], world["code"])
    assert r.status_code == 400 and "other than the code" in r.text
    assert world["hashed"] == []
    assert _claim(_app(), world["code"], "correct horse").status_code == 200


def _grant(namespace: str, subject: str, role: str, minutes_ago: int = 0) -> dict[str, Any]:
    from datetime import UTC, datetime

    granted_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    return {
        "org": "dev",
        "namespace": namespace,
        "subject": subject,
        "role": role,
        "granted_at": granted_at - timedelta(minutes=minutes_ago),
        "granted_by": namespace,
        "revoked_at": None,
        "version": time.time_ns(),
    }


def test_the_claim_names_every_grant_others_hold(world) -> None:
    # The oldest of more than a page of grants is named too.
    rows = [_grant(_LEGACY, "kept@acme.com", "admin", minutes_ago=600)]
    rows += [_grant(_LEGACY, f"filler-{i}@acme.com", "reader") for i in range(120)]
    asyncio.run(world["ch"].insert("loom.namespace_grants", rows))
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 200 and "kept@acme.com" in r.text
    [(_, kw)] = world["events"]
    assert len(kw["subject"]["other_grants"]) == 121
    assert "kept@acme.com" in kw["subject"]["other_grants"]


def test_the_agent_token_is_named_once_it_holds_more_than_writer(world) -> None:
    [agent] = [
        r["subject"]
        for r in asyncio.run(grants.active_on("dev", _NS))
        if str(r["subject"]).startswith("sa:")
    ]
    asyncio.run(world["ch"].insert("loom.namespace_grants", [_grant(_NS, agent, "admin")]))
    r = _claim(_app(), world["code"], "correct horse")
    assert r.status_code == 200 and agent in r.text
    [(_, kw)] = world["events"]
    assert kw["subject"] == {"other_grants": [agent]}


def _timed_out() -> Exception:
    from server.core.errors import BackendTimeoutError

    return BackendTimeoutError("insert ran past its deadline", statement_class="write", timeout_s=1)


def _unavailable() -> Exception:
    from server.core.errors import BackendUnavailableError

    return BackendUnavailableError("the insert timed out after it landed")


@pytest.mark.parametrize("failure", [_unavailable, _timed_out, lambda: RuntimeError("driver")])
def test_a_write_that_lands_but_reports_a_failure_still_claims(world, monkeypatch, failure) -> None:
    from server.identity.app import account_admin

    real_end, cutoffs = account_admin.end_tokens, []

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        cutoffs.append(await real_end(subject, by=by, reason=reason))
        return cutoffs[-1]

    ch, real_insert = world["ch"], world["ch"].insert

    async def insert(table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        await real_insert(table, rows, settings)
        if table == "loom.namespaces":
            raise failure()

    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    monkeypatch.setattr(ch, "insert", insert)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 200 and "Claimed" in r.text
    assert [e for e, _ in world["events"]] == ["auth.agent_claimed"]
    assert len(cutoffs) == 2 and world["waited"] == cutoffs[1:]


def test_a_raw_error_before_the_write_answers_the_page_and_keeps_the_code(
    world, monkeypatch
) -> None:
    from server.identity.app import account_admin

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        raise RuntimeError("driver")

    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 503 and r.headers["content-type"].startswith("text/html")
    assert passwords.verify_password(_row(world, _LEGACY)["password_hash"], world["legacy_code"])


def test_grants_that_cannot_be_listed_are_named_as_such(world, monkeypatch) -> None:
    calls: list[str] = []

    async def failing(org: str, namespace: str) -> list[str]:
        calls.append(namespace)
        raise RuntimeError("driver")

    monkeypatch.setattr(agent_claim, "_other_grants", failing)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 200 and "could not be listed" in r.text
    [(_, kw)] = world["events"]
    # Listed once, for the audit row and the page alike.
    assert kw["subject"] == {"other_grants": None} and calls == [_LEGACY]


def test_a_write_that_does_not_confirm_still_cuts_off_after_it(world, monkeypatch) -> None:
    # The insert fails before it lands and the read-back cannot find it: the
    # code stays, the cutoff after the write still runs, and the page says wait.
    from server.identity.app import account_admin

    real_end, cutoffs = account_admin.end_tokens, []

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        cutoffs.append(await real_end(subject, by=by, reason=reason))
        return cutoffs[-1]

    ch, real_insert = world["ch"], world["ch"].insert

    async def insert(table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        if table == "loom.namespaces":
            raise _timed_out()
        await real_insert(table, rows, settings)

    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    monkeypatch.setattr(ch, "insert", insert)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 503 and "An earlier submit may still be claiming" in r.text
    assert len(cutoffs) == 2 and world["events"] == []
    assert passwords.verify_password(_row(world, _LEGACY)["password_hash"], world["legacy_code"])


def test_a_personal_token_named_like_the_agent_is_named(world) -> None:
    # Only the token sign-up stored is the agent's; one a person named after the
    # namespace, with the same subject shape, is another holder.
    from server.identity.app.me_service_accounts import new_token_subject

    lookalike = new_token_subject("dev", _NS, _NS)
    asyncio.run(world["ch"].insert("loom.namespace_grants", [_grant(_NS, lookalike, "writer")]))
    r = _claim(_app(), world["code"], "correct horse")
    assert r.status_code == 200 and lookalike in r.text
    [(_, kw)] = world["events"]
    assert kw["subject"] == {"other_grants": [lookalike]}


@pytest.mark.parametrize("failure", [_unavailable, _timed_out, lambda: RuntimeError("driver")])
def test_a_backend_failure_during_the_code_check_answers_the_page(
    world, monkeypatch, failure
) -> None:
    async def unavailable(namespace: str, code: str) -> Any:
        raise failure()

    monkeypatch.setattr(agent_claim, "_proven", unavailable)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 503 and r.headers["Retry-After"] == "60"
    assert r.headers["content-type"].startswith("text/html")


def test_the_steps_after_the_write_outlast_the_account_lease(world, monkeypatch) -> None:
    # The read-back, the skew wait and the second cutoff run after the lease is
    # released, so its 20 s bound cannot cut them short.
    from server.auth import grant_claims
    from server.identity.app import account_admin

    real_end, cutoffs = account_admin.end_tokens, []

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        cutoffs.append(await real_end(subject, by=by, reason=reason))
        return cutoffs[-1]

    async def slow_skew_wait() -> None:
        await asyncio.sleep(3.0)  # past the lease's bound below

    monkeypatch.setattr(grant_claims, "CHANGE_S", 2.0)
    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    monkeypatch.setattr(agent_claim, "_skew_wait", slow_skew_wait)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 200 and "Claimed" in r.text
    assert len(cutoffs) == 2 and world["waited"] == cutoffs[1:]
    assert [e for e, _ in world["events"]] == ["auth.agent_claimed"]


def test_a_slow_audit_is_not_cut_short_by_the_lease(world, monkeypatch) -> None:
    # The audit runs after the lease, so the lease's bound cannot drop it.
    from server.auth import grant_claims

    real_grants = agent_claim._other_grants

    async def slow_grants(org: str, namespace: str) -> list[str]:
        await asyncio.sleep(3.0)
        return await real_grants(org, namespace)

    monkeypatch.setattr(grant_claims, "CHANGE_S", 2.0)
    monkeypatch.setattr(agent_claim, "_other_grants", slow_grants)
    r = _claim(_app(), world["legacy_code"], "correct horse", namespace=_LEGACY)
    assert r.status_code == 200 and "Claimed" in r.text
    assert [e for e, _ in world["events"]] == ["auth.agent_claimed"]


def test_a_cancelled_request_still_stores_the_last_cutoff(world, monkeypatch) -> None:
    # A client that disconnects cancels the handler during the skew wait: the
    # claim's own task runs on and stores its second cutoff.
    from server.identity.app import account_admin

    real_end, cutoffs = account_admin.end_tokens, []

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        cutoffs.append(await real_end(subject, by=by, reason=reason))
        return cutoffs[-1]

    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    stored_hash = _row(world, _LEGACY)["password_hash"]

    async def run() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def skew_wait() -> None:
            entered.set()
            await release.wait()

        monkeypatch.setattr(agent_claim, "_skew_wait", skew_wait)
        handler = asyncio.create_task(
            agent_claim.claim_shielded("dev", _LEGACY, stored_hash, passwords.hash_password("p"))
        )
        await entered.wait()
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
        release.set()
        await asyncio.gather(*agent_claim._running)

    asyncio.run(run())
    assert len(cutoffs) == 2
    assert passwords.verify_password(_row(world, _LEGACY)["password_hash"], "p")


def test_a_shutdown_drain_waits_for_a_claims_last_cutoff(world, monkeypatch) -> None:
    from server.identity.app import account_admin

    real_end, cutoffs = account_admin.end_tokens, []

    async def end_tokens(subject: str, *, by: str, reason: str) -> Any:
        cutoffs.append(await real_end(subject, by=by, reason=reason))
        return cutoffs[-1]

    monkeypatch.setattr(account_admin, "end_tokens", end_tokens)
    stored_hash = _row(world, _LEGACY)["password_hash"]

    async def run() -> None:
        entered = asyncio.Event()

        async def skew_wait() -> None:
            entered.set()
            await asyncio.sleep(0.5)

        monkeypatch.setattr(agent_claim, "_skew_wait", skew_wait)
        handler = asyncio.create_task(
            agent_claim.claim_shielded("dev", _LEGACY, stored_hash, passwords.hash_password("p"))
        )
        await entered.wait()
        handler.cancel()
        await agent_claim.drain(timeout_s=30.0)
        assert not agent_claim._running

    asyncio.run(run())
    assert len(cutoffs) == 2
    assert passwords.verify_password(_row(world, _LEGACY)["password_hash"], "p")


def revocation_cutoff(world: dict[str, Any], subject: str) -> Any:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(world["cutoffs"][subject], UTC)


def test_a_wrong_code_or_a_bad_password_claims_nothing_and_keeps_the_code(world) -> None:
    c = _app()
    assert _claim(c, "not-the-code", "correct horse").status_code == 400
    assert _claim(c, world["code"], "short").status_code == 400
    assert _claim(c, world["code"], "correct horse", "correct hors").status_code == 400
    assert _row(world, _NS)["password_hash"].startswith(claim_codes.CLAIM_PREFIX)
    assert _claim(c, world["code"], "correct horse").status_code == 200


def test_only_a_proven_code_costs_a_password_hash(world) -> None:
    c = _app()
    assert _claim(c, "anything", "correct horse", namespace="agent-unknown").status_code == 400
    assert _claim(c, "not-the-code", "correct horse").status_code == 400
    assert world["hashed"] == []
    for _ in range(9):
        _claim(c, "not-the-code", "correct horse")
    throttled = _claim(c, world["code"], "correct horse")
    assert throttled.status_code == 429 and throttled.headers["Retry-After"] == "900"
    assert "Too many failed attempts for this namespace" in throttled.text
    assert world["hashed"] == []
    assert _row(world, _NS)["password_hash"].startswith(claim_codes.CLAIM_PREFIX)


def test_a_claim_that_lands_first_is_not_overwritten(world, monkeypatch) -> None:
    # The row is read again once the account is held: a claim that held it
    # first changed the hash, and this one is refused.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def claimed_meanwhile(org: str, namespace: str) -> Any:
        row = await namespaces.fetch_row("dev", _NS)
        assert row is not None
        await world["ch"].insert("loom.namespaces", [{**row, "password_hash": "$argon2id$other"}])
        yield

    monkeypatch.setattr(agent_claim, "account_change", claimed_meanwhile)
    assert _claim(_app(), world["code"], "correct horse").status_code == 400
    assert _row(world, _NS)["password_hash"] == "$argon2id$other"


def test_the_claimed_mark_survives_a_settings_write_and_no_form_sets_it(world) -> None:
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    # The mark rides the `machine:` prefix, which `namespace.register` carries
    # forward from the row and drops from a form on this build and the one before
    # it; a trust's name never holds ':', so the mark names no trust.
    trust = claim_codes.CLAIMED_TAG.removeprefix(agents.TAG_PREFIX)
    assert trust != claim_codes.CLAIMED_TAG and ":" in trust

    assert _claim(_app(), world["code"], "correct horse").status_code == 200
    stored = _row(world, _NS)
    now = stored["updated_at"] + timedelta(seconds=1)
    form = NamespaceRegisterInput(org="dev", namespace=_NS, owner_email=_NS, tags=["mine"])
    tags = registry_row(form, stored, now)["tags"]
    assert tags == ["mine", claim_codes.CLAIMED_TAG]
    forged = NamespaceRegisterInput(
        org="dev", namespace="x", owner_email="x", tags=[claim_codes.CLAIMED_TAG]
    )
    assert registry_row(forged, None, now)["tags"] == []


def test_a_released_claimed_namespace_takes_the_owner_a_register_names(world) -> None:
    # The claim's mark is no trust's: it does not bind the owner as a trust's mark
    # does, so a release (owner cleared) followed by a register names the new one.
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    assert _claim(_app(), world["code"], "correct horse").status_code == 200
    released = {**_row(world, _NS), "owner_email": "", "password_hash": "", "active": False}
    assert claim_codes.CLAIMED_TAG in released["tags"]
    form = NamespaceRegisterInput(org="dev", namespace=_NS, owner_email="ops")
    now = released["updated_at"] + timedelta(seconds=1)
    assert registry_row(form, released, now)["owner_email"] == "ops"


def test_the_row_check_and_the_statement_agree(world) -> None:
    rows = asyncio.run(
        world["ch"].query_all(
            "SELECT *, {u} AS unclaimed FROM loom.namespaces FINAL".replace(
                "{u}", claim_codes.UNCLAIMED_SQL
            )
        )
    )
    assert {r["namespace"]: bool(r["unclaimed"]) for r in rows} == {
        _NS: True,
        _LEGACY: True,
        "agent-lee": False,
    }
    assert all(claim_codes.is_unclaimed(r) == bool(r["unclaimed"]) for r in rows)
    assert _claim(_app(), world["code"], "correct horse").status_code == 200
    assert not claim_codes.is_unclaimed(_row(world, _NS))


def test_an_sso_link_refuses_an_unclaimed_agent(world, monkeypatch) -> None:
    # An email on the row would take it out of the unclaimed shape and leave its
    # code as its password.
    from server.identity.app import account_admin

    monkeypatch.setattr(account_admin.settings, "oauth_login_provider", "oidc")
    for namespace in (_NS, _LEGACY):
        with pytest.raises(ValueError, match="nobody has claimed"):
            asyncio.run(account_admin.link_plan("lee@corp.example", "dev", namespace))


def test_no_claim_while_sso_is_on(world, monkeypatch) -> None:
    monkeypatch.setattr(oauth.settings, "oauth_login_provider", "oidc")
    c = _app()
    assert c.get("/oauth/agent-claim").status_code == 400
    assert _claim(c, world["code"], "correct horse").status_code == 400
    assert _row(world, _NS)["password_hash"].startswith(claim_codes.CLAIM_PREFIX)


# ---- the other password forms take no claim code ----


def _authorize(c: TestClient, password: str) -> Any:
    redirect = "http://127.0.0.1:9999/cb"
    reg = c.post("/oauth/register", json={"redirect_uris": [redirect], "client_name": "t"}).json()
    verifier = "v" * 43
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return c.post(
        "/oauth/authorize",
        data={
            "client_id": reg["client_id"],
            "redirect_uri": redirect,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "identifier": _NS,
            "password": password,
        },
        follow_redirects=False,
    )


def _device_sign_in(c: TestClient, password: str) -> Any:
    grant = c.post("/oauth/device_authorization", data={"client_id": device.DEVICE_CLIENT_ID})
    assert grant.status_code == 200, grant.text
    user_code = grant.json()["user_code"]
    return c.post(
        "/oauth/device", data={"user_code": user_code, "identifier": _NS, "password": password}
    )


def test_the_authorize_form_and_the_device_grant_refuse_a_claim_code(world, monkeypatch) -> None:
    async def emit(event_type: str, **kw: Any) -> str:
        return ""

    monkeypatch.setattr(device.audit, "emit", emit)
    c = TestClient(oauth_app())
    assert _authorize(c, world["code"]).status_code == 401
    assert _device_sign_in(c, world["code"]).status_code == 401
    # The same forms take the password the claim set.
    assert _claim(_app(), world["code"], "correct horse").status_code == 200
    assert _authorize(c, "correct horse").status_code in (302, 303)
    assert _device_sign_in(c, "correct horse").status_code == 200
