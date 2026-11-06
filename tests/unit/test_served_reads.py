"""`served` events: the keys an agent read's answer returned, written by the op."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import served_reads
from server.observability import consumer as consumer_mod
from server.observability.consumer import SOURCE_SELF_REPORTED


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from server.memory.store import usage

    calls: list[dict[str, Any]] = []

    async def _rec(org: str, ns_keys: dict[str, list[str]], **kw: Any) -> int:
        calls.append({"org": org, "ns_keys": ns_keys, **kw})
        return 1

    monkeypatch.setattr(usage, "record_usage_for_keys", _rec)
    return calls


def _run(handler: Any, input: Any, consumer: str = "codex") -> Any:
    from server.memory.app import after_write

    async def _go() -> Any:
        consumer_mod.set_current_consumer(consumer, SOURCE_SELF_REPORTED if consumer else "")
        try:
            out = await handler(input)
            await after_write.drain()
            return out
        finally:
            consumer_mod.set_current_consumer("", "")

    return asyncio.run(_go())


def _hit(key: str, ns: str = "n") -> SimpleNamespace:
    return SimpleNamespace(memory_key=key, namespace=ns)


def _wrapped(keys_of: Any, out: Any) -> Any:
    async def handler(_input: Any) -> Any:
        return out

    return served_reads.records_served(keys_of)(handler)


def _search_input(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{"org": "o", "namespace": "n", "agent": "codex", "auto_touch": True, **kw}
    )


def test_an_uncredited_search_records_what_it_returned(written: list[dict[str, Any]]) -> None:
    out = SimpleNamespace(
        results=[_hit("a"), _hit("b", ns="m")], reinforcement="not_credited (no_closer)"
    )
    _run(_wrapped(served_reads.search_keys, out), _search_input())
    assert written == [
        {
            "org": "o",
            "ns_keys": {"n": ["a"], "m": ["b"]},
            "event_type": "served",
            "agent": "codex",
            "consumer": "codex",
        }
    ]


def test_a_credited_search_or_one_without_usage_records_no_served(
    written: list[dict[str, Any]],
) -> None:
    credited = SimpleNamespace(results=[_hit("a")], reinforcement="credited")
    _run(_wrapped(served_reads.search_keys, credited), _search_input())
    untouched = SimpleNamespace(results=[_hit("a")], reinforcement=None)
    _run(_wrapped(served_reads.search_keys, untouched), _search_input(auto_touch=False))
    assert written == []


def test_a_pack_records_the_rows_its_blocks_cite() -> None:
    """A transcript row the pack dropped, or an item past a section budget, is not cited."""
    blocks = [
        SimpleNamespace(citations=["memory:n/a", "memory:n/b#res-1", "guide:3"]),
        SimpleNamespace(citations=["decision:n/d1", "procedure:m/p1", "profile:role"]),
        SimpleNamespace(citations=["working_memory:scratch"]),
    ]
    keys = served_reads.pack_keys(SimpleNamespace(namespace="n"), SimpleNamespace(blocks=blocks))
    assert keys == [("n", "a"), ("n", "b"), ("n", "d1"), ("m", "p1")]


def test_search_query_records_the_memory_hits_it_kept() -> None:
    inp = SimpleNamespace(namespace="n")
    kept = SimpleNamespace(memory={"results": [{"memory_key": "a", "namespace": "n"}, {"x": 1}]})
    assert served_reads.search_query_keys(inp, kept) == [("n", "a")]
    credited = SimpleNamespace(
        memory={"reinforcement": "credited", "results": [{"memory_key": "a"}]}
    )
    assert served_reads.search_query_keys(inp, credited) == []


def test_ask_and_procedure_match_record_what_they_return() -> None:
    inp = SimpleNamespace(namespace="n")
    assert served_reads.ask_keys(inp, SimpleNamespace(results=[_hit("a")])) == [("n", "a")]
    match = SimpleNamespace(memory_key="p", namespace="")
    assert served_reads.procedure_keys(inp, SimpleNamespace(matches=[match])) == [("n", "p")]


@pytest.mark.parametrize(
    ("agent", "consumer"),
    [
        ("*", "loom-ui"),  # the dashboard
        ("*", "loom-assistant"),  # the assistant
        ("latency-bench", "codex"),  # a bench label on the agent
        ("codex", "eval-w8"),  # a bench label on the consumer
        ("*", ""),  # a sentinel agent with no named consumer: a probe
        ("-", ""),
    ],
)
def test_reads_that_are_not_an_agents_record_nothing(
    written: list[dict[str, Any]], agent: str, consumer: str
) -> None:
    out = SimpleNamespace(results=[_hit("a")])
    _run(_wrapped(served_reads.ask_keys, out), _search_input(agent=agent), consumer=consumer)
    assert written == []


def test_a_sentinel_agent_with_a_named_consumer_is_counted(written: list[dict[str, Any]]) -> None:
    out = SimpleNamespace(results=[_hit("a")])
    _run(_wrapped(served_reads.ask_keys, out), _search_input(agent="*"), consumer="codex")
    assert [(w["event_type"], w["agent"]) for w in written] == [("served", "*")]


def test_a_failing_key_read_never_fails_the_answer(written: list[dict[str, Any]]) -> None:
    def broken(_i: Any, _o: Any) -> list[tuple[str, str]]:
        raise RuntimeError("boom")

    out = SimpleNamespace(results=[])
    assert _run(_wrapped(broken, out), _search_input()) is out
    assert written == []


def test_the_five_agent_read_ops_record_served() -> None:
    """The ops that return memory to an agent; decision.precedents, memory.apply and the
    workers' searches call the handlers directly or not at all, and record none."""
    from server.operations.registry import OPERATIONS_BY_NAME

    for name in ("memory.search", "context.pack", "memory.ask", "procedure.match", "search.query"):
        assert getattr(OPERATIONS_BY_NAME[name].handler, "__wrapped__", None) is not None, name
    for name in ("decision.precedents", "memory.apply", "memory.get"):
        assert getattr(OPERATIONS_BY_NAME[name].handler, "__wrapped__", None) is None, name


def test_a_narrowed_reader_counts_its_own_row_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory.get's served figures read every live row of the key, where the events land;
    a token bounded to one user or session reads its own row's and learns nothing of
    another user's copy."""
    from server.identity.app import request_scope
    from server.memory.app import handlers

    row = SimpleNamespace(user_id="u1", session_id="s1")
    monkeypatch.setattr(request_scope, "request_narrowed", lambda: True)
    assert handlers.served_scope(row) == ("u1", "s1")
    monkeypatch.setattr(request_scope, "request_narrowed", lambda: False)
    assert handlers.served_scope(row) is None
