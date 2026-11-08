"""Reads that must see a write another replica made moments ago, and writes that must
land before their answer: namespace auto-registration, history-policy witnesses, the
blacklist refresh, search read-your-writes, the key lock and the search cursor."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from server.core import namespace_write_marks, request_tail
from server.core.ch import sequential_reads_active
from server.core.ch_writes import async_insert_settings
from server.identity.app import namespace_autoregister as autoreg
from server.identity.app import policy_witness
from server.identity.domain import policy_rules as R
from server.observability.events import AgentEvent
from server.settings import settings
from tests.unit import clickhouse_local
from tests.unit.cross_replica_fake import FakeCluster, in_request


class _Recorder:
    """A client that records inserts and whether each read was sequential."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], Any]] = []
        self.sequential: list[bool] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows, settings))

    async def query_one(self, sql: str, params: Any = None) -> Any:
        self.sequential.append(sequential_reads_active())
        return None


# ---- namespace auto-registration never overrides an explicit row ------------


def test_an_explicit_row_written_on_another_replica_is_never_overridden() -> None:
    cluster = FakeCluster(lag_s=3.6)
    a, b = cluster.replica("a"), cluster.replica("b")
    autoreg.forget_all()

    async def run() -> bool:
        with cluster.on(a):
            await a.insert(
                "loom.namespaces",
                [{"org": "o", "namespace": "secret", "owner_email": "admin", "updated_at": None}],
            )
        with cluster.on(b):
            return await autoreg.ensure_registered(
                "o", "secret", subject="writer", op_name="memory.set", ch=b
            )

    assert asyncio.run(run()) is False


def test_an_owned_auto_row_carries_the_auto_row_version() -> None:
    ch = _Recorder()
    autoreg.forget_all()
    assert asyncio.run(
        autoreg.ensure_registered("o", "fresh", subject="w", op_name="memory.set", ch=ch)
    )
    ((table, rows, _),) = ch.inserts
    assert table == "loom.namespaces" and rows[0]["updated_at"] == autoreg.AUTO_ROW_VERSION
    assert ch.sequential == [True, True]  # the registry read, the in-use read


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse not installed")
@pytest.mark.parametrize("owned_first", [True, False])
def test_an_owned_auto_row_beats_an_ownerless_one_whatever_lands_last(owned_first: bool) -> None:
    def row(owner: str, version: datetime) -> str:
        return (
            "INSERT INTO loom.namespaces (org, namespace, owner_email, updated_at) VALUES"
            f" ('o', 'n', '{owner}', toDateTime({int(version.timestamp())}, 'UTC'))"
        )

    inserts = [row("w", autoreg.AUTO_ROW_VERSION), row("", autoreg.OWNERLESS_ROW_VERSION)]
    setup = [clickhouse_local.ddl("namespaces"), *(inserts if owned_first else inserts[::-1])]
    rows = clickhouse_local.run(setup, "SELECT owner_email FROM loom.namespaces FINAL")
    assert rows == [{"owner_email": "w"}]


# ---- history-policy witnesses land before the answer ------------------------


def _rule(op: str) -> R.PolicyRule:
    return R.parse_rule(
        {
            "policy_id": "r",
            "effect": "forbid",
            "actions": ["memory.delete"],
            "condition": {
                "op": "count_within",
                "within": "1h",
                "operation": op,
                "cmp": "gt",
                "value": 3,
            },
        }
    )


def _event(op: str) -> AgentEvent:
    return AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        operation=op,
        request_id="r1",
        latency_ms=1,
        success=True,
        kind="request",
    )


@pytest.fixture()
def witness(monkeypatch: pytest.MonkeyPatch) -> tuple[_Recorder, list[AgentEvent]]:
    import server.core.ch as ch_mod
    from server.identity.app import history_policy
    from server.observability import events

    ch, queued = _Recorder(), []
    monkeypatch.setattr(settings, "policy_mode", "shadow")
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)

    async def _rules(org: str, ns: str, ch: Any = None) -> list[R.PolicyRule]:
        return [_rule("memory.get")]

    monkeypatch.setattr(history_policy, "rules_for", _rules)
    monkeypatch.setattr(events.get_emitter(), "emit", queued.append)
    return ch, queued


def _in_request(event: AgentEvent) -> bool:
    async def run() -> bool:
        with in_request() as tail:
            held = policy_witness.hold_event(event)
            await request_tail.settle(tail)
            return held

    return asyncio.run(run())


@pytest.mark.parametrize("switch", [True, False])
def test_a_rule_named_event_is_written_with_the_server_clock_before_the_answer(
    witness: tuple[_Recorder, list[AgentEvent]], monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    monkeypatch.setattr(settings, "ch_batched_inserts", switch)
    ch, queued = witness
    assert _in_request(_event("memory.get"))
    ((table, rows, sent),) = ch.inserts
    assert table == "loom.agent_events" and "ts" not in rows[0]
    # A waited async insert on the batched bounds whatever the switch says.
    assert sent == async_insert_settings() and queued == []


def test_an_event_no_rule_names_is_queued_as_usual(
    witness: tuple[_Recorder, list[AgentEvent]],
) -> None:
    ch, queued = witness
    assert _in_request(_event("memory.set"))
    assert ch.inserts == [] and [e.operation for e in queued] == ["memory.set"]


def test_rule_windows_end_at_the_servers_now() -> None:
    ctx = R.CallContext(
        org="o", namespace="n", agent="a", user_id="u", session_id="s", operation="memory.delete"
    )
    leaf = _rule("memory.get").condition
    compiled = R.compile_leaf(leaf, ctx)  # type: ignore[arg-type]
    assert compiled is not None and "ts <= now64(3)" in compiled.sql
    assert "p_now" not in compiled.params


def test_policy_rules_are_read_sequentially(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.app import history_policy
    from server.identity.store import policy as policy_repo

    seen: list[bool] = []

    async def _list(org: str, ns: str, **k: Any) -> list[dict[str, Any]]:
        seen.append(sequential_reads_active())
        return []

    monkeypatch.setattr(policy_repo, "list_rules", _list)
    history_policy.invalidate()
    asyncio.run(history_policy.rules_for("o", "n"))
    assert seen == [True]


# ---- blacklist refresh reads sequentially -----------------------------------


def test_the_blacklist_watermark_and_patterns_are_read_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import blacklist_guard as bg

    seen: list[tuple[str, bool]] = []

    async def _version(**k: Any) -> tuple[int, int]:
        seen.append(("version", sequential_reads_active()))
        return (1, 1)

    async def _patterns(**k: Any) -> list[dict[str, Any]]:
        seen.append(("patterns", sequential_reads_active()))
        return []

    monkeypatch.setattr(bg._repo, "version_watermark", _version)
    monkeypatch.setattr(bg._repo, "active_patterns", _patterns)
    monkeypatch.setattr(bg, "get_client", lambda: None)
    asyncio.run(bg._refresh_cache())
    assert seen == [("version", True), ("patterns", True)]


# ---- search reads its own writes across replicas ----------------------------


def test_a_memory_write_lands_its_mark_before_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    landed: list[tuple[str, str]] = []

    async def _land(org: str, namespace: str, **k: Any) -> None:
        landed.append((org, namespace))

    monkeypatch.setattr(namespace_write_marks, "land_key", _land)
    namespace_write_marks._reset_for_tests()

    async def run() -> None:
        with in_request() as tail:
            namespace_write_marks.observe_insert("loom.memory", [{"org": "o", "namespace": "n"}])
            namespace_write_marks.observe_insert("loom.memory", [{"org": "o", "namespace": "n"}])
            await request_tail.settle(tail)

    asyncio.run(run())
    namespace_write_marks._reset_for_tests()
    assert landed == [("o", "n")]


def test_a_search_after_another_replicas_write_reads_sequentially(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.search import consistency

    seen: list[str] = []

    class _Marks:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            seen.append(sql.split("FROM ")[1].split()[0])
            return {"engine": "KeeperMap"} if "system.tables" in sql else {"n": 1}

    monkeypatch.setattr(namespace_write_marks, "_default_client", _Marks)
    namespace_write_marks._reset_for_tests()
    got = asyncio.run(
        consistency._written_elsewhere(
            "o", "n", already_sequential=False, enabled=True, window_s=20
        )
    )
    assert got is True and seen == [namespace_write_marks.KEYS_TABLE]


# ---- key lock and cursor ----------------------------------------------------


def test_a_failing_keeper_refuses_the_conditional_write() -> None:
    from server.core.errors import BackendUnavailableError
    from server.memory.store.key_write_lock import KeyWriteLocks

    class _Keeper:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            return {"engine": "KeeperMap"}

        async def execute(self, sql: str, params: Any = None) -> None:
            raise RuntimeError("Code: 999. Coordination::Exception: Connection loss")

    async def run() -> None:
        async with KeyWriteLocks(_Keeper(), wait_s=0.5).hold("o", "n", "a", "u", "s", "k"):
            pass

    with pytest.raises(BackendUnavailableError):
        asyncio.run(run())


def test_no_cursor_is_issued_or_accepted_without_a_root_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app import search_cursor
    from server.settings import settings

    good = search_cursor.encode(kind="s", run_id="r", offset=1, params="p", indexed_commit="")
    monkeypatch.setattr(settings, "jwt_secret", "")
    assert search_cursor.enabled() is False
    with pytest.raises(search_cursor.CursorError):
        search_cursor.decode(good, kind="s", params="p")


def test_the_gate_reads_history_sequentially(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.identity.app import history_policy, policy_gate
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.settings import settings

    seen: list[bool] = []

    async def _rules(org: str, ns: str, ch: Any = None) -> list[R.PolicyRule]:
        return [_rule("memory.get")]

    async def _evaluate(rules: Any, ctx: Any, ch: Any, mode: str) -> R.PolicyDecision:
        seen.append(sequential_reads_active())
        return R.PolicyDecision(decision="allow", determining_rules=[], errors=[], verdicts=[])

    monkeypatch.setattr(settings, "policy_mode", "shadow")
    monkeypatch.setattr(history_policy, "rules_for", _rules)
    monkeypatch.setattr(history_policy, "evaluate", _evaluate)
    identity = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"}
    op = OPERATIONS_BY_NAME["memory.delete"]
    asyncio.run(policy_gate.gate(op, identity, {}, ch=object()))
    assert seen == [True]


def test_deferred_steps_finish_before_the_response_starts() -> None:
    order: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        async def step() -> None:
            order.append("step")

        assert request_tail.defer(step)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message: dict[str, Any]) -> None:
        order.append(message["type"])

    middleware = request_tail.RequestTailMiddleware(app)
    asyncio.run(middleware({"type": "http"}, None, send))
    assert order == ["step", "http.response.start", "http.response.body"]
    assert request_tail.defer(lambda: asyncio.sleep(0)) is False  # outside a request


def test_a_deferred_step_queries_under_the_tag_of_the_request_that_deferred_it() -> None:
    from server.core.ch import current_query_tag, set_query_tag

    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        async def route() -> None:  # the op runs in a context of its own
            set_query_tag("memory.set req-1")

            async def step() -> None:
                seen.append(current_query_tag())

            assert request_tail.defer(step)

        await asyncio.create_task(route())
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message: dict[str, Any]) -> None:
        pass

    asyncio.run(request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send))
    assert seen == ["memory.set req-1"]
