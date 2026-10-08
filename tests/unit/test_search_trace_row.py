"""Unit: the loom.search_trace_log row builder (recall telemetry).

The write path is fire-and-forget against ClickHouse; the interesting logic is
building the row from the search input/output, which is pure and tested here.

The `query` column carries text a person typed, so it is gated on the same
per-namespace consent that decides whether prompt text reaches Langfuse. Tests
that expect a query in the row therefore have to declare that consent, exactly
as a real request does.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search.telemetry import _build_trace_row
from server.observability import genai


@pytest.fixture
def _consented():
    """A request whose namespace has opted into text capture."""
    genai.set_identity(org="o", namespace="ns", capture_level="all")
    yield
    genai.set_identity()


def test_build_trace_row_captures_query_keys_scores_and_signals(_consented):
    inp = MemorySearchInput(
        org="o",
        namespace="ns",
        agent="a",
        query="how to X",
        top_k=3,
        run_id="r1",
    )
    out = SimpleNamespace(
        results=[
            SimpleNamespace(memory_key="k1", rank_score=0.9),
            SimpleNamespace(memory_key="k2", rank_score=0.4),
        ],
        abstain=True,
        retrieval_risk="high",
    )
    emb = SimpleNamespace(model_name="text-embedding-3-large")

    row = _build_trace_row(inp, out, emb, 123)

    assert row["org"] == "o"
    assert row["namespace"] == "ns"
    assert row["agent"] == "a"
    assert row["query"] == "how to X"
    assert row["returned_keys"] == ["k1", "k2"]
    assert row["returned_scores"] == [0.9, 0.4]
    assert row["top_k"] == 3
    assert row["result_count"] == 2
    assert row["abstain"] == 1
    assert row["retrieval_risk"] == "high"
    assert row["embedding_model"] == "text-embedding-3-large"
    assert row["latency_ms"] == 123
    assert row["run_id"] == "r1"
    # Outside a transport request the op falls back to the
    # handler's own name; a stamped request labels the row with its op.
    assert row["operation"] == "memory.search"


def test_build_trace_row_defaults_on_empty():
    inp = MemorySearchInput(org="o", namespace="ns", agent="a", query="q", top_k=5)
    out = SimpleNamespace(results=[], abstain=False, retrieval_risk="low")
    emb = SimpleNamespace(model_name="m")

    row = _build_trace_row(inp, out, emb, 0)

    assert row["returned_keys"] == []
    assert row["returned_scores"] == []
    assert row["result_count"] == 0
    assert row["abstain"] == 0
    assert row["run_id"] == "-"  # run_id defaults to sentinel when unset


def _row(query: str = "what did I say about postgres"):
    inp = MemorySearchInput(org="o", namespace="ns", agent="a", query=query, top_k=2)
    out = SimpleNamespace(
        results=[SimpleNamespace(memory_key="k1", rank_score=0.7)],
        abstain=False,
        retrieval_risk="low",
    )
    return _build_trace_row(inp, out, SimpleNamespace(model_name="m"), 42)


@pytest.mark.parametrize("level", ["query", "all"])
def test_query_is_logged_when_the_namespace_consented(level):
    genai.set_identity(org="o", namespace="ns", capture_level=level)
    try:
        assert _row()["query"] == "what did I say about postgres"
    finally:
        genai.set_identity()


def test_query_is_blanked_when_consent_is_off():
    """`off` must reach loom's own trace log, not only Langfuse.

    With `LOOM_SEARCH_TRACE_LOG_ENABLED` as the only check, a namespace that
    explicitly declined text capture would still have every search phrase
    written to loom.search_trace_log: two sinks for the same text, one consent
    answer, and only one of them honouring it.
    """
    genai.set_identity(org="o", namespace="ns", capture_level="off")
    try:
        assert _row()["query"] == ""
    finally:
        genai.set_identity()


def test_declining_text_still_keeps_the_quality_signal():
    """Consent must cost text, not observability.

    Recall, abstain rate and latency carry no user content. Dropping them along
    with the query would make declining also mean "no quality data", which is
    what pressures an operator into refusing the privacy setting.
    """
    genai.set_identity(org="o", namespace="ns", capture_level="off")
    try:
        row = _row()
    finally:
        genai.set_identity()
    assert row["returned_keys"] == ["k1"]
    assert row["returned_scores"] == [0.7]
    assert row["result_count"] == 1
    assert row["latency_ms"] == 42
    assert row["retrieval_risk"] == "low"


def test_no_identity_at_all_fails_closed():
    """A worker or CLI path with no resolved consent must not log the query."""
    genai.set_identity()
    assert _row()["query"] == ""


def test_server_default_is_off():
    """The operator default is no text capture; 'query' is a deliberate opt-in.

    A "query" default would record a phrase a person typed for every
    namespace that had never been asked the question.
    """
    from server.settings import Settings

    assert Settings().langfuse_capture_io_level == "off"


def test_build_trace_row_carries_the_request_shape_digest(_consented):
    """Same query + same shape = same request;
    the digest covers modes, limits, flags and filters, never the text."""
    from server.memory.app.search.telemetry import search_shape_sha256

    base = MemorySearchInput(org="o", namespace="ns", agent="a", query="how to X", top_k=3)
    same_text_other_shape = MemorySearchInput(
        org="o",
        namespace="ns",
        agent="a",
        query="how to X",
        top_k=7,
        tags=["ci"],
    )
    other_text_same_shape = MemorySearchInput(
        org="o", namespace="ns", agent="a", query="Y", top_k=3
    )
    other_identity = MemorySearchInput(
        org="o2", namespace="ns2", agent="b", query="how to X", top_k=3
    )
    row = _build_trace_row(base, SimpleNamespace(results=[]), SimpleNamespace(model_name="m"), 1)
    assert len(row["shape_sha256"]) == 64
    assert row["shape_sha256"] == search_shape_sha256(base)
    assert search_shape_sha256(base) != search_shape_sha256(same_text_other_shape)
    assert search_shape_sha256(base) == search_shape_sha256(other_text_same_shape)
    assert search_shape_sha256(base) == search_shape_sha256(
        other_identity
    )  # identity has its own columns


def test_shape_digest_ignores_defaults_and_field_order():
    from server.memory.app.search.telemetry import search_shape_sha256

    explicit_default = MemorySearchInput(
        org="o",
        namespace="ns",
        agent="a",
        query="q",
        top_k=5,
        scope="private",
        alpha=0.5,
    )
    implicit = MemorySearchInput(org="o", namespace="ns", agent="a", query="q")
    assert search_shape_sha256(explicit_default) == search_shape_sha256(implicit)
    tagged_a = MemorySearchInput(org="o", namespace="ns", agent="a", query="q", tags=["x", "y"])
    tagged_b = MemorySearchInput(org="o", namespace="ns", agent="a", query="q", tags=["y", "x"])
    assert search_shape_sha256(tagged_a) != search_shape_sha256(
        tagged_b
    )  # tag order is a filter difference


def test_build_trace_row_persists_the_stage_partition_and_the_loop_lag(_consented):
    """`retrieval_work.stage_ms` lands as Map(String, UInt32)
    (whole milliseconds, `total` kept so the partition is checkable in SQL)
    and `loop_lag_ms` as a float, beside latency_ms."""
    inp = MemorySearchInput(org="o", namespace="ns", agent="a", query="q", top_k=3)
    out = SimpleNamespace(
        results=[],
        abstain=False,
        retrieval_risk="low",
        retrieval_work={
            "loop_lag_ms": 0.03,
            "stage_ms": {"plan": 12.4, "embed": 130.6, "vector_legs": 79.5, "total": 222.5},
            "llm_calls_total": 1,
        },
    )
    row = _build_trace_row(inp, out, SimpleNamespace(model_name="m"), 223)
    assert row["stage_ms"] == {"plan": 12, "embed": 131, "vector_legs": 80, "total": 222}
    assert all(isinstance(v, int) for v in row["stage_ms"].values())
    assert row["loop_lag_ms"] == 0.03
    assert row["latency_ms"] == 223


def test_trace_row_without_attribution_writes_an_empty_map_and_zero_lag(_consented):
    """A fallback output with no retrieval_work, or one of an odd shape, must
    still produce a row: the columns default, the write never fails."""
    inp = MemorySearchInput(org="o", namespace="ns", agent="a", query="q", top_k=3)
    emb = SimpleNamespace(model_name="m")
    bare = _build_trace_row(inp, SimpleNamespace(results=[]), emb, 1)
    assert bare["stage_ms"] == {} and bare["loop_lag_ms"] == 0.0
    odd = _build_trace_row(
        inp,
        SimpleNamespace(results=[], retrieval_work={"stage_ms": "nope", "loop_lag_ms": True}),
        emb,
        1,
    )
    assert odd["stage_ms"] == {} and odd["loop_lag_ms"] == 0.0
    negative = _build_trace_row(
        inp,
        SimpleNamespace(results=[], retrieval_work={"stage_ms": {"plan": -1, "embed": 2}}),
        emb,
        1,
    )
    assert negative["stage_ms"] == {"embed": 2}


def test_build_trace_row_labels_the_callers_operation():
    """Memory.ask / context.pack / the MCP search tool share
    this handler; the row names the op the caller actually invoked."""
    genai.set_identity(org="o", namespace="ns", capture_level="off", operation="memory.ask")
    try:
        inp = MemorySearchInput(org="o", namespace="ns", agent="a", query="q", top_k=1)
        out = SimpleNamespace(results=[], abstain=False, retrieval_risk="low")
        row = _build_trace_row(inp, out, SimpleNamespace(model_name="m"), 1)
    finally:
        genai.set_identity()
    assert row["operation"] == "memory.ask"
    assert row["query"] == ""  # consent off: the wording stays out


def test_build_trace_row_takes_the_transport_bound_run_label():
    """A read labelled with a run through the transport
    (X-Loom-Run-Id / the SDK's ambient run) lands in the trace under that
    run; a body run_id still wins; neither → '-'."""
    from server.observability.run_context import reset_ledger_run, set_ledger_run

    out = SimpleNamespace(results=[], abstain=False, retrieval_risk="low")
    emb = SimpleNamespace(model_name="m")
    tok = set_ledger_run("run-from-header")
    try:
        row = _build_trace_row(
            MemorySearchInput(org="o", namespace="ns", agent="a", query="q", top_k=1), out, emb, 1
        )
        assert row["run_id"] == "run-from-header"
        row = _build_trace_row(
            MemorySearchInput(
                org="o", namespace="ns", agent="a", query="q", top_k=1, run_id="body"
            ),
            out,
            emb,
            1,
        )
        assert row["run_id"] == "body"
    finally:
        reset_ledger_run(tok)
    row = _build_trace_row(
        MemorySearchInput(org="o", namespace="ns", agent="a", query="q", top_k=1), out, emb, 1
    )
    assert row["run_id"] == "-"
