"""memory.set_from_messages surfaces pipeline outcomes instead of hiding them.

- An extractor failure inside the pipeline is a 502 (fail-closed), never a
  200 with `written=[]`.
- Fail-open step failures recorded in `WorkflowState.errors` (operator hook
  unreachable, fetch_existing failed, unknown step) are returned to the
  caller as `pipeline_errors`.

CH-free: every projection leg is stubbed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.api.models_bulk import MemorySetFromMessagesInput


def _stub_projections(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.extract.handler as mx
    from server.memory.app import bulk_set as _bulk_set_mod

    async def _no_evidence(input, msgs, **kw):
        return []

    async def _zero(*args, **kwargs):
        return 0

    async def _none(*args, **kwargs):
        return None

    monkeypatch.setattr(mx, "_write_source_evidence", _no_evidence)
    monkeypatch.setattr(mx, "_link_written_memories_to_evidence", _zero)
    monkeypatch.setattr(mx, "write_memory_chunks", _none)
    monkeypatch.setattr(mx, "_write_session_summary", _none)
    monkeypatch.setattr(mx, "_draft_session_summary", _none)
    monkeypatch.setattr(mx, "record_input_projection_failure", _none)
    monkeypatch.setattr(_bulk_set_mod, "record_input_projection_failure", _none)

    import server.identity.app.namespace_ontology as ont

    async def _cold(org, ns):
        return {}

    monkeypatch.setattr(ont, "load_namespace_ontology", _cold)


def _input() -> MemorySetFromMessagesInput:
    return MemorySetFromMessagesInput(
        org="dev",
        namespace="ns",
        agent="a",
        messages=[{"role": "user", "content": "I moved to Lisbon in 2021."}],
    )


@pytest.mark.asyncio
async def test_extraction_failure_in_pipeline_is_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx
    import server.memory.app.pipeline as pipe
    from server.memory.app.extract import ExtractionError

    _stub_projections(monkeypatch)

    async def _pipeline_raises(steps, state):
        raise ExtractionError("no JSON object in LLM response")

    monkeypatch.setattr(pipe, "run_pipeline", _pipeline_raises)

    with pytest.raises(HTTPException) as ei:
        await mx.handle_memory_set_from_messages(_input())
    assert ei.value.status_code == 502
    detail: Any = ei.value.detail
    assert detail["code"] == "EXTRACTION_FAILED"


@pytest.mark.asyncio
async def test_pipeline_errors_surface_on_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx
    import server.memory.app.pipeline as pipe

    _stub_projections(monkeypatch)

    async def _pipeline_with_soft_errors(steps, state):
        state.errors.append("unknown_step:loom.redact")
        state.errors.append("fetch_existing_failed:timeout")
        return state

    monkeypatch.setattr(pipe, "run_pipeline", _pipeline_with_soft_errors)

    out = await mx.handle_memory_set_from_messages(_input())
    assert out.written == []
    assert out.pipeline_errors == [
        "unknown_step:loom.redact",
        "fetch_existing_failed:timeout",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("projected", [True, False])
async def test_schema_mode_surfaces_stubbed_fields(
    monkeypatch: pytest.MonkeyPatch, projected: bool
) -> None:
    """Schema mode: fields the extractor gave up on are reported on the
    output, so "extracted" and "gave up" are distinguishable to the caller;
    so is a records projection skipped for a caller bounded to one user."""
    import server.memory.app.extract.handler as mx
    from server.llm.client import LLMResult
    from server.memory.app.extract import ExtractedRecord

    _stub_projections(monkeypatch)

    async def _fake_bulk(bulk_input):
        class _Out:
            written: list = []

        return _Out()

    async def _fake_inline(*args, **kwargs):
        return projected

    async def _fake_typed(msgs, schema, *, max_tokens=1024, observation_date=None):
        return (
            ExtractedRecord(record={"category": None, "amount": 4.5}, stubbed_fields=["category"]),
            LLMResult(text="", tokens_in=0, tokens_out=0, provider="p", model="m"),
        )

    monkeypatch.setattr(mx, "handle_memory_bulk_set", _fake_bulk)
    monkeypatch.setattr(mx, "_write_inline_record_projection", _fake_inline)
    monkeypatch.setattr("server.memory.app.extract.extract_typed_record_iterative", _fake_typed)
    out = await mx.handle_memory_set_from_messages(
        MemorySetFromMessagesInput(
            org="dev",
            namespace="ns",
            agent="a",
            messages=[{"role": "user", "content": "Spent 4.50 on snacks."}],
            memory_key="rec1",
            record_schema={"category": ["coffee", "lunch"], "amount": "number"},
        )
    )
    assert out.stubbed_fields == ["category"]
    assert bool(out.degraded) is not projected


@pytest.mark.asyncio
async def test_any_pipeline_failure_stops_the_digest_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest draft (an LLM call and an embed) runs beside the extractor; a pipeline
    failure that is not an ExtractionError must stop it too, not leave it spending."""
    import asyncio

    import server.memory.app.extract.handler as mx
    import server.memory.app.pipeline as pipe

    _stub_projections(monkeypatch)
    cancelled: list[bool] = []

    async def _slow_draft(*args, **kwargs):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    async def _pipeline_raises(steps, state):
        await asyncio.sleep(0)  # the draft has started
        raise RuntimeError("bulk write refused")

    monkeypatch.setattr(mx, "_draft_session_summary", _slow_draft)
    monkeypatch.setattr(pipe, "run_pipeline", _pipeline_raises)
    with pytest.raises(RuntimeError, match="bulk write refused"):
        await mx.handle_memory_set_from_messages(_input())
    await asyncio.sleep(0)
    assert cancelled == [True]


@pytest.mark.asyncio
async def test_schema_mode_reports_the_fallbacks_noted_during_the_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.memory.app.extract.handler as mx
    from server.llm.client import LLMResult
    from server.memory.app.extract import ExtractedRecord
    from server.observability import degradations

    _stub_projections(monkeypatch)
    monkeypatch.setattr("server.observability.provider_health.record_failure", lambda *a: None)
    degradations.begin()

    async def _fake_bulk(bulk_input):
        degradations.note("embed", "fake_vector_kept", "timeout")

        class _Out:
            written: list = []

        return _Out()

    async def _fake_inline(*args, **kwargs):
        return False

    async def _fake_typed(msgs, schema, *, max_tokens=1024, observation_date=None):
        return (
            ExtractedRecord(record={"amount": 4.5}),
            LLMResult(text="", tokens_in=0, tokens_out=0, provider="p", model="m"),
        )

    monkeypatch.setattr(mx, "handle_memory_bulk_set", _fake_bulk)
    monkeypatch.setattr(mx, "_write_inline_record_projection", _fake_inline)
    monkeypatch.setattr("server.memory.app.extract.extract_typed_record_iterative", _fake_typed)
    out = await mx.handle_memory_set_from_messages(
        MemorySetFromMessagesInput(
            org="dev",
            namespace="ns",
            agent="a",
            messages=[{"role": "user", "content": "Spent 4.50 on snacks."}],
            memory_key="rec1",
            record_schema={"amount": "number"},
        )
    )
    assert out.degraded[0] == "embed: fake_vector_kept (timeout)"
    assert out.degraded[1].startswith("records: projection_skipped")
