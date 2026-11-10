"""The assistant's LibreChat service: the agent definition, when a boot writes it, and
the setup step against a LibreChat that answers as v0.8.8 does. CH-free."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from typing import Any

import httpx
import pytest

from server.assistant.app import service
from server.assistant.domain import definition
from server.assistant.store import service as store
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


def _definition(**overrides: Any) -> definition.Definition:
    raw = {
        "name": "Loom assistant",
        "version": 2,
        "provider": "anthropic",
        "model": "m-file",
        "instructions": "cite",
    }
    return definition.parse({**raw, **overrides}, ["loom_memory_get", "loom_insights_query"])


def test_tools_are_the_toolsets_mcp_names_on_the_loom_server() -> None:
    assert _definition().tools == (
        "loom_insights_query_mcp_loom",
        "loom_memory_get_mcp_loom",
    )


def test_a_set_model_replaces_the_files() -> None:
    raw = {
        "name": "n",
        "version": 1,
        "provider": "anthropic",
        "model": "m-file",
        "instructions": "",
    }
    assert definition.parse(raw, [], "m-set").model == "m-set"
    assert definition.parse(raw, []).model == "m-file"


@pytest.mark.parametrize(
    ("stored_version", "stored_model", "written"),
    [
        (0, "", True),  # nothing stored
        (1, "m-file", True),  # an older build's
        (2, "m-other", True),  # this version, another model
        (2, "m-file", False),  # already this build's
        (3, "m-other", False),  # a newer build's: N-1 never overwrites N
    ],
)
def test_when_a_boot_writes_the_definition(
    stored_version: int, stored_model: str, written: bool
) -> None:
    assert definition.needs_write(stored_version, stored_model, _definition()) is written


def test_a_version_names_one_definition() -> None:
    # Everything LibreChat is sent but the model (a changed model is written at the same
    # version): a change to any of it needs a new version in agent.yaml, and its digest.
    defn = service.current_definition()
    body = {k: v for k, v in defn.agent_body().items() if k != "model"}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    assert {defn.version: digest} == {3: DEFINITION_DIGEST}


DEFINITION_DIGEST = "199efb1be3595057"


class _Row:
    """An in-memory loom.assistant_service."""

    def __init__(self) -> None:
        self.row: store.ServiceRow | None = None
        self.writes = 0

    async def read(self, **_: Any) -> store.ServiceRow | None:
        return self.row

    async def write(self, row: store.ServiceRow, **_: Any) -> None:
        assert self.row is None or row.version > self.row.version
        self.row, self.writes = row, self.writes + 1


class _LibreChat:
    """LibreChat v0.8.8's answers to the calls the setup makes."""

    def __init__(self) -> None:
        self.users: dict[str, str] = {}
        self.keys: list[dict[str, str]] = []
        self.agents: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.fail_on = ""
        self.fail_status = 500
        self.made = 0  # ids LibreChat has handed out, so none repeats

    def forget_everything(self) -> None:
        """LibreChat's MongoDB replaced: no user, key or agent."""
        self.users, self.keys, self.agents = {}, [], []

    def _new_id(self) -> int:
        self.made += 1
        return self.made

    def handle(self, request: httpx.Request) -> httpx.Response:
        call = f"{request.method} {request.url.path}"
        self.calls.append(call)
        if call == self.fail_on:
            return httpx.Response(self.fail_status, json={"error": "boom"})
        if request.url.path.startswith("/api/agents/v1/models/"):
            # The agents API, authenticated by an API key: no browser check.
            bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
            if bearer not in {k["key"] for k in self.keys}:
                return httpx.Response(401, json={"message": "Invalid API key"})
            agent_id = request.url.path.rsplit("/", 1)[1]
            if agent_id not in {a["id"] for a in self.agents}:
                return httpx.Response(404, json={"error": {"code": "model_not_found"}})
            return httpx.Response(200, json={"id": agent_id, "object": "model"})
        if "Chrome/" not in request.headers.get("user-agent", "") and call not in (
            "POST /api/auth/login",
            "POST /api/auth/register",
        ):
            # LibreChat's uaParser: a signed-in route refuses a client that is no browser.
            return httpx.Response(200, text='event: error\ndata: {"message":"Illegal request"}')
        body = json.loads(request.content) if request.content else {}
        if call == "POST /api/auth/login":
            if body["email"] not in self.users:
                return httpx.Response(404, json={"message": "Email does not exist."})
            if self.users[body["email"]] != body["password"]:
                # v0.8.8 answers a wrong password as it answers an unknown email.
                return httpx.Response(404, json={"message": "Incorrect password."})
            return httpx.Response(200, json={"token": "jwt"})
        if call == "POST /api/auth/register":
            self.users[body["email"]] = body["password"]
            return httpx.Response(200, json={"message": "ok"})
        if call == "GET /api/api-keys":
            return httpx.Response(200, json={"keys": self.keys})
        if call == "POST /api/api-keys":
            n = self._new_id()
            key = {"id": f"k{n}", "name": body["name"], "key": f"sk-{n}"}
            self.keys.append(key)
            return httpx.Response(201, json=key)
        if request.method == "DELETE" and request.url.path.startswith("/api/api-keys/"):
            self.keys = [k for k in self.keys if k["id"] != request.url.path.rsplit("/", 1)[1]]
            return httpx.Response(204)
        if call == "GET /api/agents":
            return httpx.Response(200, json={"object": "list", "data": self.agents})
        if call == "POST /api/agents":
            n = self._new_id()
            agent = {"id": f"agent_{n}", "createdAt": f"{n:04d}", **body}
            self.agents.append(agent)
            return httpx.Response(201, json=agent)
        if request.method == "PATCH" and request.url.path.startswith("/api/agents/"):
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1], **body})
        if request.method == "DELETE" and request.url.path.startswith("/api/agents/"):
            agent_id = request.url.path.rsplit("/", 1)[1]
            self.agents = [a for a in self.agents if a["id"] != agent_id]
            return httpx.Response(204)
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # Each test starts as a process that has not tried the setup.
    monkeypatch.setattr(service, "_ready", None)
    monkeypatch.setattr(service, "_problem", "")


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[_Row, _LibreChat]:
    row, lc = _Row(), _LibreChat()
    real_client = httpx.AsyncClient

    @contextlib.asynccontextmanager
    async def no_lease(*_: Any, **__: Any) -> Any:
        yield None

    monkeypatch.setattr(settings, "assistant_librechat_url", "http://librechat:3080")
    monkeypatch.setattr(service.store, "read", row.read)
    monkeypatch.setattr(service.store, "write", row.write)
    monkeypatch.setattr(service, "lease", no_lease)
    monkeypatch.setattr(
        service.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(lc.handle), **kw),
    )
    return row, lc


def test_a_first_boot_registers_the_user_and_seeds_the_agent(
    setup: tuple[_Row, _LibreChat],
) -> None:
    row, lc = setup
    asyncio.run(service.ensure_service())
    assert row.row is not None and row.row.agent_id == "agent_2"
    assert row.row.email == settings.assistant_service_email
    assert row.row.definition_version == service.current_definition().version
    assert len(lc.users) == 1 and len(lc.keys) == 1 and len(lc.agents) == 1
    # It registered only after LibreChat said the email does not exist.
    assert lc.calls[:3] == [
        "POST /api/auth/login",
        "POST /api/auth/register",
        "POST /api/auth/login",
    ]


def test_a_boot_over_a_set_up_service_checks_it_and_writes_nothing(
    setup: tuple[_Row, _LibreChat],
) -> None:
    row, lc = setup
    asyncio.run(service.ensure_service())
    lc.calls.clear()
    writes = row.writes
    asyncio.run(service.ensure_service())
    # One call with the stored key, no sign-in.
    assert lc.calls == [f"GET /api/agents/v1/models/{row.row.agent_id}"]  # type: ignore[union-attr]
    assert row.writes == writes


def test_an_agent_deleted_in_librechat_is_made_again(setup: tuple[_Row, _LibreChat]) -> None:
    row, lc = setup
    asyncio.run(service.ensure_service())
    lc.agents.clear()
    asyncio.run(service.ensure_service())
    assert row.row is not None and [a["id"] for a in lc.agents] == [row.row.agent_id]
    assert row.row.definition_version == service.current_definition().version
    assert len(lc.keys) == 1


def test_a_librechat_that_lost_its_database_is_set_up_again(
    setup: tuple[_Row, _LibreChat],
) -> None:
    row, lc = setup
    asyncio.run(service.ensure_service())
    lc.forget_everything()
    asyncio.run(service.ensure_service())
    assert row.row is not None and len(lc.users) == 1
    assert [a["id"] for a in lc.agents] == [row.row.agent_id]
    stored = service.opened(row.row.sealed)
    assert stored is not None and [k["key"] for k in lc.keys] == [stored["api_key"]]


def test_a_key_made_but_not_stored_is_replaced_by_the_next_boot(
    setup: tuple[_Row, _LibreChat], monkeypatch: pytest.MonkeyPatch
) -> None:
    row, lc = setup
    real_write = row.write

    async def write_then_fail(new: store.ServiceRow, **kw: Any) -> None:
        if row.row is not None:  # the row exists: this write would store the key
            raise RuntimeError("ClickHouse went away")
        await real_write(new, **kw)

    monkeypatch.setattr(service.store, "write", write_then_fail)
    with pytest.raises(RuntimeError):
        asyncio.run(service.ensure_service())
    assert len(lc.keys) == 1  # LibreChat made a key the row does not hold
    monkeypatch.setattr(service.store, "write", real_write)
    asyncio.run(service.ensure_service())
    stored = service.opened(row.row.sealed)  # type: ignore[union-attr]
    assert stored is not None and [k["key"] for k in lc.keys] == [stored["api_key"]]


def test_a_boot_cut_short_is_finished_by_the_next(setup: tuple[_Row, _LibreChat]) -> None:
    row, lc = setup
    lc.fail_on = "POST /api/agents"
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(service.ensure_service())
    assert row.row is not None and row.row.agent_id == ""
    lc.fail_on = ""
    lc.calls.clear()
    asyncio.run(service.ensure_service())
    # The key it stored stands: no second key, one agent.
    assert len(lc.keys) == 1 and len(lc.agents) == 1
    assert "POST /api/api-keys" not in lc.calls


def test_a_librechat_that_cannot_answer_the_check_is_a_problem(
    setup: tuple[_Row, _LibreChat],
) -> None:
    row, lc = setup
    asyncio.run(service.ensure_service())
    assert row.row is not None
    lc.fail_on, lc.fail_status = f"GET /api/agents/v1/models/{row.row.agent_id}", 503
    assert asyncio.run(service._setup_once()) is False
    _, problem = service.assistant_state()
    assert "LibreChat answered 503 for the assistant's agent" in problem


def test_the_oldest_agent_of_the_name_is_kept_and_the_rest_deleted(
    setup: tuple[_Row, _LibreChat],
) -> None:
    row, lc = setup
    lc.agents = [
        {"id": "a_new", "name": "Loom assistant", "createdAt": "0002"},
        {"id": "a_old", "name": "Loom assistant", "createdAt": "0001"},
        {"id": "a_other", "name": "Someone else", "createdAt": "0000"},
    ]
    asyncio.run(service.ensure_service())
    assert row.row is not None and row.row.agent_id == "a_old"
    assert [a["id"] for a in lc.agents] == ["a_old", "a_other"]


def test_a_row_that_does_not_open_is_refused_as_loom_words_it(
    setup: tuple[_Row, _LibreChat], monkeypatch: pytest.MonkeyPatch
) -> None:
    row, _ = setup
    row.row = store.ServiceRow(email="e@x", sealed="sealed-under-another-secret", version=1)
    monkeypatch.setattr(service, "opened", lambda _: None)
    assert asyncio.run(service._setup_once()) is False
    ready, problem = service.assistant_state()
    assert ready is False and "does not open with LOOM_JWT_SECRET" in problem


def test_a_limited_sign_in_is_not_called_a_foreign_database(
    setup: tuple[_Row, _LibreChat],
) -> None:
    _, lc = setup
    lc.fail_on, lc.fail_status = "POST /api/auth/login", 429
    assert asyncio.run(service._setup_once()) is False
    _, problem = service.assistant_state()
    assert "limited the service user's sign-ins" in problem
    assert "empty LibreChat's database" not in problem


def test_a_failure_shows_its_kind_and_logs_its_text(
    setup: tuple[_Row, _LibreChat], caplog: pytest.LogCaptureFixture
) -> None:
    # /v1/health answers without a token: LibreChat's address stays in the log.
    _, lc = setup
    lc.fail_on = "GET /api/api-keys"
    assert asyncio.run(service._setup_once()) is False
    ready, problem = service.assistant_state()
    assert ready is False and problem.endswith("HTTPStatusError")
    assert "librechat:3080" not in problem
    assert any("librechat:3080" in r.message for r in caplog.records)


def test_no_librechat_means_no_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "assistant_librechat_url", "")
    assert service.start_setup_loop() is None


def test_a_set_model_is_the_definitions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "assistant_model", "m-set")
    assert service.current_definition().model == "m-set"


def test_a_failed_setup_is_tried_again_after_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcomes = [False, False, True]
    waits: list[float] = []

    async def _once() -> bool:
        return outcomes.pop(0)

    async def _sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(settings, "assistant_librechat_url", "http://librechat:3080")
    monkeypatch.setattr(settings, "assistant_setup_retry_s", 90.0)
    monkeypatch.setattr(service, "_setup_once", _once)
    monkeypatch.setattr(service.asyncio, "sleep", _sleep)

    async def _run() -> None:
        task = service.start_setup_loop()
        assert task is not None
        await task

    asyncio.run(_run())
    # The wait doubles after each failure.
    assert waits == [90.0, 180.0] and outcomes == []


def test_a_key_kept_across_a_new_librechat_database_is_replaced(
    setup: tuple[_Row, _LibreChat],
) -> None:
    # A boot stored the key and stopped before the agent; then LibreChat lost its database.
    row, lc = setup
    lc.fail_on = "POST /api/agents"
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(service.ensure_service())
    lc.fail_on = ""
    lc.forget_everything()
    asyncio.run(service.ensure_service())
    stored = service.opened(row.row.sealed)  # type: ignore[union-attr]
    # The key the row holds is the new user's: the one LibreChat knows.
    assert stored is not None and [k["key"] for k in lc.keys] == [stored["api_key"]]


def test_a_librechat_that_cannot_sign_in_is_not_called_a_foreign_database(
    setup: tuple[_Row, _LibreChat],
) -> None:
    _, lc = setup
    lc.fail_on, lc.fail_status = "POST /api/auth/login", 500
    assert asyncio.run(service._setup_once()) is False
    _, problem = service.assistant_state()
    assert "unavailable" in problem and "TRUNCATE" not in problem


def test_a_wrong_password_is_called_a_foreign_database(setup: tuple[_Row, _LibreChat]) -> None:
    row, lc = setup
    asyncio.run(service.ensure_service())
    lc.users = {email: "another password" for email in lc.users}
    lc.keys.clear()  # the stored key is gone too, so the setup signs in
    assert asyncio.run(service._setup_once()) is False
    _, problem = service.assistant_state()
    assert "TRUNCATE TABLE loom.assistant_service" in problem


@pytest.mark.parametrize(("ok", "shown"), [(None, False), (True, False), (False, True)])
def test_health_names_a_failed_setup_only(ok: bool | None, shown: bool) -> None:
    from server.observability.problems import HealthState, _assistant_problems

    problems = _assistant_problems(settings, HealthState(assistant_ok=ok, assistant_error="x"))
    assert [p.code for p in problems] == (["assistant_service_failing"] if shown else [])
    if shown:
        assert problems[0].severity == "warn" and "TRUNCATE" in problems[0].fix


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_service_row_reads_back_as_written() -> None:
    row = store.ServiceRow(
        email="loom-assistant@loom.invalid",
        sealed="sealed-value",
        agent_id="agent_1",
        definition_version=3,
        definition_model="claude-haiku-4-5",
        version=7,
    )

    class _LocalCH:
        """loom.assistant_service on clickhouse-local: what the store inserts, then reads."""

        def __init__(self) -> None:
            self.inserted: list[dict[str, Any]] = []

        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            assert table == "loom.assistant_service"
            self.inserted.extend(rows)

        async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
            cols = sorted(self.inserted[0])
            values = ", ".join(
                "(" + ", ".join(repr(r[c]) for c in cols) + ")" for r in self.inserted
            )
            setup = [
                ddl("assistant_service"),
                f"INSERT INTO loom.assistant_service ({', '.join(cols)}) VALUES {values}",
            ]
            rows = run(setup, sql, params)
            return rows[0] if rows else None

    ch = _LocalCH()
    asyncio.run(store.write(row, ch=ch))  # type: ignore[arg-type]
    assert asyncio.run(store.read(ch=ch)) == row  # type: ignore[arg-type]
