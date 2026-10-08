"""set_from_messages says how it cut the session.

`segments` rides the output, and the output also names WHICH planner cut the
session; both fields are ledger identifiers, so the segmentation fire rate
is measurable.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import pipeline as mp
from server.memory.app import pipeline_state as pipeline_state


def _state(n_messages: int) -> Any:
    msgs = [{"role": "user", "content": f"turn {i}"} for i in range(n_messages)]
    return pipeline_state.WorkflowState(messages=msgs)


@pytest.mark.asyncio
async def test_whole_session_is_one_segment_named_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_size_segment_messages", 60)
    st = _state(10)
    segs = await mp._plan_segments(st)
    assert len(segs) == 1 and st.segmenter == "whole"


@pytest.mark.asyncio
async def test_size_fallback_names_size(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_size_segment_messages", 60)
    monkeypatch.setattr(settings, "memory_extract_size_segment_turns", 20)
    st = _state(100)
    segs = await mp._plan_segments(st)
    assert len(segs) > 1 and st.segmenter == "size"


def test_segments_and_segmenter_reach_the_ledger() -> None:
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.observability.event_args import project_result

    out = MemorySetFromMessagesOutput(segments=3, segmenter="size")
    projected = project_result(out)
    assert projected["out_segments"] == "3" and projected["out_segmenter"] == "size"
