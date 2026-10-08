"""Which memory rows are raw trajectory transcripts: the tags and source traces
that mark them, as a Python test and as its SQL twin."""

from __future__ import annotations

from typing import Any

EVENT_TAG = "event"
FROM_OBSERVATION_TAG = "from_observation"
EVENT_SOURCE_TRACE = "observe.extract:event"
OBSERVATION_RECORD_TRACE = "observe.record"
RECORD_TAG = "observation"  # server.memory.app.observe.OBSERVATION_TAG


def is_transcript_row(tags: Any, source_trace: Any) -> bool:
    """A raw trajectory row: an `observe.record` slice (one tool call) or an
    event of server.memory.app.trajectory_notes ("Step 12/40 · After Bash `command=…`: … revealed
    content"). Its text is a tool transcript — evidence of a run, never a
    claim about the world — so it is not text-mined into the entity graph and
    not injected as recall. The facts and hints DISTILLED
    from a session (tag `from_observation`) are not
    transcript rows; search, run.get and memory.evidence still read every
    row."""
    trace = str(source_trace or "")
    if trace.startswith((OBSERVATION_RECORD_TRACE, EVENT_SOURCE_TRACE)):
        return True
    tag_set = {str(t).lower() for t in (tags or [])}
    return RECORD_TAG in tag_set or (EVENT_TAG in tag_set and FROM_OBSERVATION_TAG in tag_set)


# The SQL twin of `is_transcript_row`, over loom.memory's own columns.
TRANSCRIPT_ROW_SQL = (
    f"(startsWith(source_trace, '{OBSERVATION_RECORD_TRACE}')"
    f" OR startsWith(source_trace, '{EVENT_SOURCE_TRACE}')"
    f" OR has(tags, '{RECORD_TAG}')"
    f" OR (has(tags, '{EVENT_TAG}') AND has(tags, '{FROM_OBSERVATION_TAG}')))"
)
