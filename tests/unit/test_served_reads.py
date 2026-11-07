"""Served reads: the keys an agent read's answer returned, recorded by the op."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import served_reads
from server.observability import consumer as consumer_mod
from server.observability.consumer import SOURCE_SELF_REPORTED


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from server.memory.store import served

    calls: list[dict[str, Any]] = []

    async def _rec(org: str, ns_keys: dict[str, list[str]], **kw: Any) -> int:
        calls.append({"org": org, "ns_keys": ns_keys, **kw})
        return 1

    monkeypatch.setattr(served, "record_served", _rec)
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
    return SimpleNamespace(memory_key=key, namespace=ns, parent_memory_key="")


def _wrapped(keys_of: Any, out: Any) -> Any:
    async def handler(_input: Any) -> Any:
        return out

    return served_reads.records_served(keys_of)(handler)


def _search_input(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "org": "o",
            "namespace": "n",
            "agent": "codex",
            "auto_touch": True,
            "reinforce": True,
            **kw,
        }
    )


@pytest.mark.parametrize("credit", ["credited", "not_credited (no_closer)"])
def test_a_search_records_what_it_returned_credited_or_not(
    written: list[dict[str, Any]], credit: str
) -> None:
    out = SimpleNamespace(results=[_hit("a"), _hit("b", ns="m")], reinforcement=credit)
    _run(_wrapped(served_reads.search_keys, out), _search_input())
    assert written == [
        {
            "org": "o",
            "ns_keys": {"n": ["a"], "m": ["b"]},
            "agent": "codex",
            "scope": WHOLE_NAMESPACE,
        }
    ]


def test_a_search_without_usage_records_nothing(written: list[dict[str, Any]]) -> None:
    untouched = SimpleNamespace(results=[_hit("a")], reinforcement=None)
    _run(_wrapped(served_reads.search_keys, untouched), _search_input(auto_touch=False))
    assert written == []


def test_memory_stack_records_its_stored_rows() -> None:
    entries = [
        SimpleNamespace(section="pinned", memory_key="pin1"),
        SimpleNamespace(section="recent_recall", memory_key="r1"),
        SimpleNamespace(section="profile", memory_key="role"),
        SimpleNamespace(section="knowledge", memory_key=""),
    ]
    out = SimpleNamespace(entries=entries)
    keys = served_reads.stack_keys(SimpleNamespace(namespace="n"), out)
    assert keys == [("n", "pin1"), ("n", "r1")]


def test_search_query_records_the_memory_hits_it_kept() -> None:
    inp = SimpleNamespace(namespace="n")
    kept = SimpleNamespace(memory={"results": [{"memory_key": "a", "namespace": "n"}, {"x": 1}]})
    assert served_reads.search_query_keys(inp, kept) == [("n", "a")]
    credited = SimpleNamespace(
        memory={"reinforcement": "credited", "results": [{"memory_key": "a"}]}
    )
    assert served_reads.search_query_keys(inp, credited) == [("n", "a")]


def test_ask_and_procedure_match_record_what_they_return() -> None:
    inp = SimpleNamespace(namespace="n", reinforce=True)
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
    assert [w["agent"] for w in written] == ["*"]


def test_a_failing_key_read_never_fails_the_answer(written: list[dict[str, Any]]) -> None:
    def broken(_i: Any, _o: Any) -> list[tuple[str, str]]:
        raise RuntimeError("boom")

    out = SimpleNamespace(results=[])
    assert _run(_wrapped(broken, out), _search_input()) is out
    assert written == []


def test_the_agent_read_ops_record_served() -> None:
    """The ops that recall memory for an agent: a search, a pack or stack, an ask, a
    procedure match, the pre-edit file read. A key lookup is the agent naming a key it
    knows, and decision.precedents, memory.apply and the workers search for their own use;
    none of them records."""
    from server.operations.registry import OPERATIONS_BY_NAME

    for name in (
        "memory.search",
        "context.pack",
        "memory.ask",
        "procedure.match",
        "search.query",
        "memory.stack",
        "memory.for_file",
    ):
        assert getattr(OPERATIONS_BY_NAME[name].handler, "__wrapped__", None) is not None, name
    # key lookups (the agent names the key it reads) and ops that search for their own use
    for name in (
        "memory.get",
        "memory.bulk_get",
        "memory.list",
        "decision.precedents",
        "memory.apply",
    ):
        assert getattr(OPERATIONS_BY_NAME[name].handler, "__wrapped__", None) is None, name


def test_an_evaluation_ask_records_nothing() -> None:
    inp = SimpleNamespace(namespace="n", reinforce=False)
    assert served_reads.ask_keys(inp, SimpleNamespace(results=[_hit("a")])) == []


# ---------------------------------------------------------------- the real wire models


def _real_hit(key: str, ns: str = "", parent: str = "") -> Any:
    from server.memory.api.models_search import _MemorySearchHit

    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt="x",
        distance=0.1,
        similarity=0.9,
        namespace=ns,
        parent_memory_key=parent,
    )


def test_each_extractor_reads_its_ops_real_models() -> None:
    """A renamed field would make `records_served` log and record nothing, with every
    stand-in test green; these run the extractors on the models the ops answer with."""
    from server.memory.api.models_ask import MemoryAskInput, MemoryAskOutput
    from server.memory.api.models_loom_search import SearchInput, SearchOutput
    from server.memory.api.models_procedure import ProcedureMatchInput, ProcedureMatchOutput
    from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput

    search_in = MemorySearchInput(org="o", namespace="n", query="q")
    # a chunk hit records its parent fact, the stored row
    chunk = _real_hit("chunk:s1:3", parent="long_fact")
    search_out = MemorySearchOutput(
        results=[_real_hit("a"), _real_hit("b", "m"), chunk], embedding_model="e"
    )
    assert served_reads.search_keys(search_in, search_out) == [
        ("n", "a"),
        ("m", "b"),
        ("n", "long_fact"),
    ]

    ask_in = MemoryAskInput(org="o", namespace="n", query="q")
    ask_out = MemoryAskOutput(answer="", results=[_real_hit("a")])
    assert served_reads.ask_keys(ask_in, ask_out) == [("n", "a")]

    match_in = ProcedureMatchInput(org="o", namespace="n", situation="s")
    match_out = ProcedureMatchOutput.model_validate(
        {
            "matches": [
                {
                    "name": "p",
                    "situation": "s",
                    "steps": [],
                    "similarity": 0.9,
                    "memory_key": "proc",
                    "namespace": "n",
                }
            ]
        }
    )
    assert served_reads.procedure_keys(match_in, match_out) == [("n", "proc")]

    from server.memory.api.models_lifecycle import MemoryStackInput, MemoryStackOutput

    stack_in = MemoryStackInput(org="o", namespace="n", agent="w")
    stack_out = MemoryStackOutput.model_validate(
        {
            "entries": [
                {"section": "pinned", "memory_key": "pin", "tokens": 1},
                {"section": "profile", "memory_key": "role", "tokens": 1},
            ],
            "tokens_used": 2,
            "tokens_budget": 10,
            "truncated": False,
            "section_counts": {},
        }
    )
    assert served_reads.stack_keys(stack_in, stack_out) == [("n", "pin")]

    query_in = SearchInput(org="o", namespace="n", query="q")
    query_out = SearchOutput.model_validate({"memory": search_out.model_dump(mode="json")})
    assert served_reads.search_query_keys(query_in, query_out) == [
        ("n", "a"),
        ("m", "b"),
        ("n", "long_fact"),
    ]


def test_a_narrowed_reader_records_under_its_own_scope(
    monkeypatch: pytest.MonkeyPatch, written: list[dict[str, Any]]
) -> None:
    """The funnel then picks the newest copy inside the reader's scope, the row it could
    have been served, never another user's."""
    from server.identity.app import request_scope
    from server.identity.domain.read_scope import ReadFilters

    own = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    monkeypatch.setattr(request_scope, "request_scope", lambda: own)
    out = SimpleNamespace(results=[_hit("a")])
    _run(_wrapped(served_reads.ask_keys, out), _search_input())
    assert [w["scope"] for w in written] == [own]


def test_an_op_whose_input_names_no_agent_records_its_named_consumer(
    written: list[dict[str, Any]],
) -> None:
    """memory.for_file's input has no `agent`: the named consumer is who read, and with
    none named the read records nothing, as a sentinel agent with no consumer does."""
    out = SimpleNamespace(_served_rows=[("n", "rule")])
    inp = SimpleNamespace(org="o", namespace="n")
    _run(_wrapped(served_reads.rendered_rows, out), inp, consumer="claude-code")
    assert [(w["ns_keys"], w["agent"]) for w in written] == [({"n": ["rule"]}, "claude-code")]
    written.clear()
    _run(_wrapped(served_reads.rendered_rows, out), inp, consumer="")
    assert written == []
