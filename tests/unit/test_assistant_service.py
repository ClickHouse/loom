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
    # The instructions and the tools (the assistant toolset) are the definition: a
    # change to either needs a new version in agent.yaml, and its digest here.
    defn = service.current_definition()
    digest = hashlib.sha256(json.dumps([defn.instructions, list(defn.tools)]).encode()).hexdigest()[
        :16
    ]
    assert {defn.version: digest} == {1: DEFINITION_DIGEST}


DEFINITION_DIGEST = "1b4d1aaaa44d1345"


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

    def handle(self, request: httpx.Request) -> httpx.Response:
        call = f"{request.method} {request.url.path}"
        self.calls.append(call)
        if call == self.fail_on:
            return httpx.Response(self.fail_status, json={"error": "boom"})
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
                return httpx.Response(401, json={"message": "Incorrect password."})
            return httpx.Response(200, json={"token": "jwt"})
        if call == "POST /api/auth/register":
            self.users[body["email"]] = body["password"]
            return httpx.Response(200, json={"message": "ok"})
        if call == "GET /api/api-keys":
            return httpx.Response(200, json={"keys": self.keys})
        if call == "POST /api/api-keys":
            key = {"id": f"k{len(self.keys)}", "name": body["name"], "key": f"sk-{len(self.keys)}"}
            self.keys.append(key)
            return httpx.Response(201, json=key)
        if request.method == "DELETE" and request.url.path.startswith("/api/api-keys/"):
            self.keys = [k for k in self.keys if k["id"] != request.url.path.rsplit("/", 1)[1]]
            return httpx.Response(204)
        if call == "GET /api/agents":
            return httpx.Response(200, json={"object": "list", "data": self.agents})
        if call == "POST /api/agents":
            agent = {"id": f"agent_{len(self.agents)}", "createdAt": str(len(self.agents)), **body}
            self.agents.append(agent)
            return httpx.Response(201, json=agent)
        if request.method == "PATCH" and request.url.path.startswith("/api/agents/"):
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1], **body})
        if request.method == "DELETE" and request.url.path.startswith("/api/agents/"):
            agent_id = request.url.path.rsplit("/", 1)[1]
            self.agents = [a for a in self.agents if a["id"] != agent_id]
            return httpx.Response(204)
        return httpx.Response(404)


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
    assert asyncio.run(service.ensure_service()) is True
    assert row.row is not None and row.row.agent_id == "agent_0"
    assert row.row.email == settings.assistant_service_email
    assert row.row.definition_version == service.current_definition().version
    assert len(lc.users) == 1 and len(lc.keys) == 1 and len(lc.agents) == 1
    # It registered only after LibreChat said the email does not exist.
    assert lc.calls[:3] == [
        "POST /api/auth/login",
        "POST /api/auth/register",
        "POST /api/auth/login",
    ]


def test_a_boot_over_a_set_up_service_calls_nothing(setup: tuple[_Row, _LibreChat]) -> None:
    row, lc = setup
    asyncio.run(service.ensure_service())
    lc.calls.clear()
    writes = row.writes
    asyncio.run(service.ensure_service())
    assert lc.calls == [] and row.writes == writes


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


def test_the_oldest_agent_of_the_name_is_kept_and_the_rest_deleted(
    setup: tuple[_Row, _LibreChat],
) -> None:
    row, lc = setup
    lc.agents = [
        {"id": "a_new", "name": "Loom assistant", "createdAt": "2"},
        {"id": "a_old", "name": "Loom assistant", "createdAt": "1"},
        {"id": "a_other", "name": "Someone else", "createdAt": "0"},
    ]
    asyncio.run(service.ensure_service())
    assert row.row is not None and row.row.agent_id == "a_old"
    assert [a["id"] for a in lc.agents] == ["a_old", "a_other"]


def test_a_row_that_does_not_open_is_refused_as_loom_words_it(
    setup: tuple[_Row, _LibreChat], monkeypatch: pytest.MonkeyPatch
) -> None:
    row, _ = setup
    row.row = store.ServiceRow(email="e@x", sealed="sealed-under-another-secret", version=1)
    monkeypatch.setattr(service, "_opened", lambda _: None)
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
    assert waits == [90.0, 90.0] and outcomes == []
