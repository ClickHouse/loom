"""A code_search_trace row that can answer
the next round — the index commit and lag the op answered from, the abstain
reason, the answer-changing arguments, and the 422 path's error text."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.code.app import common as cc
from server.code.domain.index import verdicts
from server.observability.event_args import bounded_error, validation_error_text
from server.settings import settings

# ---- pure helpers ----------------------------------------------------------------


class _Inp:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def test_answer_args_copies_set_knobs_counts_id_lists_and_drops_unset() -> None:
    inp = _Inp(
        mode="files",
        regex=False,
        case_sensitive=True,
        path_globs=["src/**/*.cpp"],
        langs=[],
        exclude_unit_ids=["a", "b", "c"],
        limit=None,
        depth=2,
    )
    out = cc.answer_args(
        inp,
        "mode",
        "regex",
        "case_sensitive",
        "path_globs",
        "langs",
        "exclude_unit_ids",
        "limit",
        "depth",
        "missing",
        paged=None,
        widen=True,
    )
    assert out == {
        "mode": "files",
        "regex": False,
        "case_sensitive": True,
        "path_globs": ["src/**/*.cpp"],
        "exclude_unit_ids_n": 3,
        "depth": 2,
        "widen": True,
    }, "None, '' and [] are unset; False and 0 are values"
    assert cc.answer_args(_Inp(rerank="", top_k=0, expand=False), "rerank", "top_k", "expand") == {
        "top_k": 0,
        "expand": False,
    }
    # a raw 422 body (dict) reads the same way, values as the caller sent them
    assert cc.answer_args({"depth": "two", "files": "src/a.cpp"}, "depth", "files") == {
        "depth": "two",
        "files": "src/a.cpp",
    }


def test_bounded_args_json_is_compact_sorted_and_empty_for_nothing() -> None:
    assert cc.bounded_args_json(None) == "" and cc.bounded_args_json({}) == ""
    text = cc.bounded_args_json({"regex": True, "case_sensitive": False, "mode": "matches"})
    assert text == '{"case_sensitive":false,"mode":"matches","regex":true}'
    assert json.loads(text)["mode"] == "matches"


def test_bounded_args_json_trims_largest_values_first_and_stays_valid_json() -> None:
    big = {
        "path_globs": ["src/" + "x" * 190 + f"{i}" for i in range(20)],
        "mode": "files",
        "case_sensitive": True,
    }
    text = cc.bounded_args_json(big)
    assert len(text.encode()) <= cc.ARGS_JSON_CAP
    body = json.loads(text)
    assert "path_globs" not in body and body["_trimmed"] == 1
    assert body["mode"] == "files" and body["case_sensitive"] is True


def test_bounded_args_json_reduces_path_shaped_values_to_counts_when_redacted() -> None:
    text = cc.bounded_args_json(
        {
            "path_globs": ["src/a/**", "src/b/**"],
            "path_prefix": "src/",
            "langs": ["cpp"],
            "mode": "files",
        },
        redacted=True,
    )
    body = json.loads(text)
    assert body == {"path_globs_n": 2, "path_prefix_n": 1, "langs": ["cpp"], "mode": "files"}


def test_as_of_columns_read_the_index_block_and_default_to_unknown() -> None:
    assert cc.as_of_columns({"commit": "abc123", "lag_commits": 4}) == {
        "as_of_commit": "abc123",
        "lag_commits": 4,
    }
    assert cc.as_of_columns({}) == {"as_of_commit": "", "lag_commits": -1}
    assert cc.as_of_columns(None) == {"as_of_commit": "", "lag_commits": -1}
    assert cc.as_of_columns({"commit": "x", "lag_commits": "bad"}) == {
        "as_of_commit": "",
        "lag_commits": -1,
    }


def test_validation_error_text_names_the_field_first_and_bounds_like_the_ledger() -> None:
    errors = [
        {
            "type": "less_than_equal",
            "loc": ["depth"],
            "msg": "Input should be less than or equal to 2",
        },
        {"type": "list_type", "loc": ["files"], "msg": "Input should be a valid list"},
    ]
    text = validation_error_text(errors)
    assert text == (
        "depth: Input should be less than or equal to 2; files: Input should be a valid list"
    )
    assert cc.bounded_error_text(text).startswith("depth: Input should be")
    assert len(cc.bounded_error_text("x" * 500)) == 200
    assert cc.bounded_error_text("") == ""
    # the two ledgers agree: the same bound and redaction as agent_events.args.error
    assert (
        cc.bounded_error_text("mode: Input should be 'matches' or 'files'")
        == (bounded_error("mode: Input should be 'matches' or 'files'")[1])
    )


def test_impact_reason_names_why_the_radius_is_empty() -> None:
    assert verdicts.impact_reason("", ["treesitter_only_files"], []) == ""
    assert verdicts.impact_reason(
        "no_static_callers", ["treesitter_only_files", "ctor_edges_absent"], []
    ) == ("treesitter_only_files,ctor_edges_absent")
    assert verdicts.impact_reason("no_static_callers", [], []) == "no_edges_from_seeds"
    seen = [
        {"path": "t/x.sql", "class": "test"},
        {"path": "d.md", "class": "doc"},
        {"path": "t/y.sh", "class": "test"},
    ]
    assert verdicts.impact_reason("no_indexed_paths", [], seen) == "paths_doc+test"
    assert verdicts.impact_reason("no_indexed_paths", [], []) == "no_paths"
    assert verdicts.impact_reason("not_covered", [], []) == "paths_unindexed"
    assert verdicts.impact_reason("no_seeds_in_hunks", [], []) == "hunks_outside_functions"


# ---- the trace row ------------------------------------------------------------------


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch):
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    as_of_reads: list[tuple[str, str]] = []

    class _Inner:
        async def insert(self, table, data, column_names=None, settings=None):
            if errors:
                raise RuntimeError(errors.pop(0))
            rows.append(dict(zip(column_names, data[0], strict=True)))

    class _Client:
        client = _Inner()

    async def _as_of(_repo, org, ns):
        as_of_reads.append((org, ns))
        return {"commit": "61124de8", "lag_commits": 3, "stale": False, "status": "behind"}

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Client())
    monkeypatch.setattr(cc, "index_as_of", _as_of)
    monkeypatch.setattr(settings, "code_trace_log_enabled", True, raising=False)
    yield rows, errors, as_of_reads


def _schedule(**kw: Any) -> None:
    base: dict[str, Any] = dict(
        org="o",
        ns="n",
        op="code.grep",
        query="needle",
        route="grep_literal",
        returned_ids=[],
        returned_paths=[],
        returned_scores=[],
        top_k=200,
        tokens_returned=0,
        abstain=True,
        retrieval_risk="abstain",
        embedding_model="",
        started_monotonic=time.monotonic(),
        trace_run_id="run-1",
    )
    base.update(kw)
    cc.schedule_code_trace(**base)


async def _flush() -> None:
    await asyncio.gather(*list(cc._TRACE_TASKS))


@pytest.mark.asyncio
async def test_row_carries_as_of_from_the_cache_inside_the_write_task(capture) -> None:
    rows, _, reads = capture
    _schedule(args={"mode": "files", "case_sensitive": False}, reason="")
    assert reads == [], "the request path did not read the index state"
    await _flush()
    (row,) = rows
    assert reads == [("o", "n")]
    assert row["as_of_commit"] == "61124de8" and row["lag_commits"] == 3
    assert json.loads(row["args_json"]) == {"case_sensitive": False, "mode": "files"}
    assert row["reason"] == "" and row["error_code"] == "" and row["error_text"] == ""
    assert list(row) == cc.TRACE_COLUMNS


@pytest.mark.asyncio
async def test_a_handler_supplied_as_of_block_skips_the_read_and_empty_means_unknown(
    capture,
) -> None:
    rows, _, reads = capture
    _schedule(as_of={"commit": "deadbeef", "lag_commits": 0})
    _schedule(as_of={})
    await _flush()
    assert reads == []
    by_commit = {r["as_of_commit"]: r for r in rows}
    assert by_commit["deadbeef"]["lag_commits"] == 0
    assert by_commit[""]["lag_commits"] == -1


@pytest.mark.asyncio
async def test_row_defaults_leave_the_decidable_columns_empty(capture) -> None:
    rows, _, _ = capture
    _schedule()
    await _flush()
    (row,) = rows
    assert row["args_json"] == "" and row["reason"] == ""
    assert row["error_code"] == "" and row["error_text"] == ""
    assert list(row) == cc.TRACE_COLUMNS


@pytest.mark.asyncio
async def test_reason_and_verdict_travel_together_and_are_bounded(capture) -> None:
    rows, _, _ = capture
    _schedule(
        op="code.verify",
        verdict="no_static_callers",
        reason="no_edge_subject_not_compiler_indexed" + "x" * 100,
    )
    await _flush()
    (row,) = rows
    assert row["verdict"] == "no_static_callers"
    assert (
        row["reason"].startswith("no_edge_subject_not_compiler_indexed")
        and len(row["reason"]) == 64
    )


@pytest.mark.asyncio
async def test_a_restricted_namespace_row_keeps_knobs_and_drops_path_arguments(
    capture, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, _, _ = capture
    monkeypatch.setattr(
        "server.identity.app.namespace_classification.is_restricted_cached", lambda org, ns: True
    )
    _schedule(
        args={"mode": "files", "path_globs": ["src/Secret/**"], "case_sensitive": True},
        returned_paths=["src/Secret/a.cpp"],
        path_prefix="src/Secret",
    )
    await _flush()
    (row,) = rows
    assert row["redacted"] == 1 and row["returned_paths"] == [] and row["path_prefix"] == ""
    assert json.loads(row["args_json"]) == {
        "case_sensitive": True,
        "mode": "files",
        "path_globs_n": 1,
    }


@pytest.mark.asyncio
async def test_the_422_row_names_the_failing_field_the_knobs_and_a_query_digest(capture) -> None:
    rows, _, reads = capture
    body = {
        "org": "o",
        "namespace": "n",
        "consumer": "clickgap",
        "name": "DB::foo",
        "depth": 3,
        "files": ["a.cpp"],
        "diff": "--- a\n+++ b\n",
    }
    errors = [
        {
            "type": "less_than_equal",
            "loc": ["depth"],
            "msg": "Input should be less than or equal to 2",
            "input": 3,
        }
    ]
    cc.schedule_code_trace_validation_failure("code.impact", body, errors)
    await _flush()
    (row,) = rows
    assert reads == [], "a refused request names no index: no as_of read"
    assert row["op"] == "code.impact" and row["route"] == "validation_failed"
    assert row["error_code"] == "VALIDATION_FAILED"
    assert row["error_text"] == "depth: Input should be less than or equal to 2"
    assert (
        row["abstain"] == 1 and row["retrieval_risk"] == "error" and row["consumer"] == "clickgap"
    )
    assert row["query"].startswith("sha256:") and "DB::foo" not in row["query"]
    assert json.loads(row["args_json"]) == {"depth": 3, "files_n": 1, "diff_bytes": 12}
    assert row["as_of_commit"] == "" and row["lag_commits"] == -1


@pytest.mark.asyncio
async def test_the_422_row_is_written_for_code_ops_only(capture) -> None:
    rows, _, _ = capture
    cc.schedule_code_trace_validation_failure(
        "memory.search", {"org": "o"}, [{"loc": ["top_k"], "msg": "x"}]
    )
    cc.schedule_code_trace_validation_failure("code.grep", "not a body", [])  # type: ignore[arg-type]
    await _flush()
    assert rows == []


# ---- the transport --------------------------------------------------------------------


def test_http_422_emits_the_field_on_the_event_and_a_code_trace_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import http as http_mod
    from server.transport.http import build_router

    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: False)
    emitted: list[Any] = []
    traced: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    monkeypatch.setattr(http_mod, "_emit_event", lambda *a, **k: emitted.append((a, k)))
    monkeypatch.setattr(
        http_mod,
        "schedule_code_trace_validation_failure",
        lambda op, body, errs: traced.append((op, body, errs)),
    )
    app = FastAPI()
    app.include_router(build_router())
    c = TestClient(app)
    r = c.post(
        "/v1/code.impact", json={"org": "o", "namespace": "n", "name": "DB::foo", "depth": 3}
    )
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "VALIDATION_FAILED" and detail["errors"][0]["loc"] == ["depth"]
    ((args, kw),) = emitted[-1:]
    assert kw["error_code"] == "VALIDATION_FAILED"
    cls, msg = bounded_error(kw["error"])
    assert cls == "ValidationError" and msg.startswith(
        "depth: Input should be less than or equal to 2"
    )
    assert traced == [
        (
            "code.impact",
            {"org": "o", "namespace": "n", "name": "DB::foo", "depth": 3},
            detail["errors"],
        )
    ]
    # a non-code op still reaches the hook, which is where the code.* filter lives
    r = c.post("/v1/memory.search", json={"org": "o", "namespace": "n", "top_k": "many"})
    assert r.status_code == 422
    assert traced[-1][0] == "memory.search"
