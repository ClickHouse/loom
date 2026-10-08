"""A write with no observation date is anchored at the request
time, and the response says which."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory.domain.observation_date import resolve_observation_date


def test_caller_value_wins_and_is_trimmed() -> None:
    assert resolve_observation_date("  2023-05-30 ") == ("2023-05-30", "caller")
    assert resolve_observation_date("2023-05-30T10:00:00", default="none") == (
        "2023-05-30T10:00:00",
        "caller",
    )


def test_blank_defaults_to_the_request_date_or_nothing() -> None:
    now = datetime(2026, 9, 12, 14, 40, tzinfo=UTC)
    assert resolve_observation_date("", now=now) == ("2026-09-12", "request_time")
    assert resolve_observation_date(None, now=now) == ("2026-09-12", "request_time")
    assert resolve_observation_date("   ", default="none") == (None, "")
    assert resolve_observation_date("", default="NONE") == (None, "")


@pytest.mark.asyncio
async def test_set_from_messages_dates_the_facts_and_names_the_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.identity.app.namespace_ontology as ont
    import server.memory.app.extract.handler as mx
    import server.memory.app.pipeline as pipe
    from server.memory.api.models_bulk import MemorySetFromMessagesInput
    from server.memory.app import bulk_set as _bulk_set_mod
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_extract_observation_date_default", "request_time")
    seen: dict[str, Any] = {}

    async def _no_evidence(input: Any, msgs: Any, **kw: Any) -> list[str]:
        return []

    async def _pipeline(steps: Any, state: Any) -> Any:
        seen["observation_date"] = state.observation_date
        return state

    async def _none(*_: Any, **__: Any) -> None:
        return None

    async def _zero(*_: Any, **__: Any) -> int:
        return 0

    async def _cold(org: str, ns: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(mx, "_write_source_evidence", _no_evidence)
    monkeypatch.setattr(pipe, "run_pipeline", _pipeline)
    monkeypatch.setattr(mx, "_link_written_memories_to_evidence", _zero)
    monkeypatch.setattr(mx, "write_memory_chunks", _none)
    monkeypatch.setattr(mx, "_write_session_summary", _none)
    monkeypatch.setattr(mx, "_draft_session_summary", _none)
    monkeypatch.setattr(mx, "record_input_projection_failure", _none)
    monkeypatch.setattr(_bulk_set_mod, "record_input_projection_failure", _none)
    monkeypatch.setattr(ont, "load_namespace_ontology", _cold)

    out = await mx.handle_memory_set_from_messages(
        MemorySetFromMessagesInput(
            org="dev",
            namespace="ns",
            agent="a",
            messages=[{"role": "user", "content": "I moved to Lisbon."}],
        )
    )
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert seen["observation_date"] == today
    assert out.observation_date_source == "request_time"

    out2 = await mx.handle_memory_set_from_messages(
        MemorySetFromMessagesInput(
            org="dev",
            namespace="ns",
            agent="a",
            observation_date="2023-05-30",
            messages=[{"role": "user", "content": "I moved to Lisbon."}],
        )
    )
    assert seen["observation_date"] == "2023-05-30"
    assert out2.observation_date_source == "caller"

    monkeypatch.setattr(settings, "memory_extract_observation_date_default", "none")
    out3 = await mx.handle_memory_set_from_messages(
        MemorySetFromMessagesInput(
            org="dev",
            namespace="ns",
            agent="a",
            messages=[{"role": "user", "content": "I moved to Lisbon."}],
        )
    )
    assert seen["observation_date"] is None and out3.observation_date_source == ""


def test_source_rides_the_ledger() -> None:
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.observability.event_args import project_result

    out = MemorySetFromMessagesOutput(observation_date_source="request_time")
    assert project_result(out)["out_observation_date_source"] == "request_time"
