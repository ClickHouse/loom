"""Digest text is extractive, bounded and independent of model-generated prose."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.domain.summary_provenance import render_summary, source_text
from tests.unit import test_session_summaries
from tests.unit.test_memory_verify import _row

writer = test_session_summaries.writer


def test_renderer_preserves_source_text_without_inventing_facts() -> None:
    facts = ["Lives in Lisbon.", "Deployments use Tuesday and Thursday."]
    assert render_summary(facts) == "Lives in Lisbon.\nDeployments use Tuesday and Thursday."
    assert render_summary([]) == ""


def test_structured_source_values_have_a_stable_rendering() -> None:
    first = _row(value={"city": "Lisbon", "age": 30})
    second = _row(value={"age": 30, "city": "Lisbon"})
    now = datetime.now()
    assert (
        source_text(first, now=now)
        == source_text(second, now=now)
        == '{"age": 30, "city": "Lisbon"}'
    )


async def test_digest_generation_never_calls_a_model(writer: Any, monkeypatch: Any) -> None:
    from server.memory.app.session_summary import _write_session_summary

    def unexpected() -> Any:
        raise AssertionError("a model could introduce text without a source")

    monkeypatch.setattr("server.llm.client.get_llm_client", unexpected)
    await _write_session_summary(writer.input, writer.state)
    writer.ch.insert.assert_awaited_once()


@pytest.mark.parametrize("basis", ["created_at", "temporal_anchor", "valid_at"])
def test_a_source_cannot_lend_text_before_its_valid_time_lower_bound(basis):
    from datetime import timedelta

    now = datetime(2026, 1, 2)
    source = _row(value="a later fact", created_at=now)
    if basis != "created_at":
        source = source.model_copy(update={basis: now, "created_at": now - timedelta(days=2)})
    assert source_text(source, now=now, as_of=now - timedelta(days=1)) is None
