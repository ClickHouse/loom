"""Unit tests for server/observability/metrics.py.

Two things are worth pinning here, and neither is "does it emit a number".

**Cardinality.** Metric attributes must come from a closed set. A single
unbounded label (memory_key, user_id, query text) multiplies series without
bound and turns `otel_metrics_*` into a write-amplification engine. That damage
is invisible in tests unless something asserts on the attribute keys.

**Fail-open.** Telemetry runs inside the request path. Every entry point here
must swallow its own errors — a metrics bug must never turn a served response
into a 500.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from server.observability import metrics as m
from server.observability import metrics_gauges

# Attribute keys the domain metrics are allowed to set. Anything outside this
# set is either unbounded or high-cardinality.
_ALLOWED_KEYS = {
    "loom.operation",
    "loom.namespace",
    "loom.transport",
    "reason",
    "plane",
    "kind",
    "outcome",
    "stage",
}

_FORBIDDEN_SUBSTRINGS = ("memory_key", "user_id", "session", "query", "trace", "request_id")


class _Recorder:
    """Stand-in instrument capturing (value, attributes) pairs."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dict[str, Any]]] = []

    def record(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))

    def add(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))


@pytest.fixture
def instruments(monkeypatch):
    """Swap every instrument for a recorder and hand them back by name."""
    names = (
        "_search_results",
        "_search_top_similarity",
        "_search_confidence",
        "_search_abstain",
        "_search_plane_hits",
        "_search_assist",
        "_memory_writes",
        "_memory_drops",
    )
    recs = {n: _Recorder() for n in names}
    for n, r in recs.items():
        monkeypatch.setattr(m, n, r)
    return recs


def _response(**over) -> dict[str, Any]:
    base = {
        "results": [
            {"distance": 0.42, "source": "fact"},
            {"distance": 0.31, "source": "fact"},
            {"distance": 0.55, "source": "chunk"},
        ],
        "retrieval_confidence": 0.61,
        "abstain": False,
        # Present on every real MemorySearchOutput, and the field the shape gate
        # keys on — `results` alone also matches memory.get_many, memory.related
        # and knowledge.search.
        "retrieval_risk": "medium",
    }
    base.update(over)
    return base


def _all_attrs(recs) -> list[dict[str, Any]]:
    return [a for r in recs.values() for _, a in r.calls]


def test_no_unbounded_attribute_keys(instruments) -> None:
    """The whole point: attributes stay inside a closed, bounded set."""
    m.record_search_quality(
        "memory.search",
        namespace="clickgap-public",
        result=_response(abstain=True, abstain_reason="weak_top_hit"),
    )
    m.record_memory_write(namespace="clickgap-public", outcome="conflicted")
    attrs = _all_attrs(instruments)
    assert attrs, "nothing recorded — fixture wiring is wrong"
    for a in attrs:
        extra = set(a) - _ALLOWED_KEYS
        assert not extra, f"unbounded attribute key(s) introduced: {extra}"
        for k in a:
            for bad in _FORBIDDEN_SUBSTRINGS:
                assert bad not in k.lower(), f"high-cardinality key {k!r}"


def test_top_similarity_uses_the_best_distance_not_the_first(instruments) -> None:
    """Hits arrive rank-ordered, but the metric must not trust position.

    The best distance in the fixture is 0.31 (the SECOND hit), so a naive
    `hits[0]` would report 0.58 instead of 0.69.
    """
    m.record_search_quality("memory.search", namespace="ns", result=_response())
    vals = [v for v, _ in instruments["_search_top_similarity"].calls]
    assert len(vals) == 1
    assert vals[0] == pytest.approx(1.0 - 0.31)


def test_records_result_count_and_confidence(instruments) -> None:
    m.record_search_quality("memory.search", namespace="ns", result=_response())
    assert [v for v, _ in instruments["_search_results"].calls] == [3]
    assert [v for v, _ in instruments["_search_confidence"].calls] == [pytest.approx(0.61)]


def test_plane_attribution_counts_per_source(instruments) -> None:
    """Tells you which retrieval legs earn their latency."""
    m.record_search_quality("memory.search", namespace="ns", result=_response())
    by_plane = {a["plane"]: v for v, a in instruments["_search_plane_hits"].calls}
    assert by_plane == {"fact": 2, "chunk": 1}


_ABSTAIN_CODES = {
    "no_match",
    "below_threshold",
    "low_calibrated_confidence",
    "unspecified",
}


def test_abstain_is_counted_with_a_bounded_reason(instruments) -> None:
    m.record_search_quality(
        "memory.search",
        namespace="ns",
        result=_response(abstain=True, abstain_reason="x" * 200),
    )
    calls = instruments["_search_abstain"].calls
    assert len(calls) == 1
    assert calls[0][1]["reason"] in _ABSTAIN_CODES


def test_abstain_label_is_a_code_not_the_interpolated_prose(instruments) -> None:
    """The prose reason embeds floats, so it cannot be the label.

    `abstain_reason` renders as "top hit similarity 0.19 is below the abstain
    threshold 0.45 — …". Using it directly minted one series per distinct
    similarity value; truncating to 48 chars bounded the length and not the
    cardinality. Two abstentions differing only in their similarity must
    collapse onto a single series.
    """
    for sim in (0.19, 0.23, 0.41):
        m.record_search_quality(
            "memory.search",
            namespace="ns",
            result=_response(
                abstain=True,
                abstain_code="below_threshold",
                abstain_reason=(
                    f"top hit similarity {sim:.2f} is below the abstain "
                    f"threshold 0.45 — results returned but agent should hedge"
                ),
            ),
        )
    labels = {a["reason"] for _, a in instruments["_search_abstain"].calls}
    assert labels == {"below_threshold"}


def test_abstain_code_is_derived_from_the_reason_when_the_response_has_none(
    instruments,
) -> None:
    """Responses without `abstain_code` still get a bounded label by prefix."""
    for reason, expected in (
        ("no memories matched the query above the noise floor", "no_match"),
        ("top hit similarity 0.19 is below the abstain threshold", "below_threshold"),
        (
            "calibrated retrieval confidence is too low for a grounded answer: x",
            "low_calibrated_confidence",
        ),
        ("something new nobody anticipated", "unspecified"),
    ):
        instruments["_search_abstain"].calls.clear()
        m.record_search_quality(
            "memory.search",
            namespace="ns",
            result=_response(abstain=True, abstain_reason=reason),
        )
        assert instruments["_search_abstain"].calls[0][1]["reason"] == expected


def test_no_abstain_counter_when_not_abstaining(instruments) -> None:
    m.record_search_quality("memory.search", namespace="ns", result=_response())
    assert instruments["_search_abstain"].calls == []


def test_assists_are_counted_by_kind(instruments) -> None:
    """Each assist adds 1-3s; counting them explains latency spikes that the
    duration histogram alone makes look like a regression.

    Assist flags live in `retrieval_work` (built from
    `SearchContext.actual_work`), not as top-level response fields.
    """
    m.record_search_quality(
        "memory.search",
        namespace="ns",
        result=_response(
            retrieval_work={
                "inline_rerank_used": True,
                "llm_hyde_calls": 1,
                # Zero-valued counters must not register as an assist.
                "llm_iterative_judge_calls": 0,
            },
        ),
    )
    kinds = {a["kind"] for _, a in instruments["_search_assist"].calls}
    assert kinds == {"hyde", "rerank"}


def test_assist_flags_are_read_off_the_real_response_model(instruments) -> None:
    """Regression guard for the fabricated-shape trap.

    `_response` is a plain dict, so it accepts any key: a reader of top-level
    `rerank_used` / `query_rewrite_used` would pass against it although those
    fields do not exist on `MemorySearchOutput`. Driving the metric off the real
    model means a field name that doesn't exist fails here.
    """
    from server.memory.api.models_search import MemorySearchOutput

    out = MemorySearchOutput(
        results=[],
        embedding_model="fake",
        retrieval_work={"external_rerank_used": True, "hyde_initial_used": True},
    )
    m.record_search_quality("memory.search", namespace="ns", result=out)
    kinds = {a["kind"] for _, a in instruments["_search_assist"].calls}
    assert kinds == {"hyde", "rerank"}
    assert not hasattr(out, "rerank_used")
    assert not hasattr(out, "query_rewrite_used")


def test_every_record_helper_has_a_production_call_site() -> None:
    """Every `record_*` helper is called from production code.

    A metric that is defined but never emitted is indistinguishable from "no
    events happened" on a dashboard. A `record_*` helper with no call site
    anywhere in `server/` outside this module is dead instrumentation, so fail
    here rather than discover it by querying `otel_metrics_*`.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "server"
    metrics_mod = src / "observability" / "metrics.py"
    helpers = set(re.findall(r"^def (record_\w+)", metrics_mod.read_text(), re.M))
    assert helpers, "no record_* helpers found — did metrics.py move?"

    callers = "\n".join(p.read_text() for p in src.rglob("*.py") if p != metrics_mod)
    # Require actual CALL syntax, not a bare substring. A plain `h in callers`
    # is satisfied by a mention in a comment or docstring (documented but never
    # invoked) and would also match a longer name that merely starts with it.
    dead = sorted(h for h in helpers if not re.search(rf"\b{h}\s*\(", callers))
    assert not dead, f"dead instrumentation, no production call site: {dead}"


def test_assist_flags_reference_keys_the_handler_actually_writes() -> None:
    """Guard the `retrieval_work` key names the assist counter reads.

    This is the original bug generalised. The counter read top-level
    `rerank_used` / `query_rewrite_used`, which never existed, and returned
    False forever — the metric looked merely quiet. Reading `retrieval_work` by
    string key has the same exposure: rename a key in the handler and the
    corresponding assist silently stops counting, with nothing failing.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    metrics_src = (root / "server" / "observability" / "metrics.py").read_text()

    # The keys `_flag(...)` is called with inside record_search_quality.
    flag_keys = set(re.findall(r"_flag\(([^)]*)\)", metrics_src))
    keys: set[str] = set()
    for group in flag_keys:
        keys.update(re.findall(r'"([a-z_]+)"', group))
    assert keys, "no _flag keys found — did the assist block move?"

    handler_src = "\n".join(p.read_text() for p in (root / "server").rglob("*.py"))
    # The handler writes them as actual_work["<key>"] (and they reach the
    # response verbatim through _actual_work_payload).
    # memory.search writes `actual_work[...]`; code.search's pipeline names its
    # dict `work` (server/code/app/search/) — both reach the response as
    # `retrieval_work`, so both count as a write.
    written = set(re.findall(r'(?:actual_)?work\[\s*"([a-z_]+)"\s*\]', handler_src))
    missing = sorted(keys - written)
    assert not missing, f"assist counter reads retrieval_work keys nothing writes: {missing}"


def test_non_search_response_is_ignored(instruments) -> None:
    """Duck-typed on the search shape, so non-search ops cost nothing."""
    m.record_search_quality("memory.set", namespace="ns", result={"ok": True})
    assert _all_attrs(instruments) == []


def test_other_ops_that_also_return_results_are_not_counted_as_searches(
    instruments, monkeypatch
) -> None:
    """`results` alone is not a search.

    memory.get_many returns a *dict* under `results` (len() would be a key
    count, iteration would yield strings); memory.related, knowledge.search and
    ops.projection_failure_retry all return a `results` list. Counting them
    inflated the abstain-rate denominator with bulk reads and graph lookups, so
    a real retrieval outage read healthy on the panel.
    """
    from server.observability import otel as _otel

    spans: list[dict[str, Any]] = []
    monkeypatch.setattr(_otel, "set_search_attributes", lambda **kw: spans.append(kw))

    # memory.get_many — dict under `results`
    m.record_search_quality(
        "memory.get_many", namespace="ns", result={"results": {"k1": {}, "k2": {}}}
    )
    # memory.related / knowledge.search — list, but no retrieval_risk envelope
    m.record_search_quality(
        "memory.related",
        namespace="ns",
        result={"results": [{"memory_key": "a", "distance": 0.1, "source": "fact"}]},
    )

    assert _all_attrs(instruments) == []
    assert spans == [], "non-search op stamped loom.search.* on its span"


def test_namespace_is_length_clamped(instruments) -> None:
    m.record_memory_write(namespace="n" * 500, outcome="created")
    ns = instruments["_memory_writes"].calls[0][1]["loom.namespace"]
    assert len(ns) <= 64


def test_empty_namespace_becomes_the_sentinel(instruments) -> None:
    m.record_memory_write(namespace="", outcome="created")
    assert instruments["_memory_writes"].calls[0][1]["loom.namespace"] == "-"


def test_never_raises_on_a_malformed_response(instruments) -> None:
    """Fail-open: a telemetry bug must not fail a served request."""

    class Hostile:
        @property
        def results(self):
            raise RuntimeError("boom")

    m.record_search_quality("memory.search", namespace="ns", result=Hostile())
    m.record_search_quality("memory.search", namespace="ns", result=None)
    m.record_search_quality("memory.search", namespace="ns", result={"results": "nonsense"})


def test_no_ops_cleanly_when_uninitialised(monkeypatch) -> None:
    """With OTel off the instruments are None; calls must be silent no-ops."""
    monkeypatch.setattr(m, "_search_results", None)
    monkeypatch.setattr(m, "_memory_writes", None)
    m.record_search_quality("memory.search", namespace="ns", result=_response())
    m.record_memory_write(namespace="ns", outcome="created")


def _drain(cache: metrics_gauges._TTLCache, timeout_s: float = 5.0) -> None:
    """Wait for an in-flight background refresh to land."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with cache._lock:
            if not cache._refreshing and cache._fetched_at > 0.0:
                return
        time.sleep(0.005)
    raise AssertionError("background refresh never completed")


def test_ttl_cache_first_refresh_runs_on_a_host_younger_than_the_ttl(monkeypatch) -> None:
    """monotonic() counts from boot. On a runner up for 5 s a 900 s cache read
    `5.0 - 0.0 >= 900` as false and never produced. Never-fetched is always
    due."""
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return {"agent_labels_24h": [(1.0, {"loom.kind": "used"})]}

    # The clock stays at 5 s for the whole test: restoring it early would make
    # the second read see `now - 5.0 >= 900` and start a legitimate second refresh.
    monkeypatch.setattr(metrics_gauges.time, "monotonic", lambda: 5.0)
    c = metrics_gauges._TTLCache(900.0, producer)
    assert c.get() is None  # first read: no observation, refresh started
    with c._lock:
        started = c._refreshing or c._fetched_at > 0.0
    assert started
    _drain(c)
    assert c.get() == {"agent_labels_24h": [(1.0, {"loom.kind": "used"})]}
    assert calls["n"] == 1  # within the TTL: no second refresh


def test_ttl_cache_calls_the_producer_once_within_the_window() -> None:
    """Gauge callbacks fire every 3s at the export interval; without this cache
    each CH-backed gauge would issue ~20 queries a minute forever."""
    calls = {"n": 0}

    def producer():
        calls["n"] += 1
        return {"embed_pending": calls["n"]}

    c = metrics_gauges._TTLCache(60.0, producer)
    c.get()  # kicks off the first background refresh
    _drain(c)
    assert c.get() == {"embed_pending": 1}
    assert c.get() == {"embed_pending": 1}
    assert calls["n"] == 1, "producer called more than once inside the TTL"


def test_ttl_cache_never_blocks_the_caller_on_a_slow_producer() -> None:
    """The callback runs inside the reader's collect(), which has a 2.5s export
    timeout. The state probe is a multi-join FINAL query against CH Cloud that
    routinely exceeds it — produced inline, every cache expiry would be a
    MetricsTimeoutError and a dropped export cycle."""
    release = threading.Event()

    def slow_producer():
        release.wait(10)
        return {"embed_pending": 7}

    c = metrics_gauges._TTLCache(60.0, slow_producer)
    t0 = time.monotonic()
    first = c.get()
    second = c.get()  # single-flight: must not start (or wait on) a second refresh
    assert time.monotonic() - t0 < 1.0, "get() blocked on the producer"
    assert first is None and second is None  # stale-while-revalidate: no value yet
    release.set()
    _drain(c)
    assert c.get() == {"embed_pending": 7}


def test_ttl_cache_survives_a_failing_producer() -> None:
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("CH down")

    c = metrics_gauges._TTLCache(60.0, boom)
    assert c.get() is None
    _drain(c)
    assert c.get() is None
    assert calls["n"] == 1, "a failed refresh must wait out the TTL, not retry per export"


# ----------------------------------------------------------------------
# Telemetry environment guard
# ----------------------------------------------------------------------


def test_otel_refuses_to_initialise_under_pytest() -> None:
    """A test run must never ship telemetry to the production tables.

    `init_otel(app)` is called at MODULE level in server/main.py, so importing
    the app from a test starts an exporter. With LOOM_CH_HOST pointed at
    production that writes spans, logs and metrics into the real observability
    tables, as ERROR rows indistinguishable from a real incident.

    pytest is detected rather than configured, because the failure is silent and
    whoever adds the next test will not think to opt out.
    """
    import sys

    from server.observability import otel

    assert "pytest" in sys.modules, "guard relies on pytest being importable"
    otel._initialized = False
    try:
        assert otel.init_otel(None) is None, "OTel initialised during a test run"
    finally:
        otel._initialized = False


def test_otel_master_switch_is_respected(monkeypatch) -> None:
    from server.observability import otel
    from server.settings import settings

    monkeypatch.setattr(settings, "otel_enabled", False)
    otel._initialized = False
    try:
        assert otel.init_otel(None) is None
    finally:
        otel._initialized = False


def test_span_attributes_are_set_even_without_domain_instruments(monkeypatch) -> None:
    """Span attribution must not depend on the metric instruments existing.

    The domain instruments are created only under `exporter_mode == "ch"`, but
    span export is configured independently. An early `if _search_results is
    None: return` would leave OTLP deployments with no `loom.search.*`
    attributes at all, so the "show me the searches that abstained" query
    would return zero rows on a system that abstains constantly.
    """
    from server.observability import otel as _otel

    for name in (
        "_search_results",
        "_search_confidence",
        "_search_top_similarity",
        "_search_abstain",
        "_search_plane_hits",
        "_search_assist",
    ):
        monkeypatch.setattr(m, name, None)

    seen: dict[str, Any] = {}
    monkeypatch.setattr(_otel, "set_search_attributes", lambda **kw: seen.update(kw))

    m.record_search_quality(
        "memory.search",
        namespace="ns",
        result=_response(abstain=True, abstain_code="below_threshold"),
    )

    assert seen, "span attribution skipped when instruments are absent"
    assert seen["hits"] == 3
    assert seen["abstain"] is True


def test_top_similarity_ignores_planes_whose_distance_is_not_a_cosine(
    instruments,
) -> None:
    """`distance` is overloaded; only vector planes may feed the similarity.

    Record-plane rows store ``max(0, 1 - lexical_rank_score)`` — a token-overlap
    score — and injected computed/derived evidence rows hardcode
    ``distance=0.0``, i.e. a perfect 1.0. A plain min over every hit therefore
    reported near-perfect retrieval quality for searches that abstained, and
    every aggregational query recorded exactly 1.0, pinning the decile buckets
    in the top bin so the panel meant to catch degradation read healthy.
    """
    m.record_search_quality(
        "memory.search",
        namespace="ns",
        result=_response(
            results=[
                # The real semantic evidence: weak.
                {"distance": 0.81, "source": "fact"},
                # Token-overlap masquerading as a near-perfect cosine.
                {"distance": 0.05, "source": "record"},
                # Injected evidence row, hardcoded perfect.
                {"distance": 0.0, "source": "computed"},
                {"distance": 0.0, "source": "derived"},
            ]
        ),
    )
    recorded = [v for v, _ in instruments["_search_top_similarity"].calls]
    assert recorded == [pytest.approx(1.0 - 0.81)], (
        "similarity must come from the vector plane only, not from lexical "
        f"or hardcoded-zero distances; got {recorded}"
    )


def test_top_similarity_is_not_recorded_when_no_vector_plane_hit_exists(
    instruments,
) -> None:
    """An all-record/computed result set has no cosine to report at all.

    Recording anything here would be inventing a similarity. Emitting nothing is
    correct: the count and plane-attribution metrics still describe the search.
    """
    m.record_search_quality(
        "memory.search",
        namespace="ns",
        result=_response(
            results=[
                {"distance": 0.02, "source": "record"},
                {"distance": 0.0, "source": "computed"},
            ]
        ),
    )
    assert instruments["_search_top_similarity"].calls == []
    # The search itself is still counted.
    assert instruments["_search_results"].calls


def test_search_trace_is_covered_not_silently_dropped(instruments, monkeypatch) -> None:
    """`memory.search_trace` must still be measured.

    The gate must not require `retrieval_risk`, which `MemorySearchTraceOutput`
    does not have; otherwise every search_trace response is silently dropped
    from all `loom.search.*` metrics and from span attribution.
    """
    from server.observability import otel as _otel

    spans: list[dict[str, Any]] = []
    monkeypatch.setattr(_otel, "set_search_attributes", lambda **kw: spans.append(kw))

    # The real trace output shape: results + embedding_model, no envelope.
    m.record_search_quality(
        "memory.search_trace",
        namespace="ns",
        result={
            "results": [{"distance": 0.42, "source": "fact"}],
            "embedding_model": "fake",
            "entity_filter": [],
        },
    )
    assert instruments["_search_results"].calls, "search_trace was dropped"
    assert spans, "search_trace got no span attribution"


def test_every_search_op_is_covered_by_the_shape_gate() -> None:
    """Guard the allowlist against a new search op silently vanishing.

    The gate names its ops explicitly because duck typing failed in both
    directions — `results` alone also matched memory.get_many/related, and
    requiring `retrieval_risk` dropped search_trace. The cost of an allowlist is
    that it can go stale, so assert it against the ops whose output model
    carries a retrieval envelope.
    """
    import server.memory.api.ops_search as ms

    # Enumerate by SHAPE, not by `retrieval_risk`. Keying on that field is
    # tautological here: memory.search_trace has no retrieval_risk, so that op
    # would not appear in the enumeration and the guard would pass whether or
    # not it was registered. A search op is one
    # whose output model returns a list of hits under `results`.
    search_like: set[str] = set()
    for op in vars(ms).values():
        name = getattr(op, "name", None)
        model = getattr(op, "output_model", None)
        if not isinstance(name, str) or model is None:
            continue
        field = getattr(model, "model_fields", {}).get("results")
        if field is None:
            continue
        ann = str(getattr(field, "annotation", ""))
        if ann.startswith("list["):
            search_like.add(name)

    assert "memory.search" in search_like and "memory.search_trace" in search_like, (
        f"the enumeration itself is broken; found {sorted(search_like)}"
    )
    missing = sorted(search_like - m._SEARCH_OPS)
    assert not missing, (
        f"these search-shaped ops are not in _SEARCH_OPS, so their telemetry is "
        f"silently dropped: {missing}"
    )


def test_workers_that_bind_a_cost_accumulator_also_drain_it() -> None:
    """`begin_op()` without a drain records nothing.

    It only gives `report_llm()` somewhere to write; something must then read the
    accumulator and emit an AgentEvent, or the spend stays invisible on every cost
    panel.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "server"
    offenders: list[str] = []
    for path in sorted((root / "workers").glob("*.py")):
        src = path.read_text()
        if "begin_op()" not in src:
            continue
        drains = (
            "emit_worker_cost_event_for" in src
            or "cost_accumulator.current()" in src
            or "emit_agent_event" in src
        )
        if not drains:
            offenders.append(path.name)
    assert not offenders, (
        f"these workers bind a cost accumulator that nothing ever reads, so "
        f"their LLM spend is not recorded anywhere: {offenders}"
    )


def test_code_lag_series_measures_commits_not_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A branch sitting exactly at its tip reads zero lag however old its last
    commit is: lag is computed in commits from the bare clone per configured
    repo, not as an age."""
    import server.operations  # noqa: F401
    from server.code.domain.index import repos as r
    from server.observability import otel_probes

    class _Cfg:
        def __init__(self, ns: str, branch: str) -> None:
            self.org, self.namespace, self.git_branch, self.git_dir = "o", ns, branch, "/bare"

    monkeypatch.setattr(
        r, "configured_repos", lambda: [_Cfg("code-x", "master"), _Cfg("code-x-26-1", "26.1")]
    )
    rows = [
        ("o", "code-x", "state:git_head", 0.0, "h1"),
        ("o", "code-x", "state:scip_commit", 0.0, "s1"),
        ("o", "code-x-26-1", "state:git_head", 0.0, "t261"),
        ("o", "code-x-26-1", "state:scip_commit", 0.0, "t261"),
        ("o", "code-x-26-1", "files_errored", 3.0, ""),
    ]
    tips = {"master": "tip1", "26.1": "t261"}
    counts = {("h1", "tip1"): 7, ("s1", "h1"): 120, ("t261", "t261"): 0}
    out = otel_probes._code_lag_series(
        rows,
        tip_of=lambda d, b: tips[b],
        lag_of=lambda d, a, b: (counts.get((a, b), -1), 1.0 if counts.get((a, b)) else 0.0),
    )
    by = {(k, a["loom.namespace"]): v for k, xs in out.items() for v, a in xs}
    assert by[("code_index_lag_commits", "code-x")] == 7.0
    assert by[("code_scip_lag_commits", "code-x")] == 120.0
    assert by[("code_index_lag_commits", "code-x-26-1")] == 0.0
    assert by[("code_scip_lag_commits", "code-x-26-1")] == 0.0


def test_memory_drop_counter_is_bounded_and_counts_n(instruments) -> None:
    m.record_memory_drop(namespace="ns", stage="extract.judge", reason="wrong", n=3)
    calls = instruments["_memory_drops"].calls
    assert calls == [(3, {"loom.namespace": "ns", "stage": "extract.judge", "reason": "wrong"})]
    for k in calls[0][1]:
        assert k in _ALLOWED_KEYS
    # No instrument -> silent no-op, never an exception.
    m._memory_drops = None
    m.record_memory_drop(namespace="ns", stage="x", reason="y")
