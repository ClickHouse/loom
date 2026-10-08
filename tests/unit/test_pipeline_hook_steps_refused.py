"""`memory.set_from_messages` makes no outbound HTTP call on a caller's behalf:
a `hook.*` pipeline step is refused at validation, and the pipeline itself treats
one as an unknown step rather than calling out."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import server.memory.app.pipeline as pipe
from server.memory.api.models_bulk import MemorySetFromMessagesInput
from server.memory.app import pipeline_state as pipeline_state


def _input(steps: list[dict[str, str]]) -> MemorySetFromMessagesInput:
    return MemorySetFromMessagesInput(
        org="dev",
        namespace="ns",
        agent="a",
        messages=[{"role": "user", "content": "hi"}],
        pipeline_steps=steps,
    )


def test_hook_step_is_a_validation_error() -> None:
    with pytest.raises(ValidationError, match="hook.pre_extract"):
        _input([{"step_id": "hook.pre_extract", "url": "http://169.254.169.254/"}])


def test_builtin_steps_still_validate() -> None:
    inp = _input([{"step_id": "loom.llm_extract"}, {"step_id": "loom.bulk_write"}])
    assert [s["step_id"] for s in inp.pipeline_steps] == ["loom.llm_extract", "loom.bulk_write"]


@pytest.mark.asyncio
async def test_pipeline_skips_a_hook_step_without_calling_out() -> None:
    state = pipeline_state.WorkflowState(
        org="dev", namespace="ns", agent="a", user_id="", session_id="", run_id=""
    )
    step = pipeline_state.PipelineStep(step_id="hook.x", config={"url": "http://127.0.0.1:8123/"})
    out = await pipe.run_pipeline([step], state)
    assert out.errors == ["unknown_step:hook.x"]
    assert not hasattr(pipe, "httpx")
