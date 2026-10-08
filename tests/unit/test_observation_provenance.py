"""Rows the observe-extract worker derives from tool
observations carry `observed` provenance, and contrast reflections `derived`,
instead of the `asserted` a worker's memory.set stamped by default."""

from __future__ import annotations

from server.memory.app.trajectory_notes import HINT_SOURCE_TRACE
from server.memory.domain.provenance import class_from_trace, source_class_for
from server.memory.domain.transcript_rows import EVENT_SOURCE_TRACE


def test_step_events_and_hints_are_observed() -> None:
    assert class_from_trace(f"{EVENT_SOURCE_TRACE}:obs:a>obs:b") == "observed"
    assert class_from_trace(HINT_SOURCE_TRACE) == "observed"
    assert source_class_for(default="asserted", source_trace=HINT_SOURCE_TRACE) == "observed"


def test_contrast_reflections_are_derived() -> None:
    trace = "contrast:74b641a8-a0fd-4d85-b988-824d087620bb<06f05f17-ee9b-47a4"
    assert source_class_for(default="asserted", source_trace=trace) == "derived"


def test_other_traces_keep_their_class() -> None:
    assert class_from_trace("observe.record") == "observed"
    assert class_from_trace("pipeline:set_from_messages|prompt=x") == "extracted"
    assert source_class_for(default="asserted", source_trace="") == "asserted"
    assert source_class_for(default="asserted", source_trace="some:other") == "asserted"
