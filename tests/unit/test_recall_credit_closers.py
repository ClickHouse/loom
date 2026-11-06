"""A recall credit is written only for a reader that closes the
loop — a named consumer inside a run or with a recent closer — and the bench
labels (`eval-*`, `latency-bench`, `f1-drive`) never credit. The agent
sentinels `*` / `-` are a scope, not a label,
once a consumer is named — the SDK's default cross-agent read under
`Agent(consumer=...)` is credited — and stay a measurement read with no
consumer behind them; every credit carries the request's `consumer` on the
usage ledger. The response discloses the decision as
`reinforcement`; a refused read writes nothing; an evaluation read's payload
is byte-identical."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import recall_credit as rc
from server.observability import consumer as consumer_mod
from server.observability.consumer import (
    SOURCE_CLAIMED,
    SOURCE_CLASS,
    SOURCE_SELF_REPORTED,
)


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    rc._reset_for_tests()
    yield
    rc._reset_for_tests()


# ---------------------------------------------------------------- labels and consumers


@pytest.mark.parametrize("label", ["*", "-", "eval-r6g", "eval-w8", "latency-bench", "f1-drive"])
def test_measurement_labels(label: str) -> None:
    # standing alone, none of these names a reader that can close a loop
    assert rc.is_measurement_label(label)


@pytest.mark.parametrize("label", ["eval-r6g", "eval-w8", "latency-bench", "f1-drive"])
def test_bench_labels_are_explicit(label: str) -> None:
    assert rc.is_bench_label(label) and not rc.is_agent_sentinel(label)


@pytest.mark.parametrize("label", ["*", "-"])
def test_agent_sentinels_are_not_bench_labels(label: str) -> None:
    # `*` is "read across my agents", `-` "no agent" — a
    # scope, not a measurement, once a consumer stands behind the read
    assert rc.is_agent_sentinel(label) and not rc.is_bench_label(label)


@pytest.mark.parametrize("label", ["clickgap", "demo", "travel-assistant", "evaluator", ""])
def test_named_agents_are_not_measurement_labels(label: str) -> None:
    assert not rc.is_measurement_label(label)
    assert not rc.is_bench_label(label) and not rc.is_agent_sentinel(label)


def test_the_consumer_label_counts_too() -> None:
    # r6g read under agent=clickgap with consumer=eval-r6g: a bench.
    assert rc.is_measurement_read(agent="clickgap", consumer="eval-r6g")
    assert not rc.is_measurement_read(agent="clickgap", consumer="clickgap")
    # a bench label on the agent is a bench whoever the consumer is
    assert rc.is_measurement_read(agent="eval-w9", consumer="clickgap")
    assert rc.is_measurement_read(agent="latency-bench", consumer="clickgap")


def test_a_named_consumer_reads_across_its_agents_without_being_a_measurement() -> None:
    # The SDK's default `memory.search` posts `agent: "*"` — the cross-agent
    # read boundary — and a consumer's closers may arrive under `agent: "-"`;
    # with the consumer named, neither sentinel is a measurement label.
    assert not rc.is_measurement_read(agent="*", consumer="clickgap")
    assert not rc.is_measurement_read(agent="-", consumer="clickgap")
    assert not rc.is_measurement_read(
        agent="*", consumer="clickgap", consumer_source=SOURCE_SELF_REPORTED
    )
    # ... and stays one with no consumer behind it: the probes and peer
    # benches read as `*` with the class fallback or nothing
    assert rc.is_measurement_read(agent="*", consumer="")
    assert rc.is_measurement_read(agent="-", consumer="")
    assert rc.is_measurement_read(
        agent="*", consumer="service-account", consumer_source=SOURCE_CLASS
    )
    assert rc.is_measurement_read(agent="*", consumer="service-account")
    # a bench consumer behind a sentinel is still a bench
    assert rc.is_measurement_read(agent="*", consumer="eval-p6r12")


def test_is_measurement_read_defaults_to_the_requests_consumer() -> None:
    consumer_mod.set_current_consumer("clickgap", SOURCE_SELF_REPORTED)
    try:
        assert not rc.is_measurement_read(agent="*")
    finally:
        consumer_mod.set_current_consumer("service-account", SOURCE_CLASS)
    try:
        assert rc.is_measurement_read(agent="*")
    finally:
        consumer_mod.set_current_consumer("", "")
    assert rc.is_measurement_read(agent="*")


def test_named_consumer_is_the_claimed_or_self_reported_value() -> None:
    assert rc.named_consumer("clickgap", SOURCE_SELF_REPORTED) == "clickgap"
    assert rc.named_consumer("clickgap", SOURCE_CLAIMED) == "clickgap"
    # the class fallback is a subject class, not a client
    assert rc.named_consumer("service-account", SOURCE_CLASS) == ""
    assert rc.named_consumer("user", SOURCE_CLASS) == ""
    assert rc.named_consumer("anonymous", SOURCE_CLASS) == ""
    # a row written before the class fallback / outside a request
    assert rc.named_consumer("", "") == ""
    assert rc.named_consumer("service-account", "") == ""


# ---------------------------------------------------------------- the rule


def _static(**kw: Any) -> rc.CreditDecision | None:
    base = dict(
        agent="clickgap",
        consumer="clickgap",
        consumer_source=SOURCE_SELF_REPORTED,
        run_id="",
        session_id="",
    )
    base.update(kw)
    return rc.decide_static(**base)


def test_static_rules_in_order() -> None:
    d = _static(agent="eval-w9")
    assert d is not None and not d.credited and d.reason == rc.REASON_MEASUREMENT_LABEL
    assert d.disclosure == "not_credited (measurement_label)"
    d = _static(consumer="eval-x")
    assert d is not None and d.reason == rc.REASON_MEASUREMENT_LABEL
    # the sentinels with no consumer: still a measurement read
    d = _static(agent="*", consumer="", consumer_source="")
    assert d is not None and d.reason == rc.REASON_MEASUREMENT_LABEL
    d = _static(agent="-", consumer="service-account", consumer_source=SOURCE_CLASS)
    assert d is not None and d.reason == rc.REASON_MEASUREMENT_LABEL
    # the sentinels behind a named consumer: the consumer's rules decide
    assert _static(agent="*") is None
    d = _static(agent="*", run_id="run-1")
    assert d is not None and d.credited
    d = _static(agent="-", session_id="sess-1")
    assert d is not None and d.credited
    d = _static(consumer="service-account", consumer_source=SOURCE_CLASS)
    assert d is not None and not d.credited and d.reason == rc.REASON_NO_CONSUMER
    d = _static(consumer="", consumer_source="")
    assert d is not None and d.reason == rc.REASON_NO_CONSUMER
    # a run or a session: credited without any lookup
    d = _static(run_id="run-1")
    assert d is not None and d.credited and d.disclosure == "credited"
    d = _static(session_id="sess-1")
    assert d is not None and d.credited
    # the sentinels are not a run
    assert _static(run_id="-") is None and _static(session_id="-") is None
    # a named consumer with no run: the closer window decides
    assert _static() is None


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return self.rows


def test_closer_window_reads_the_ledger_once_per_ttl() -> None:
    ch = _CH([{"seen": 1}])
    assert asyncio.run(rc.consumer_closed_loop("o", "clickgap", ch=ch)) is True
    assert asyncio.run(rc.consumer_closed_loop("o", "clickgap", ch=ch)) is True
    assert len(ch.calls) == 1  # cached
    sql, params = ch.calls[0]
    assert "FROM loom.agent_events" in sql and "consumer = {consumer:String}" in sql
    assert "PREWHERE" not in sql
    assert params["ops"] == ["memory.outcome", "memory.outcome_run", "run.complete"]
    assert params["days"] == 14 and params["consumer"] == "clickgap"


def test_no_closer_is_cached_and_note_closer_lifts_it() -> None:
    ch = _CH([])
    assert asyncio.run(rc.consumer_closed_loop("o", "clickgap", ch=ch)) is False
    assert asyncio.run(rc.consumer_closed_loop("o", "clickgap", ch=ch)) is False
    assert len(ch.calls) == 1
    rc.note_closer("o", "clickgap")
    assert asyncio.run(rc.consumer_closed_loop("o", "clickgap", ch=ch)) is True
    assert len(ch.calls) == 1  # answered from the note
    # another org / consumer is its own key
    assert asyncio.run(rc.consumer_closed_loop("o2", "clickgap", ch=ch)) is False
    assert asyncio.run(rc.consumer_closed_loop("o", "", ch=ch)) is False


def test_a_lookup_error_credits_nothing_and_caches_nothing() -> None:
    class _Boom:
        async def query_all(
            self, sql: str, params: dict[str, Any] | None = None
        ) -> list[dict[str, Any]]:
            raise RuntimeError("ch down")

    assert asyncio.run(rc.consumer_closed_loop("o", "clickgap", ch=_Boom())) is False
    assert rc._CLOSER_CACHE == {}


def test_decide_recall_credit_end_to_end() -> None:
    ch = _CH([{"seen": 1}])
    kw = dict(
        org="o",
        agent="clickgap",
        consumer="clickgap",
        consumer_source=SOURCE_SELF_REPORTED,
        run_id="",
        session_id="",
        ch=ch,
    )
    assert asyncio.run(rc.decide_recall_credit(**kw)).credited  # type: ignore[arg-type]
    rc._reset_for_tests()
    ch.rows = []
    d = asyncio.run(rc.decide_recall_credit(**kw))  # type: ignore[arg-type]
    assert not d.credited and d.disclosure == "not_credited (no_closer)"
    # a bench label never reaches the ledger
    ch.calls.clear()
    d = asyncio.run(rc.decide_recall_credit(**{**kw, "agent": "eval-c2"}))  # type: ignore[arg-type]
    assert d.reason == rc.REASON_MEASUREMENT_LABEL and ch.calls == []
    # nor does a sentinel with no consumer behind it
    d = asyncio.run(
        rc.decide_recall_credit(**{**kw, "agent": "*", "consumer": "", "consumer_source": ""})  # type: ignore[arg-type]
    )
    assert d.reason == rc.REASON_MEASUREMENT_LABEL and ch.calls == []
    # the SDK's default read under a named consumer asks the window like any other
    rc._reset_for_tests()
    ch.rows = [{"seen": 1}]
    d = asyncio.run(rc.decide_recall_credit(**{**kw, "agent": "*"}))  # type: ignore[arg-type]
    assert d.credited and len(ch.calls) == 1


def test_note_closer_from_request_uses_the_named_consumer() -> None:
    consumer_mod.set_current_consumer("service-account", SOURCE_CLASS)
    rc.note_closer_from_request("o")
    assert rc._CLOSER_CACHE == {}
    consumer_mod.set_current_consumer("clickgap", SOURCE_SELF_REPORTED)
    rc.note_closer_from_request("o")
    assert ("o", "clickgap") in rc._CLOSER_CACHE
    consumer_mod.set_current_consumer("", "")


# ---------------------------------------------------------------- the search handler


def _ctx(**overrides: Any) -> SimpleNamespace:
    inp = SimpleNamespace(
        org="o", namespace="n", agent="clickgap", auto_touch=True, run_id=None, session_id=None
    )
    for k, v in overrides.items():
        setattr(inp, k, v)
    hit = SimpleNamespace(namespace="n", memory_key="lesson_1")
    return SimpleNamespace(input=inp, final_hits=[hit], recall_credit=None)


async def _drain() -> None:
    from server.memory.app import after_write

    await after_write.drain()


def _run_auto_touch(
    monkeypatch: pytest.MonkeyPatch,
    ctx: SimpleNamespace,
    *,
    consumer: tuple[str, str] = ("clickgap", SOURCE_SELF_REPORTED),
    closer_rows: list[dict[str, Any]] | None = None,
    operation: str = "",
    request_agent: str = "",
    worker: str = "",
    request_id: str = "",
) -> list[dict[str, Any]]:
    from server.core.logging_config import request_id_var
    from server.memory.app.search import telemetry as h
    from server.memory.store import usage as mru
    from server.observability.genai import set_identity, set_worker_identity

    written: list[dict[str, Any]] = []

    async def _rec(
        org: str,
        ns_keys: dict[str, list[str]],
        *,
        event_type: str,
        agent: str = "",
        consumer: str | None = None,
        ch: Any = None,
    ) -> int:
        written.append(
            {
                "org": org,
                "ns_keys": ns_keys,
                "event_type": event_type,
                "agent": agent,
                "consumer": consumer,
            }
        )
        return 1

    monkeypatch.setattr(mru, "record_usage_for_keys", _rec)
    ch = _CH(closer_rows if closer_rows is not None else [{"seen": 1}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _go() -> None:
        consumer_mod.set_current_consumer(*consumer)
        set_identity(org="o" if operation else "", agent=request_agent, operation=operation)
        if worker:
            set_worker_identity(worker, org="o", namespace="n")
        request_id_var.set(request_id or None)
        try:
            await h._schedule_auto_touch(ctx)
            await _drain()
        finally:
            consumer_mod.set_current_consumer("", "")

    asyncio.run(_go())
    return written


def test_handler_credits_a_closing_consumer_and_discloses_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    written = _run_auto_touch(monkeypatch, ctx)
    assert ctx.recall_credit == "credited"
    # the credit carries the request's consumer
    assert written == [
        {
            "org": "o",
            "ns_keys": {"n": ["lesson_1"]},
            "event_type": "recall",
            "agent": "clickgap",
            "consumer": "clickgap",
        }
    ]


def test_handler_credits_the_sdk_default_read_under_a_named_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `Agent(consumer="clickgap").memory.search(...)` posts
    # `agent: "*"`; the consumer closes loops, so the read is credited and
    # the ledger row says who — agent `*`, consumer `clickgap`.
    ctx = _ctx(agent="*")
    written = _run_auto_touch(monkeypatch, ctx)
    assert ctx.recall_credit == "credited"
    assert written == [
        {
            "org": "o",
            "ns_keys": {"n": ["lesson_1"]},
            "event_type": "recall",
            "agent": "*",
            "consumer": "clickgap",
        }
    ]
    # `-` (ClickGap's closer process) the same
    ctx = _ctx(agent="-", run_id="run-9")
    assert len(_run_auto_touch(monkeypatch, ctx, closer_rows=[])) == 1
    assert ctx.recall_credit == "credited"


def test_handler_refuses_a_measurement_label_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the sentinels with no consumer behind them (the MCP default, a probe)
    ctx = _ctx(agent="*")
    assert _run_auto_touch(monkeypatch, ctx, consumer=("", "")) == []
    assert ctx.recall_credit == "not_credited (measurement_label)"
    ctx = _ctx(agent="*")
    assert _run_auto_touch(monkeypatch, ctx, consumer=("service-account", SOURCE_CLASS)) == []
    assert ctx.recall_credit == "not_credited (measurement_label)"
    # the bench labels, on the consumer or the agent, whoever else is named
    ctx = _ctx()
    assert _run_auto_touch(monkeypatch, ctx, consumer=("eval-w8", SOURCE_SELF_REPORTED)) == []
    assert ctx.recall_credit == "not_credited (measurement_label)"
    ctx = _ctx(agent="*")
    assert _run_auto_touch(monkeypatch, ctx, consumer=("eval-w8", SOURCE_SELF_REPORTED)) == []
    assert ctx.recall_credit == "not_credited (measurement_label)"
    for label in ("eval-c2", "latency-bench", "f1-drive"):
        ctx = _ctx(agent=label, run_id="run-1")
        assert _run_auto_touch(monkeypatch, ctx) == []
        assert ctx.recall_credit == "not_credited (measurement_label)"


def test_handler_refuses_the_class_fallback_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    # ClickGap today: agent=clickgap, consumer=service-account (class), no run
    ctx = _ctx()
    assert _run_auto_touch(monkeypatch, ctx, consumer=("service-account", SOURCE_CLASS)) == []
    assert ctx.recall_credit == "not_credited (no_consumer)"


def test_handler_refuses_a_consumer_without_a_closer_unless_in_a_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    assert _run_auto_touch(monkeypatch, ctx, closer_rows=[]) == []
    assert ctx.recall_credit == "not_credited (no_closer)"
    rc._reset_for_tests()
    ctx = _ctx(run_id="run-7")
    assert len(_run_auto_touch(monkeypatch, ctx, closer_rows=[])) == 1
    assert ctx.recall_credit == "credited"


def test_handler_honours_the_transport_bound_ledger_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability.run_context import reset_ledger_run, set_ledger_run

    ctx = _ctx()
    tok = set_ledger_run("mcp-session-run")
    try:
        assert len(_run_auto_touch(monkeypatch, ctx, closer_rows=[])) == 1
    finally:
        reset_ledger_run(tok)
    assert ctx.recall_credit == "credited"


def test_evaluation_read_is_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _ctx(auto_touch=False)
    assert _run_auto_touch(monkeypatch, ctx) == []
    assert ctx.recall_credit is None
    ctx = _ctx()
    ctx.final_hits = []
    assert _run_auto_touch(monkeypatch, ctx) == []
    assert ctx.recall_credit is None


# ---------------------------------------------------------------- the ledger column


@pytest.mark.asyncio
async def test_usage_event_carries_the_consumer_column(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every usage event names the consumer — the caller's
    when passed, else the request's (claim / body / class),
    else '' outside a request (a worker, an import)."""
    from server.memory.store import usage as mru

    class _Ins:
        def __init__(self) -> None:
            self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

        async def insert(
            self, table: str, rows: list[dict[str, Any]], settings: Any = None
        ) -> None:
            self.inserted.append((table, rows))

    ch = _Ins()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    mru._reset_for_tests()
    try:
        consumer_mod.set_current_consumer("clickgap", SOURCE_SELF_REPORTED)
        try:
            await mru.record_usage_event(
                memory_id="m1", org="o", namespace="n", event_type="recall", agent="*"
            )
            await mru.record_usage_event(
                memory_id="m1", org="o", namespace="n", event_type="used", consumer="other"
            )
        finally:
            consumer_mod.set_current_consumer("", "")
        await mru.record_usage_event(memory_id="m1", org="o", namespace="n", event_type="acted")
        await mru._flush_usage_buffer_now(background=True)
    finally:
        mru._reset_for_tests()
    rows = [r for table, batch in ch.inserted if table == "loom.memory_usage_events" for r in batch]
    assert [(r["event_type"], r["agent"], r["consumer"]) for r in rows] == [
        ("recall", "*", "clickgap"),
        ("used", "", "other"),
        ("acted", "", ""),
    ]
    assert set(rows[0]) == {
        "memory_id",
        "org",
        "namespace",
        "event_type",
        "event_count",
        "agent",
        "consumer",
        "ts",
    }


def test_usage_events_are_projected_onto_the_live_table() -> None:
    # a build ahead of the service's schema (an isolated server, the
    # deployment before its boot migration) keeps writing the credit
    from server.core.ch import PROJECTED_TABLES

    assert "loom.memory_usage_events" in PROJECTED_TABLES


# ---------------------------------------------------------------- the wire


def test_reinforcement_is_absent_unless_set() -> None:
    from server.memory.api.models_search import MemorySearchOutput

    before = MemorySearchOutput(results=[], embedding_model="e").model_dump(mode="json")
    assert "reinforcement" not in before
    out = MemorySearchOutput(results=[], embedding_model="e")
    out.reinforcement = None
    assert out.model_dump(mode="json") == before  # byte-identical for an evaluation read
    out.reinforcement = "not_credited (no_closer)"
    dumped = out.model_dump(mode="json")
    assert dumped["reinforcement"] == "not_credited (no_closer)"
    assert {k: v for k, v in dumped.items() if k != "reinforcement"} == before
    assert MemorySearchOutput.model_fields["reinforcement"].default is None


def test_ask_credits_skip_measurement_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.ask import handler as ha
    from server.memory.store import usage as mru

    called: list[str] = []

    async def _boom(*a: Any, **k: Any) -> int:
        called.append("credit")
        return 1

    monkeypatch.setattr(mru, "record_usage_for_keys", _boom)
    res = SimpleNamespace(
        abstain=False,
        source="synthesized",
        answer="x [m1]",
        results=[SimpleNamespace(memory_key="m1", namespace="n")],
        consistency="contradicted",
        contradicted_key="m1",
    )
    inp = SimpleNamespace(reinforce=True, org="o", namespace="n", agent="eval-l11")
    ha._schedule_cited_used(inp, res)
    ha._record_contradiction(inp, res)
    assert called == []
    # a `*` ask with no consumer behind it is a measurement read too
    inp = SimpleNamespace(reinforce=True, org="o", namespace="n", agent="*")
    ha._schedule_cited_used(inp, res)
    ha._record_contradiction(inp, res)
    assert called == []


def test_sdk_result_carries_the_disclosure() -> None:
    from loom.memory import _search_result

    assert _search_result({"results": [], "reinforcement": "credited"}).reinforcement == "credited"
    assert _search_result({"results": []}).reinforcement == ""


def test_settings_defaults() -> None:
    from server.settings import Settings

    f = Settings.model_fields
    assert f["memory_reinforcement_closer_window_days"].default == 14
    assert f["memory_reinforcement_closer_cache_seconds"].default == 300


@pytest.mark.parametrize("inspector", ["loom-ui", "loom-assistant"])
def test_a_person_looking_from_the_dashboard_or_the_assistant_is_never_credited(
    inspector: str,
) -> None:
    # A closer posted under the inspector's name (it is self-reported) does not open credit.
    ch = _CH([{"seen": 1}])
    for run_id, session_id in (("", ""), ("run-1", "s-1")):
        decision = asyncio.run(
            rc.decide_recall_credit(
                org="o",
                agent="*",
                consumer=inspector,
                consumer_source="self-reported",
                run_id=run_id,
                session_id=session_id,
                ch=ch,
            )
        )
        assert decision == rc.CreditDecision(False, rc.REASON_NO_CONSUMER)
    assert ch.calls == []  # refused before the closer window is read


# ---------------------------------------------------------------- served, not credited


def _types(written: list[dict[str, Any]]) -> list[str]:
    return [w["event_type"] for w in written]


@pytest.mark.parametrize("operation", ["context.pack", "memory.ask", "procedure.match"])
def test_an_op_that_turns_auto_touch_off_still_records_what_it_served(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    # context.pack and memory.ask run their search with auto_touch off to keep the ranking
    # credit out; the keys they return were still shown to an agent.
    ctx = _ctx(agent="*", auto_touch=False)
    written = _run_auto_touch(monkeypatch, ctx, consumer=("", ""), operation=operation)
    assert _types(written) == ["served"]
    assert written[0]["ns_keys"] == {"n": ["lesson_1"]}
    assert ctx.recall_credit is None
    # the op searches as `*`; the event names the agent that called it
    ctx = _ctx(agent="*", auto_touch=False)
    written = _run_auto_touch(
        monkeypatch, ctx, consumer=("", ""), operation=operation, request_agent="codex"
    )
    assert [w["agent"] for w in written] == ["codex"]


def test_an_uncredited_search_records_served_and_a_credited_one_only_its_credit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    written = _run_auto_touch(monkeypatch, ctx, closer_rows=[], operation="memory.search")
    assert ctx.recall_credit == "not_credited (no_closer)"
    assert _types(written) == ["served"]
    rc._reset_for_tests()
    ctx = _ctx()
    assert _types(_run_auto_touch(monkeypatch, ctx, operation="memory.search")) == ["recall"]


def test_reads_that_record_no_served_event(monkeypatch: pytest.MonkeyPatch) -> None:
    # the caller of memory.search / search.query asked for no usage
    for op in ("memory.search", "search.query"):
        ctx = _ctx(auto_touch=False)
        assert _run_auto_touch(monkeypatch, ctx, operation=op) == []
    # the dashboard and the assistant: a person inspecting the namespace
    for consumer in ("loom-ui", "loom-assistant"):
        ctx = _ctx(agent="*", auto_touch=False)
        written = _run_auto_touch(
            monkeypatch, ctx, consumer=(consumer, SOURCE_SELF_REPORTED), operation="context.pack"
        )
        assert written == []
    # a bench label, on the agent, the consumer, or the agent calling a pack
    ctx = _ctx(agent="latency-bench")
    assert _run_auto_touch(monkeypatch, ctx, operation="memory.search") == []
    ctx = _ctx(agent="*", auto_touch=False)
    written = _run_auto_touch(
        monkeypatch, ctx, consumer=("eval-w8", SOURCE_SELF_REPORTED), operation="context.pack"
    )
    assert written == []
    ctx = _ctx(agent="*", auto_touch=False)
    written = _run_auto_touch(
        monkeypatch, ctx, operation="context.pack", request_agent="latency-bench"
    )
    assert written == []
    # a worker's search, stamped the way the supervisor stamps a pass
    ctx = _ctx(auto_touch=False)
    assert _run_auto_touch(monkeypatch, ctx, worker="knowledge_source_sync") == []
    # ops that run a search without returning memory to an agent
    for op in ("decision.precedents", "memory.apply"):
        ctx = _ctx(agent="*", auto_touch=False)
        assert _run_auto_touch(monkeypatch, ctx, consumer=("", ""), operation=op) == []


def test_one_request_writes_one_served_event_per_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """memory.ask runs several passes whose pools overlap; one ask is one read."""
    first = _ctx(agent="*", auto_touch=False)
    written = _run_auto_touch(
        monkeypatch, first, consumer=("", ""), operation="memory.ask", request_id="rid-1"
    )
    assert _types(written) == ["served"]
    again = _ctx(agent="*", auto_touch=False)
    written = _run_auto_touch(
        monkeypatch, again, consumer=("", ""), operation="memory.ask", request_id="rid-1"
    )
    assert written == []
    # another request reads it again
    other = _ctx(agent="*", auto_touch=False)
    written = _run_auto_touch(
        monkeypatch, other, consumer=("", ""), operation="memory.ask", request_id="rid-2"
    )
    assert _types(written) == ["served"]
