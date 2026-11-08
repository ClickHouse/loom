"""The policy gate: off / shadow / enforce, fail-closed for
writes, fail-open for reads, exemptions, the ledger row, the 403 shape."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from server.identity.app import history_policy as P
from server.identity.app import policy_gate as G
from server.identity.store import policy as policy_repo
from server.operations.base import Operation, Role


class _In(BaseModel):
    org: str = "acme"
    namespace: str = "team"


async def _noop(i: BaseModel) -> BaseModel:  # pragma: no cover
    return i


WRITE_OP = Operation(
    name="memory.set",
    input_model=_In,
    output_model=_In,
    handler=_noop,
    idempotent=False,
    required_role=Role.WRITER,
)
READ_OP = Operation(
    name="memory.get",
    input_model=_In,
    output_model=_In,
    handler=_noop,
    idempotent=True,
    required_role=Role.VIEWER,
)
POLICY_OP = Operation(
    name="policy.set",
    input_model=_In,
    output_model=_In,
    handler=_noop,
    idempotent=True,
    required_role=Role.ADMIN,
)

IDENTITY = {"org": "acme", "namespace": "team", "agent": "bot", "user_id": "u", "session_id": "s"}


class FakeCH:
    def __init__(self, n: int | None = 0, fail: bool = False):
        self.n, self.fail, self.calls = n, fail, 0

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("clickhouse unreachable")
        return [{"n": self.n}]


@pytest.fixture
def rules(monkeypatch):
    """One `require formerly memory.get within 1h` rule on memory.*; ledger captured."""
    rows: list[dict[str, Any]] = []

    async def fake_list(org: str, ns: str, *, include_disabled: bool = True, ch: Any = None):
        return [
            {
                "policy_id": "read-before-write",
                "effect": "require",
                "actions": ["memory.set"],
                "condition": '{"op":"formerly","within":"1h","operation":["memory.get"]}',
                "description": "",
                "enabled": True,
            }
        ]

    async def fake_insert(row: dict[str, Any], *, ch: Any = None) -> None:
        rows.append(row)

    monkeypatch.setattr(policy_repo, "list_rules", fake_list)
    monkeypatch.setattr(policy_repo, "insert_decision", fake_insert)
    P.invalidate()
    yield rows
    P.invalidate()


def _mode(monkeypatch, mode: str) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "policy_mode", mode)


@pytest.mark.asyncio
async def test_off_touches_nothing(monkeypatch, rules):
    _mode(monkeypatch, "off")
    ch = FakeCH(n=0)
    assert await G.gate(WRITE_OP, IDENTITY, {}, ch=ch) is None
    assert ch.calls == 0 and rules == []


@pytest.mark.asyncio
async def test_shadow_records_the_would_be_denial_and_never_blocks(monkeypatch, rules):
    _mode(monkeypatch, "shadow")
    d = await G.gate(WRITE_OP, IDENTITY, {"memory_key": "k"}, request_id="req-1", ch=FakeCH(n=0))
    assert d is not None and d.decision == "deny"
    await _drain()
    assert len(rules) == 1
    row = rows = rules[0]
    assert row["mode"] == "shadow" and row["decision"] == "deny" and row["enforced"] is False
    assert row["determining_rules"] == ["read-before-write"] and row["request_id"] == "req-1"
    assert row["operation"] == "memory.set" and row["agent"] == "bot"
    assert rows["errors"] == []


@pytest.mark.asyncio
async def test_enforce_raises_a_structured_403(monkeypatch, rules):
    _mode(monkeypatch, "enforce")
    with pytest.raises(G.PolicyForbiddenError) as ei:
        await G.gate(WRITE_OP, IDENTITY, {"memory_key": "k"}, ch=FakeCH(n=0))
    detail = ei.value.to_detail()
    assert ei.value.status_code == 403 and detail["code"] == "POLICY_FORBIDDEN"
    assert detail["decision"] == "deny" and detail["determining_rules"] == ["read-before-write"]
    assert detail["errors"] == []  # denied BY POLICY, not by a broken check
    assert detail["rules"][0]["leaves"][0]["value"] == 0
    await _drain()
    assert rules[0]["enforced"] is True


@pytest.mark.asyncio
async def test_enforce_allows_when_the_history_holds(monkeypatch, rules):
    _mode(monkeypatch, "enforce")
    d = await G.gate(WRITE_OP, IDENTITY, {"memory_key": "k"}, ch=FakeCH(n=2))
    assert d is not None and d.decision == "allow"
    await _drain()
    assert rules[0]["decision"] == "allow" and rules[0]["enforced"] is False


@pytest.mark.asyncio
async def test_enforce_fails_closed_for_writes_when_history_cannot_be_read(monkeypatch, rules):
    _mode(monkeypatch, "enforce")
    with pytest.raises(G.PolicyForbiddenError) as ei:
        await G.gate(WRITE_OP, IDENTITY, {}, ch=FakeCH(fail=True))
    detail = ei.value.to_detail()
    assert detail["determining_rules"] == []  # nobody denied it —
    assert detail["errors"] == ["formerly: RuntimeError"]  # — the check broke,
    assert "unreachable" not in str(detail)  # and the store's own text stays in the log
    assert "fail closed" in ei.value.message


@pytest.mark.asyncio
async def test_a_gate_that_cannot_read_its_rules_names_the_error_kind_only(monkeypatch):
    async def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("SELECT * FROM loom.policies WHERE secret = 1")

    monkeypatch.setattr(policy_repo, "list_rules", broken)
    P.invalidate()
    _mode(monkeypatch, "enforce")
    try:
        with pytest.raises(G.PolicyForbiddenError) as ei:
            await G.gate(WRITE_OP, IDENTITY, {}, ch=FakeCH(n=2))
    finally:
        P.invalidate()
    detail = ei.value.to_detail()
    assert detail["errors"] == ["policy gate: RuntimeError"]
    assert "secret" not in str(detail)


@pytest.mark.asyncio
async def test_enforce_fails_open_for_reads_but_reports(monkeypatch):
    from server.identity.store import policy as repo

    async def fake_list(org: str, ns: str, *, include_disabled: bool = True, ch: Any = None):
        return [
            {
                "policy_id": "r",
                "effect": "require",
                "actions": ["memory.get"],
                "condition": '{"op":"formerly","within":"1h","operation":["memory.search"]}',
                "description": "",
                "enabled": True,
            }
        ]

    recorded: list[dict[str, Any]] = []

    async def fake_insert(row: dict[str, Any], *, ch: Any = None) -> None:
        recorded.append(row)

    monkeypatch.setattr(repo, "list_rules", fake_list)
    monkeypatch.setattr(repo, "insert_decision", fake_insert)
    P.invalidate()
    _mode(monkeypatch, "enforce")
    d = await G.gate(READ_OP, IDENTITY, {}, ch=FakeCH(fail=True))
    assert d is not None and d.decision == "allow" and d.errors
    await _drain()
    assert recorded and recorded[0]["errors"]
    P.invalidate()


@pytest.mark.asyncio
async def test_policy_ops_and_incomplete_identity_are_exempt(monkeypatch, rules):
    _mode(monkeypatch, "enforce")
    ch = FakeCH(n=0)
    assert await G.gate(POLICY_OP, IDENTITY, {}, ch=ch) is None
    assert await G.gate(WRITE_OP, {"org": "-", "namespace": "-"}, {}, ch=ch) is None
    assert await G.gate(WRITE_OP, {"org": "acme", "namespace": "-"}, {}, ch=ch) is None
    assert ch.calls == 0


@pytest.mark.asyncio
async def test_no_applicable_rule_means_no_query_and_no_ledger_row(monkeypatch, rules):
    _mode(monkeypatch, "enforce")
    ch = FakeCH(n=0)
    assert await G.gate(READ_OP, IDENTITY, {}, ch=ch) is None  # rule is on memory.set only
    assert ch.calls == 0
    await _drain()
    assert rules == []


@pytest.mark.asyncio
async def test_a_pending_ledger_write_survives_garbage_collection(monkeypatch, rules):
    """The loop keeps only weak references to tasks: an insert suspended on
    I/O that nothing else references is collected unless the gate holds it."""
    import asyncio
    import gc
    import weakref

    async def slow_insert(row: dict[str, Any], *, ch: Any = None) -> None:
        # a future no one outside the task references, like a socket wait
        await asyncio.get_running_loop().create_future()

    monkeypatch.setattr(policy_repo, "insert_decision", slow_insert)
    _mode(monkeypatch, "shadow")
    before = asyncio.all_tasks()
    await G.gate(WRITE_OP, IDENTITY, {"memory_key": "k"}, ch=FakeCH(n=0))
    [task] = asyncio.all_tasks() - before
    await asyncio.sleep(0)  # the write starts and suspends
    ref = weakref.ref(task)
    del task, before
    gc.collect()
    alive = ref()
    assert alive is not None and not alive.done()
    alive.cancel()
    await asyncio.gather(alive, return_exceptions=True)
    await asyncio.sleep(0)  # the done-callback runs
    assert alive not in G._LEDGER_TASKS


def test_write_shaped_is_role_based():
    assert G.is_write_shaped(WRITE_OP) and G.is_write_shaped(POLICY_OP)
    assert not G.is_write_shaped(READ_OP)


async def _drain() -> None:
    """Let the fire-and-forget ledger task run."""
    import asyncio

    await asyncio.sleep(0)
    await asyncio.sleep(0)
