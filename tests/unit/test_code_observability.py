"""Code-plane observability: bounded span attributes, the OTel<->trace-row
link, shunt outcomes, and the trace rows the graph/verify/grep/pack ops now
write. Everything here is CH-free and OTel-free (no SDK configured): the
helpers must be no-ops that never raise, and the handlers must call
schedule_code_trace with the right op/route regardless."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from server.code.app import common as cc
from server.code.app import spans
from server.settings import settings


@pytest.fixture(autouse=True)
def _intel_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


class _RecordingSpan:
    def __init__(self, recording: bool = True) -> None:
        self.attrs: dict[str, Any] = {}
        self._recording = recording

    def is_recording(self) -> bool:
        return self._recording

    def set_attribute(self, k: str, v: Any) -> None:
        self.attrs[k] = v


# --------------------------------------------------------------------------
# set_code_span_attributes / _bounded
# --------------------------------------------------------------------------


def test_span_attributes_are_bounded_scalars_under_a_closed_key_set() -> None:
    span = _RecordingSpan()
    with patch("opentelemetry.trace.get_current_span", return_value=span):
        spans.set_code_span_attributes(
            route="graph_callers",
            results=7,
            tokens_returned=1200,
            stale=False,
            lag_commits=-1,
            retrieval_risk="low",
            consumer="x" * 500,
            dispatch_seeds=[{"a": 1}, {"b": 2}],  # collections -> their size
            top_similarity=0.8123,
            abstain_code=None,  # None is skipped
            query="SELECT secret",  # not in the closed set -> dropped
            weird=object(),  # unknown key AND unsupported type -> dropped
        )
    a = span.attrs
    assert a["loom.code.route"] == "graph_callers"
    assert a["loom.code.results"] == 7 and isinstance(a["loom.code.results"], int)
    assert a["loom.code.stale"] is False
    assert a["loom.code.lag_commits"] == -1
    assert a["loom.code.dispatch_seeds"] == 2
    assert a["loom.code.top_similarity"] == pytest.approx(0.8123)
    assert len(a["loom.code.consumer"]) == 64
    assert "loom.code.abstain_code" not in a
    assert "loom.code.query" not in a and "loom.code.weird" not in a
    assert all(k.startswith("loom.code.") for k in a)
    assert all(isinstance(v, (bool, int, float, str)) for v in a.values())


def test_span_attributes_skip_non_recording_and_never_raise() -> None:
    span = _RecordingSpan(recording=False)
    with patch("opentelemetry.trace.get_current_span", return_value=span):
        spans.set_code_span_attributes(route="x")
    assert span.attrs == {}

    class _Boom:
        def is_recording(self) -> bool:
            raise RuntimeError("telemetry broke")

    with patch("opentelemetry.trace.get_current_span", return_value=_Boom()):
        spans.set_code_span_attributes(route="x")  # must not propagate
    # no SDK at all: the default API span is non-recording -> silent no-op
    spans.set_code_span_attributes(results=1, tokens_returned=float("nan"))


def test_bounded_sanitizes_nan_and_inf_floats() -> None:
    out = spans._bounded({"top_similarity": float("nan"), "lag_commits": True})
    assert out["top_similarity"] == 0.0
    assert out["lag_commits"] is True  # bools stay bools (checked before int)


def test_code_span_and_span_attrs_work_without_an_sdk() -> None:
    with spans.code_span("callers.hop", depth=1, seeds=3, query="dropped") as sp:
        spans.span_attrs(sp, edges=12)  # NonRecordingSpan or None: no-op
        spans.span_attrs(None, edges=12)
    with patch("server.observability.otel.traced", side_effect=RuntimeError("no otel")):
        with spans.code_span("x") as sp:  # falls back to a nullcontext
            assert sp is None


# --------------------------------------------------------------------------
# OTel id formatting + the trace-row link
# --------------------------------------------------------------------------


def test_otel_ids_are_w3c_hex_and_empty_when_invalid() -> None:
    t, s = spans.format_otel_ids(0x5B8AA5A2D2C872E8321CF37308D69DF2, 0x051581BF3B53BC3B)
    assert t == "5b8aa5a2d2c872e8321cf37308d69df2" and len(t) == 32
    assert s == "051581bf3b53bc3b" and len(s) == 16
    assert spans.format_otel_ids(0, 0) == ("", "")
    assert spans.format_otel_ids(1, 1) == ("0" * 31 + "1", "0" * 15 + "1")


def test_current_otel_ids_reads_the_recording_span_context() -> None:
    class _Ctx:
        is_valid = True
        trace_id = 0xABC
        span_id = 0x12

    class _Span(_RecordingSpan):
        def get_span_context(self) -> _Ctx:
            return _Ctx()

    with patch("opentelemetry.trace.get_current_span", return_value=_Span()):
        assert spans.current_otel_ids() == (format(0xABC, "032x"), format(0x12, "016x"))
    with patch("opentelemetry.trace.get_current_span", return_value=_Span(recording=False)):
        assert spans.current_otel_ids() == ("", "")
    assert spans.current_otel_ids() == ("", "")  # no SDK


def test_schedule_code_trace_writes_otel_ids_and_stamps_trace_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_trace_log_enabled", True)
    monkeypatch.setattr(cc, "current_otel_ids", lambda: ("t" * 32, "s" * 16))
    span = _RecordingSpan()
    inserted: list[Any] = []

    class _Client:
        class client:
            @staticmethod
            async def insert(
                table: str, rows: list[Any], *, column_names: list[str], settings: Any
            ):
                inserted.append((table, dict(zip(column_names, rows[0], strict=True))))

    async def go() -> None:
        with (
            patch("opentelemetry.trace.get_current_span", return_value=span),
            patch("server.core.ch.get_client", return_value=_Client()),
        ):
            cc.schedule_code_trace(
                org="o",
                ns="n",
                op="code.callers",
                query="f",
                route="graph_callers",
                returned_ids=["a"],
                returned_paths=["p"],
                returned_scores=[],
                top_k=1,
                tokens_returned=0,
                abstain=False,
                retrieval_risk="low",
                embedding_model="",
                started_monotonic=0.0,
                trace_run_id="run1",
            )
            await asyncio.sleep(0)
            await asyncio.gather(*cc._TRACE_TASKS)

    asyncio.run(go())
    assert span.attrs["loom.code.trace_run_id"] == "run1"
    table, row = inserted[0]
    assert table == "loom.code_search_trace"
    assert row["otel_trace_id"] == "t" * 32 and row["otel_span_id"] == "s" * 16
    assert row["trace_run_id"] == "run1" and row["op"] == "code.callers"


def test_a_trace_task_of_another_loop_is_dropped_when_the_next_is_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task left by a loop that is gone can never finish here; a later gather of the
    set would fail on it, so scheduling the next trace drops it."""
    monkeypatch.setattr(settings, "code_trace_log_enabled", True)

    class _Client:
        class client:
            @staticmethod
            async def insert(table: str, rows: list[Any], **_: Any) -> None:
                return None

    other = asyncio.new_event_loop()
    stale = other.create_task(asyncio.sleep(0))
    cc._TRACE_TASKS.add(stale)

    async def go() -> None:
        with patch("server.core.ch.get_client", return_value=_Client()):
            cc.schedule_code_trace(
                org="o",
                ns="n",
                op="code.callers",
                query="f",
                route="graph_callers",
                returned_ids=[],
                returned_paths=[],
                returned_scores=[],
                top_k=1,
                tokens_returned=0,
                abstain=False,
                retrieval_risk="low",
                embedding_model="",
                started_monotonic=0.0,
                trace_run_id="run2",
            )
            assert stale not in cc._TRACE_TASKS and cc._TRACE_TASKS
            await asyncio.gather(*cc._TRACE_TASKS)

    try:
        asyncio.run(go())
    finally:
        other.run_until_complete(stale)
        other.close()


# --------------------------------------------------------------------------
# code.outcome: shunt actions
# --------------------------------------------------------------------------


def test_outcome_validation_requires_trace_id_except_for_shunt_actions() -> None:
    from pydantic import ValidationError

    from server.code.api.models_outcome import CodeOutcomeInput

    ok = CodeOutcomeInput(action="shunt_hit", targets=["Read", "Bash"], tokens_saved=900)
    assert ok.trace_run_id == "" and ok.tokens_saved == 900
    CodeOutcomeInput(action="shunt_pass")
    CodeOutcomeInput(action="opened_file", trace_run_id="abc")
    for action in ("opened_file", "used_in_answer", "fell_back_grep", "ignored"):
        with pytest.raises(ValidationError):
            CodeOutcomeInput(action=action)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        CodeOutcomeInput(action="shunt_hit", tokens_saved=-1)
    with pytest.raises(ValidationError):
        CodeOutcomeInput(action="shunt_miss", trace_run_id="x")  # type: ignore[arg-type]


def test_outcome_shunt_row_carries_tokens_saved_and_skips_the_fold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_outcome import CodeOutcomeInput
    from server.code.app import outcome

    monkeypatch.setattr(settings, "code_trace_log_enabled", True)
    inserted: list[dict[str, Any]] = []
    counted: list[dict[str, Any]] = []

    class _Client:
        class client:
            @staticmethod
            async def insert(table, rows, *, column_names, settings):
                inserted.append(dict(zip(column_names, rows[0], strict=True)))

    async def _no_fold(*a: Any, **k: Any) -> None:
        raise AssertionError("shunt outcomes must not touch unit signals")

    monkeypatch.setattr(outcome, "_fold_outcome_signal", _no_fold)
    monkeypatch.setattr(outcome, "record_code_shunt", lambda **kw: counted.append(kw))
    with patch("server.core.ch.get_client", return_value=_Client()):
        out = asyncio.run(
            outcome.handle_code_outcome(
                CodeOutcomeInput(
                    action="shunt_hit",
                    targets=["Read", "Read", "Bash"],
                    tokens_saved=4200,
                    source="claude-code-shunt",
                )
            )
        )
    assert out.recorded is True
    assert inserted[0]["action"] == "shunt_hit"
    assert inserted[0]["tokens_saved"] == 4200 and inserted[0]["trace_run_id"] == ""
    assert counted == [
        {"decision": "hit", "count": 3, "tokens_saved": 4200, "namespace": "n", "reason": ""},
    ]
    assert inserted[0]["spans"] == ["", "", ""]  # padded to `targets`, aligned


# --------------------------------------------------------------------------
# A shunt pass names its reason; every outcome carries spans
# --------------------------------------------------------------------------


def test_outcome_pass_reason_is_the_source_from_a_fixed_vocabulary() -> None:
    from server.code.api.models_outcome import SHUNT_PASS_REASONS, CodeOutcomeInput

    assert SHUNT_PASS_REASONS == {
        "modified",
        "untracked",
        "targeted_span",
        "under_max_lines",
        "not_indexed",
        "binary",
        "single_file_grep",
        "file_grep_lines",  # One-file grep asking for lines, run as content
        "tree_content_grep",  # Tree-wide grep for matching lines
        "file_list_grep",  # Tree-wide grep listing files or counts
        "cap_spent",  # Read-window cap budget spent
        "symbol_grep_spent",  # Single-file symbol grep budget spent
    }
    # The plugin's vocabulary is the same list, in the same order of concern.
    plugin = (
        Path(__file__).resolve().parents[2]
        / "integrations"
        / "claude-code-plugin"
        / "lib"
        / "code-shunt.mjs"
    ).read_text()
    block = plugin.split("export const PASS_REASONS = Object.freeze([", 1)[1].split("]);", 1)[0]
    plugin_reasons = set(re.findall(r'^\s*"([a-z_]+)",', block, flags=re.M))
    assert plugin_reasons == SHUNT_PASS_REASONS
    for reason in sorted(SHUNT_PASS_REASONS):
        row = CodeOutcomeInput(action="shunt_pass", targets=["Read"], source=reason)
        assert row.pass_reason == reason
    # An older plugin (or a client of its own) is visible as such, never rejected.
    legacy = CodeOutcomeInput(action="shunt_pass", targets=["Grep"], source="claude-code-shunt")
    assert legacy.pass_reason == "unattributed" and legacy.source == "claude-code-shunt"
    # Hits and retrieval outcomes have no pass reason.
    assert CodeOutcomeInput(action="shunt_hit", source="claude-code-shunt").pass_reason == ""
    assert (
        CodeOutcomeInput(action="opened_file", trace_run_id="t", source="plugin_hook").pass_reason
        == ""
    )


def test_outcome_spans_normalise_and_never_reject() -> None:
    from server.code.api.models_outcome import CodeOutcomeInput, normalize_outcome_span

    assert normalize_outcome_span("src/a.cpp:120-160") == "src/a.cpp:120-160"
    assert normalize_outcome_span("120-160") == "120-160"
    assert normalize_outcome_span("src/a.cpp") == "src/a.cpp"  # file known, lines not
    assert normalize_outcome_span("") == "" and normalize_outcome_span(None) == ""
    assert normalize_outcome_span("src/a.cpp:160-120") == "src/a.cpp"  # end < start: lines unknown
    assert normalize_outcome_span("0-5") == ""  # 1-based
    assert normalize_outcome_span("a\tb:1-2") == "a b:1-2"  # TSV-safe
    assert normalize_outcome_span("x" * 700) == ""
    # A digit int() refuses ("²") leaves the lines unparsed, never raises.
    assert normalize_outcome_span("src/a.cpp:1-²") == "src/a.cpp:1-²"
    assert CodeOutcomeInput(action="shunt_hit", spans=["²-³"]).spans == ["²-³"]
    row = CodeOutcomeInput(
        action="shunt_pass",
        targets=["Read", "Read"],
        source="modified",
        spans=["src/a.cpp:1-900", 42],  # a non-string entry normalises, the array stays aligned
    )
    assert row.spans == ["src/a.cpp:1-900", "42"]
    assert len(CodeOutcomeInput(action="shunt_hit", spans=["1-2"] * 80).spans) == 50


def test_outcome_row_is_inserted_into_code_search_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """code.outcome writes ONE fire-and-forget row into code_search_outcome
    with the aligned spans."""
    from server.code.api.models_outcome import CodeOutcomeInput
    from server.code.app import outcome

    monkeypatch.setattr(settings, "code_trace_log_enabled", True)
    monkeypatch.setattr(outcome, "record_code_shunt", lambda **kw: None)
    calls: list[tuple[str, list[list[Any]], dict[str, Any]]] = []

    class _Recording:
        class client:
            @staticmethod
            async def insert(table: str, data: list[list[Any]], **k: Any) -> None:
                calls.append((table, data, k))

    with patch("server.core.ch.get_client", return_value=_Recording()):
        out = asyncio.run(
            outcome.handle_code_outcome(
                CodeOutcomeInput(
                    action="shunt_pass",
                    targets=["Read", "Bash:cat"],
                    tokens_saved=1234,
                    source="modified",
                    spans=["src/a.cpp:1-900"],
                )
            )
        )
    assert out.recorded is True
    [(table, data, kw)] = calls
    assert table == "loom.code_search_outcome"
    row = dict(zip(kw["column_names"], data[0], strict=True))
    assert row["targets"] == ["Read", "Bash:cat"] and row["spans"] == ["src/a.cpp:1-900", ""]
    assert row["action"] == "shunt_pass" and row["tokens_saved"] == 1234
    assert kw["settings"] == {"async_insert": 1, "wait_for_async_insert": 0}


def test_outcome_pass_reason_reaches_the_shunt_counter_and_the_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_outcome import CodeOutcomeInput
    from server.code.app import outcome

    monkeypatch.setattr(settings, "code_trace_log_enabled", False)  # no write: the labels alone
    counted: list[dict[str, Any]] = []
    attrs: list[dict[str, Any]] = []
    monkeypatch.setattr(outcome, "record_code_shunt", lambda **kw: counted.append(kw))
    monkeypatch.setattr(outcome, "set_code_span_attributes", lambda **kw: attrs.append(kw))
    asyncio.run(
        outcome.handle_code_outcome(
            CodeOutcomeInput(
                action="shunt_pass",
                targets=["Read"] * 4,
                tokens_saved=800,
                source="targeted_span",
            )
        )
    )
    assert counted == [
        {
            "decision": "pass",
            "count": 4,
            "tokens_saved": 800,
            "namespace": "n",
            "reason": "targeted_span",
        },
    ]
    assert attrs[0]["source"] == "targeted_span"


def test_freshness_status_word_and_the_as_of_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """`status` is the freshness envelope in one word (+7
    tokens measured), and the block is served from a 30 s per-namespace
    cache that code.refresh invalidates."""
    from datetime import UTC, datetime

    assert cc.freshness_status(0, False) == "fresh"
    assert cc.freshness_status(3, False) == "behind"
    assert cc.freshness_status(40, True) == "stale"
    assert cc.freshness_status(-1, False) == "unknown"

    calls: list[str] = []

    class _Repo:
        async def all_state(self, org: str, ns: str) -> dict[str, Any]:
            calls.append(ns)
            return {
                "git_head": {"value": "a" * 40, "updated_at": datetime(2026, 9, 14, tzinfo=UTC)}
            }

    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: None,
    )
    cc.invalidate_index_as_of("o", "ns1")
    first = asyncio.run(cc.index_as_of(_Repo(), "o", "ns1"))  # type: ignore[arg-type]
    assert first["commit"] == "a" * 40 and first["lag_commits"] == -1
    assert first["status"] == "unknown" and first["stale"] is False
    again = asyncio.run(cc.index_as_of(_Repo(), "o", "ns1"))  # type: ignore[arg-type]
    assert again == first and calls == ["ns1"]  # served from the cache: one state read
    again["status"] = "mutated"
    assert asyncio.run(cc.index_as_of(_Repo(), "o", "ns1"))["status"] == "unknown"  # type: ignore[arg-type]
    cc.invalidate_index_as_of("o", "ns1")
    asyncio.run(cc.index_as_of(_Repo(), "o", "ns1"))  # type: ignore[arg-type]
    assert calls == ["ns1", "ns1"]  # refresh invalidation re-reads
    cc.invalidate_index_as_of("o", "ns1")


def test_fold_skips_ops_whose_ids_are_not_unit_ids() -> None:
    from server.code.api.models_outcome import CodeOutcomeInput
    from server.code.app import outcome

    class _Repo:
        async def trace_by_run_id(self, org, ns, rid):
            return {
                "op": "code.grep",
                "returned_ids": ["src/a.cpp"],
                "returned_paths": ["src/a.cpp"],
                "abstain": 0,
            }

        async def add_unit_signals(self, org, ns, deltas):
            raise AssertionError("grep paths must not become unit signals")

    with patch.object(outcome, "CodeRepo", return_value=_Repo()):
        asyncio.run(
            outcome._fold_outcome_signal(
                "o",
                "n",
                CodeOutcomeInput(
                    trace_run_id="r",
                    action="opened_file",
                    targets=["src/a.cpp"],
                ),
            )
        )


def test_record_code_shunt_never_raises_without_a_meter_provider() -> None:
    cc.reset_shunt_counters_for_tests()
    cc.record_code_shunt(decision="hit", count=2, tokens_saved=100, namespace="n")
    cc.record_code_shunt(decision="bogus", count=0, tokens_saved=0, namespace="")
    cc.reset_shunt_counters_for_tests()


# --------------------------------------------------------------------------
# New trace call sites: callers / impact / verify / grep / context_pack
# --------------------------------------------------------------------------


def _sym(name: str, sid: str, path: str = "src/A.cpp", tier: str = "scip") -> dict[str, Any]:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "name": name.split("::")[-1],
        "kind": "function",
        "path": path,
        "start_line": 10,
        "end_line": 20,
        "tier": tier,
        "signature": "",
        "doc_comment": "",
    }


def _edge(frm: str, to: str, fq: str, tq: str, tier: str = "scip") -> dict[str, Any]:
    return {
        "from_symbol_id": frm,
        "to_symbol_id": to,
        "from_qualified_name": fq,
        "to_qualified_name": tq,
        "path": "src/X.cpp",
        "line": 5,
        "tier": tier,
        "edge_kind": "calls",
        "confidence": 1.0,
    }


class _GraphRepo:
    def __init__(self) -> None:
        self.symbols = {"DB::A::f": _sym("DB::A::f", "sA")}
        self.callers = {
            "sA": [
                _edge("sX", "sA", "DB::X::x", "DB::A::f"),
                _edge("sY", "sA", "DB::Y::y", "DB::A::f", tier="treesitter"),
            ]
        }

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        s = self.symbols.get(name)
        return [s] if s else []

    async def symbols_by_ids(self, org, ns, ids):
        return [s for s in self.symbols.values() if s["symbol_id"] in ids]

    async def edges_for_symbols(
        self, org, ns, ids, *, direction, edge_kinds=None, limit=200, min_confidence=0.0
    ):
        if direction != "callers":
            return []
        return [e for sid in ids for e in self.callers.get(sid, [])]

    async def functions_for_paths(self, org, ns, paths, *, limit=400, **_kw):
        return [s for s in self.symbols.values() if s["path"] in paths]

    async def test_map_for(self, org, ns, paths):
        return [{"test_path": "tests/t.cpp", "src_path": "src/A.cpp", "confidence": 0.9}]

    async def pr_history_for(self, org, ns, *, paths, limit):
        return []

    async def reviewers_by_pr(self, org, ns, pr_numbers):
        return {}

    async def top_reviewers(self, org, ns, *, days=180, limit=6):
        return []


def _trace_calls(monkeypatch: pytest.MonkeyPatch, module: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "schedule_code_trace", lambda **kw: calls.append(kw))
    return calls


def test_callers_writes_a_graph_trace_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol import graph as h

    calls = _trace_calls(monkeypatch, h)

    async def _no_dispatch(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(h, "_dispatch_seeds", _no_dispatch)
    with patch.object(h, "CodeRepo", return_value=_GraphRepo()):
        out = asyncio.run(
            h.handle_code_callers(
                CodeCallersInput(
                    name="DB::A::f",
                    consumer="clickgap",
                    agent="bot",
                    user_id="u1",
                )
            )
        )
    assert len(out.edges) == 2 and out.trace_run_id
    (c,) = calls
    assert c["op"] == "code.callers" and c["route"] == "graph_callers"
    assert c["returned_ids"] == ["sX", "sY"]  # far end of each edge
    assert c["returned_paths"] == ["src/X.cpp"]
    assert c["abstain"] is False and c["retrieval_risk"] == "medium"  # a treesitter edge
    assert c["trace_run_id"] == out.trace_run_id
    assert (c["consumer"], c["agent"], c["user_id"]) == ("clickgap", "bot", "u1")


def test_callers_ambiguous_refusal_still_traces_as_abstain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_symbol import CodeCallersInput
    from server.code.app.symbol import graph as h

    calls = _trace_calls(monkeypatch, h)
    monkeypatch.setattr(settings, "code_callers_max_distinct_seeds", 1)
    repo = _GraphRepo()
    repo.symbols["f"] = _sym("DB::A::f", "sA")

    async def many(org, ns, name, *, limit=50):
        return [_sym("DB::A::f", "sA"), _sym("DB::B::f", "sB")]

    repo.symbols_by_name = many  # type: ignore[method-assign]
    with patch.object(h, "CodeRepo", return_value=repo):
        out = asyncio.run(h.handle_code_callees(CodeCallersInput(name="f")))
    assert out.ambiguous is True and out.trace_run_id
    (c,) = calls
    assert c["op"] == "code.callees" and c["route"] == "graph_callees"
    assert c["abstain"] is True and c["retrieval_risk"] == "abstain"


def test_impact_traces_symbol_and_files_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_symbol import CodeImpactInput
    from server.code.app.symbol import impact as h

    calls = _trace_calls(monkeypatch, h)

    async def _no_dispatch(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(h, "_dispatch_seeds", _no_dispatch)
    with patch.object(h, "CodeRepo", return_value=_GraphRepo()):
        sym = asyncio.run(h.handle_code_impact(CodeImpactInput(name="DB::A::f")))
        files = asyncio.run(h.handle_code_impact(CodeImpactInput(files=["src/A.cpp"])))
        diff = asyncio.run(
            h.handle_code_impact(
                CodeImpactInput(
                    diff="--- a/src/A.cpp\n+++ b/src/A.cpp\n@@ -10,2 +12,3 @@ void f()\n+x\n"
                )
            )
        )
    assert [c["route"] for c in calls] == ["impact_symbol", "impact_files", "impact_diff"]
    assert all(c["op"] == "code.impact" for c in calls)
    assert calls[0]["returned_ids"] == ["sX", "sY"]
    assert calls[0]["query"] == "DB::A::f" and calls[1]["query"] == "src/A.cpp"
    assert sym.trace_run_id == calls[0]["trace_run_id"]
    assert files.trace_run_id and diff.trace_run_id
    assert len(sym.impacted_symbols) == 2


def test_verify_traces_verdict_per_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_verify import CodeVerifyInput
    from server.code.app.symbol import verify as h

    calls = _trace_calls(monkeypatch, h)
    with patch.object(h, "CodeRepo", return_value=_GraphRepo()):
        refuted = asyncio.run(
            h.handle_code_verify(
                CodeVerifyInput(
                    kind="unused",
                    subject="DB::A::f",
                )
            )
        )
    assert refuted.verdict == "refuted" and refuted.trace_run_id
    (c,) = calls
    assert c["op"] == "code.verify" and c["route"] == "verify_unused"
    assert c["returned_ids"] == ["refuted"] and c["returned_paths"] == ["src/X.cpp"]
    assert c["abstain"] is False and c["retrieval_risk"] == "low"

    repo = _GraphRepo()
    repo.callers = {}
    with patch.object(h, "CodeRepo", return_value=repo):
        floored = asyncio.run(
            h.handle_code_verify(
                CodeVerifyInput(
                    kind="unused",
                    subject="DB::A::f",
                )
            )
        )
    assert floored.verdict == "not_established"
    # The walk ran from a resolved seed and found no edge —
    # the graph's answer, traced as one, not as an abstain.
    assert floored.index_verdict == "no_static_callers"
    assert calls[-1]["abstain"] is False and calls[-1]["retrieval_risk"] == "low"
    assert calls[-1]["verdict"] == "no_static_callers"


def test_verify_refusal_writes_no_trace_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_verify import CodeVerifyInput
    from server.code.app.symbol import verify as h
    from server.core.errors import NotFoundError

    calls = _trace_calls(monkeypatch, h)
    repo = _GraphRepo()

    async def fuzzy(org, ns, needle, *, limit=25):
        return []

    repo.symbols_fuzzy = fuzzy  # type: ignore[attr-defined]
    with patch.object(h, "CodeRepo", return_value=repo), pytest.raises(NotFoundError):
        asyncio.run(h.handle_code_verify(CodeVerifyInput(kind="unused", subject="Nope::x")))
    assert calls == []


def test_grep_traces_literal_and_regex_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.api.models_grep import CodeGrepInput
    from server.code.app import grep

    calls = _trace_calls(monkeypatch, grep)

    async def _as_of(*a: Any, **k: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(grep, "index_as_of", _as_of)

    class _Repo:
        async def grep_chunks(self, org, ns, pattern, *, regex, case_sensitive, path_prefix, limit):
            return [
                {
                    "path": "src/A.cpp",
                    "start_line": 1,
                    "scope": "f",
                    "content": "int x;\nLOGICAL_ERROR here\n",
                },
                {
                    "path": "src/B.cpp",
                    "start_line": 40,
                    "scope": "g",
                    "content": "LOGICAL_ERROR again\n",
                },
            ]

    with patch.object(grep, "CodeRepo", return_value=_Repo()):
        lit = asyncio.run(grep.handle_code_grep(CodeGrepInput(pattern="LOGICAL_ERROR")))
        rex = asyncio.run(grep.handle_code_grep(CodeGrepInput(pattern="LOGICAL_.*", regex=True)))
    assert len(lit.matches) == 2 and lit.trace_run_id and rex.trace_run_id
    assert [c["route"] for c in calls] == ["grep_literal", "grep_regex"]
    assert all(c["op"] == "code.grep" for c in calls)
    assert calls[0]["returned_paths"] == ["src/A.cpp", "src/B.cpp"]
    assert calls[0]["returned_ids"] == calls[0]["returned_paths"]
    assert calls[0]["abstain"] is False and calls[0]["query"] == "LOGICAL_ERROR"


def test_context_pack_traces_precomputed_and_orientation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_context import CodeContextPackInput
    from server.code.app import context as hc

    calls = _trace_calls(monkeypatch, hc)

    class _Repo:
        async def context_pack(self, org, ns, pr):
            if pr == 7:
                return {"pack": "# PR 7 pack", "pack_tokens": 120, "merge_commit_sha": "abc"}
            return None

    async def _map(repo, org, ns, prefix, budget):
        return "# Repo map\n- **src/** (included by 3)"

    monkeypatch.setattr(hc, "_render_map", _map)
    with patch.object(hc, "CodeRepo", return_value=_Repo()):
        pre = asyncio.run(hc.handle_code_context_pack(CodeContextPackInput(pr_number=7)))
        orient = asyncio.run(hc.handle_code_context_pack(CodeContextPackInput()))
        missing = asyncio.run(hc.handle_code_context_pack(CodeContextPackInput(pr_number=99)))
    assert pre.source == "precomputed" and pre.trace_run_id
    assert orient.source == "orientation" and orient.trace_run_id
    assert missing.source == "unavailable" and missing.trace_run_id
    assert "PR #99" in missing.context and "# Repo map" not in missing.context
    assert missing.tokens_used < 100
    assert [c["route"] for c in calls] == [
        "pack_precomputed",
        "pack_orientation",
        "pack_unavailable",
    ]
    assert calls[0]["returned_ids"] == ["pr:7"] and calls[0]["tokens_returned"] == pre.tokens_used
    assert calls[0]["query"] == "pr:7" and calls[1]["query"] == "orientation"
    assert calls[2]["query"] == "pr:99" and calls[2]["abstain"] is True
    assert all(c["op"] == "code.context_pack" for c in calls)


def test_every_read_output_carries_trace_run_id() -> None:
    from server.code.api.models_context import CodeContextPackOutput, CodeMapOutput
    from server.code.api.models_grep import CodeGrepOutput
    from server.code.api.models_history import CodeHistoryOutput
    from server.code.api.models_navigation import CodeExamplesOutput, CodeTraceOutput
    from server.code.api.models_search import CodeAskOutput, CodeSearchOutput
    from server.code.api.models_symbol import CodeCallersOutput, CodeImpactOutput, CodeSymbolOutput
    from server.code.api.models_verify import CodeVerifyOutput

    for model in (
        CodeSearchOutput,
        CodeAskOutput,
        CodeSymbolOutput,
        CodeCallersOutput,
        CodeImpactOutput,
        CodeGrepOutput,
        CodeVerifyOutput,
        CodeTraceOutput,
        CodeContextPackOutput,
        CodeMapOutput,
        CodeExamplesOutput,
        CodeHistoryOutput,
    ):
        f = model.model_fields["trace_run_id"]
        assert f.annotation is str and f.default == "", model.__name__


def test_judge_excerpts_keep_code_unit_identity_when_body_is_elided() -> None:
    from server.code.domain.units import CodeUnit
    from server.observability.retrieval_judge import _excerpts

    hits = [
        CodeUnit(
            unit_id="1",
            path="src/a.cpp",
            start_line=10,
            end_line=40,
            symbol="DB::f",
            body="int f() { return 1; }",
        ),
        CodeUnit(
            unit_id="2",
            path="src/b.cpp",
            start_line=5,
            end_line=9,
            symbol="DB::g",
            body="",
            elided=True,
        ),
        {"value": "a memory hit"},
    ]
    out = _excerpts(hits, 5)
    assert len(out) == 3
    assert out[0].startswith("src/a.cpp:10-40 DB::f\n") and "int f()" in out[0]
    assert out[1] == "src/b.cpp:5-9 DB::g"
    assert out[2] == "a memory hit"
