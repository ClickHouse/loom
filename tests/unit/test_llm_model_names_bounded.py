"""A model name a request or a namespace picks (`memory.ask`'s `mid_model`, a
rerank endpoint's `builtin:<provider>:<model>`) mints no metric series and
grows no process-lifetime map without bound."""

from __future__ import annotations

from typing import Any

import pytest

from server.llm import client as llm_client
from server.observability import cost_accumulator, metrics
from server.settings import settings


class _Counter:
    def __init__(self) -> None:
        self.points: list[dict[str, Any]] = []

    def add(self, _n: int, attributes: dict[str, Any]) -> None:
        self.points.append(attributes)


def test_model_label_keeps_listed_and_configured_models_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_model", "house-model-1")
    monkeypatch.setattr(settings, "llm_fallback_model", "house-fallback-1")
    assert metrics.model_label("gpt-4o-mini") == "gpt-4o-mini"  # priced
    assert metrics.model_label("house-model-1") == "house-model-1"
    assert metrics.model_label("house-fallback-1") == "house-fallback-1"
    assert metrics.model_label("anything a caller typed") == metrics.UNLISTED_MODEL
    assert metrics.model_label("") == metrics.UNLISTED_MODEL


def test_cache_and_fallback_counters_label_through_model_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, fallback = _Counter(), _Counter()
    monkeypatch.setattr(metrics, "_llm_response_cache", cache)
    monkeypatch.setattr(metrics, "_llm_fallback", fallback)
    metrics.record_llm_response_cache(model="caller-picked-9", outcome="miss")
    metrics.record_llm_fallback(from_model="caller-picked-9", to_model="gpt-4o", reason="http_503")
    assert cache.points == [{"loom.llm.model": "unlisted", "loom.llm.cache_outcome": "miss"}]
    assert fallback.points[0]["loom.llm.from_model"] == "unlisted"
    assert fallback.points[0]["loom.llm.to_model"] == "gpt-4o"


def test_override_clients_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_client, "_MAX_OVERRIDE_CLIENTS", 3)
    monkeypatch.setattr(llm_client, "_override_cache", {})
    monkeypatch.setattr(llm_client, "_wrapped_cache", {})
    monkeypatch.setattr(settings, "llm_fallback_model", "")
    for i in range(10):
        client = llm_client.get_llm_client_for_model(f"fake-picked-{i}")
        assert client.model == f"fake-picked-{i}"
        assert len(llm_client._override_cache) <= 3 and len(llm_client._wrapped_cache) <= 3
    # A cached model is served from the cache until it is dropped.
    again = llm_client.get_llm_client_for_model("fake-picked-9")
    assert again is llm_client.get_llm_client_for_model("fake-picked-9")


def test_unpriced_models_are_named_up_to_a_bound(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(cost_accumulator, "_UNPRICED_SEEN", set())
    monkeypatch.setattr(cost_accumulator, "_UNPRICED_NAMED_MAX", 2)
    with caplog.at_level("WARNING", logger=cost_accumulator.log.name):
        for i in range(5):
            cost_accumulator.llm_cost_usd(f"unpriced-{i}", 10, 10)
    assert cost_accumulator._UNPRICED_SEEN == {"unpriced-0", "unpriced-1"}
    lines = [r.getMessage() for r in caplog.records if "no price for model" in r.getMessage()]
    assert len(lines) == 2 and "further unpriced models are not named" in lines[1]
