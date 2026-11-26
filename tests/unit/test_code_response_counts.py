"""Code delivery telemetry measures returned content without capturing it."""

from __future__ import annotations

import json

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from server.observability.code_response import code_response_counts, record_response_counts
from server.observability.event_args import project_result


def test_batched_bodies_and_file_lessons_are_counted_once_where_returned() -> None:
    result = {
        "results": [{"body": "abc", "body_elided": False}, {"body": "de", "body_elided": True}],
        "lessons": [
            {"memory_key": "private-key", "excerpt": "private note", "code_state": "fresh"},
            {"excerpt": "older", "code_state": "changed"},
        ],
    }
    before = json.dumps(result)
    counts = code_response_counts(result)
    assert counts["source_bodies"] == 2 and counts["source_chars"] == 5
    assert counts["elided_bodies"] == 1
    assert counts["lesson_rows"] == 2 and counts["lesson_chars"] == 17
    assert counts["lesson_fresh"] == 1 and counts["lesson_changed"] == 1
    assert counts["lesson_unclassified"] == 0
    assert json.dumps(result) == before
    projected = project_result(result, op_name="code.enclosing")
    assert projected["out_source_chars"] == "5"
    assert "private" not in json.dumps(projected)
    assert "out_lesson_rows" not in project_result(result, op_name="memory.search")


def test_grep_reports_returned_lines_and_disclosed_lesson_omissions() -> None:
    result = {
        "matches": [{"path": "private.py", "text": "private source"}, {"path": "private.py"}],
        "lessons_left_out": 5,
    }
    projected = project_result(result, op_name="code.grep")
    assert projected["out_grep_match_lines"] == "2"
    assert projected["out_lesson_rows_omitted"] == "5"
    assert "private" not in json.dumps(projected)
    assert project_result({"matches": []}, op_name="code.grep")["out_grep_match_lines"] == "0"
    assert "out_grep_match_lines" not in project_result(result, op_name="code.verify")


def test_grep_counts_related_source_separately_from_exact_matches() -> None:
    counts = code_response_counts(
        {
            "matches": [],
            "related": {
                "matches": [{}, {}],
                "definitions": [{"body": "related"}],
                "dependencies": [{"body": "helper"}],
            },
        },
        op_name="code.grep",
    )
    assert counts["grep_match_lines"] == 0 and counts["grep_related_lines"] == 2
    assert counts["source_bodies"] == 2 and counts["source_chars"] == 13


def test_read_counts_verified_windows_and_lessons_without_capturing_content() -> None:
    result = {
        "results": [
            {"windows": [{"text": "private source\n"}]},
            {"windows": [{"text": "other\n"}, {"text": ""}]},
            {"windows": []},
        ],
        "lessons": [{"excerpt": "private note", "code_state": "fresh"}],
    }
    projected = project_result(result, op_name="code.read")
    assert projected["out_source_windows"] == "2"
    assert projected["out_source_chars"] == "21"
    assert projected["out_source_bodies"] == "0"
    assert projected["out_lesson_fresh"] == "1"
    assert "private" not in json.dumps(projected)


def test_symbol_lessons_and_empty_or_elided_bodies_keep_their_meaning() -> None:
    counts = code_response_counts(
        {
            "definitions": [
                {"body": "source", "lessons": [{"excerpt": "a", "code_state": "moved"}]},
                {
                    "body": "",
                    "body_elided": True,
                    "lessons": [{"excerpt": "b", "code_state": "missing"}],
                },
                {"lessons": [{"excerpt": "c", "code_state": "unresolved"}, {"excerpt": "d"}]},
            ],
            "examples": [{"body": "This is not a returned definition"}],
        }
    )
    assert counts["source_bodies"] == 1 and counts["source_chars"] == 6
    assert counts["elided_bodies"] == 1 and counts["lesson_rows"] == 4
    assert counts["lesson_moved"] == counts["lesson_missing"] == counts["lesson_unresolved"] == 1
    assert counts["lesson_unclassified"] == 1


@pytest.mark.parametrize("result", [{}, {"matches": []}, {"results": "bad", "lessons": None}])
def test_absent_content_is_zero_not_a_claim_about_enrichment(result: dict) -> None:
    assert all(value == 0 for value in code_response_counts(result).values())


def test_delivery_counts_reach_the_existing_otel_span_without_io() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer(__name__).start_as_current_span("code-read"):
        record_response_counts(
            project_result(
                {"definitions": [{"body": "abc"}], "matches": [{}], "lessons_left_out": 2},
                op_name="code.grep",
            )
        )
    span = exporter.get_finished_spans()[0]
    assert span.attributes["loom.response.code.source_chars"] == 3
    assert span.attributes["loom.response.code.lesson_rows"] == 0
    assert span.attributes["loom.response.code.grep_match_lines"] == 1
    assert span.attributes["loom.response.code.lesson_rows_omitted"] == 2
    assert "abc" not in json.dumps(dict(span.attributes))
    provider.shutdown()


def test_no_recording_span_is_a_noop() -> None:
    with trace.use_span(trace.INVALID_SPAN):
        record_response_counts({"out_source_chars": "3"})
