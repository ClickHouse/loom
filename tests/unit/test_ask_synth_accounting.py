"""memory.ask's synthesis accounting: the answering model's calls, tokens,
provider prompt-cache shares and dollars, on the wire and the ledger. The
two-call ask environment here is shared with the prompt-block accounting
test."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit

_SELECTOR_MARK = "worth opening at full detail"


class _RecordingLLM:
    """Records (system, user) per call; answers the selector with a JSON pick
    and every synthesis with `text`."""

    provider = "fake"

    def __init__(self, text: str, model: str = "fake-llm") -> None:
        self._text = text
        self.model = model
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        if _SELECTOR_MARK in system:
            return SimpleNamespace(text=json.dumps({"picks": [0]}), model=self.model)
        return SimpleNamespace(text=self._text, model=self.model)

    def synth_calls(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if _SELECTOR_MARK not in c[0]]


def _hit(key: str, excerpt: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        rank_score=0.8,
    )


def make_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A two-call ask against fakes; the prompt-block accounting test builds
    the same environment."""
    import server.llm.client as llm_client
    import server.memory.app.deep_evidence as deep
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace()
    e.llm = _RecordingLLM("She met Samantha on 21 May 2023 [m1].")
    e.settings = settings

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return MemorySearchOutput(
            results=[_hit("m1", "met Samantha at the beach"), _hit("m2", "likes hiking")],
            embedding_model="fake-embed",
        )

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    async def fake_fetch(**kw: Any) -> list[Any]:
        return [
            deep.EvidenceTurn(
                memory_key="m1",
                role="user",
                observed_at="2023-05-21 10:00",
                content="I met Samantha at the beach outing",
            )
        ]

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(deep, "fetch_raw_evidence", fake_fetch)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    monkeypatch.setattr(settings, "memory_ask_deep_select_tier", "cheap")
    monkeypatch.setattr(settings, "memory_ask_consistency_check", False, raising=False)
    monkeypatch.setattr(settings, "memory_ask_precedents", False, raising=False)
    monkeypatch.setattr(settings, "memory_ask_llm_cache_default", False, raising=False)
    return e


async def _ask(**overrides: Any) -> Any:
    from server.memory.app.ask.handler import handle_memory_ask

    body: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "when did she meet Samantha",
        "decompose": "off",
        "deep_first": False,  # two calls: excerpt first, then deep
    }
    body.update(overrides)
    return await handle_memory_ask(MemoryAskInput(**body))


# ---- the ledger ---------------------------------------------------------------------


def test_accumulator_and_wire_carry_the_cache_counters() -> None:
    from server.memory.app.ask.handler import _synth_accounting
    from server.observability import cost_accumulator as ca

    acc = ca.begin_op()
    ca.report_llm("claude-opus-4-8", 400, 300, cache_read=0, cache_write_5m=6000)
    ca.report_llm("claude-opus-4-8", 500, 200, cache_read=6000, cache_write_5m=0)
    ca.report_llm("gpt-4o-mini", 100, 10)
    assert acc.llm_cache_read_tokens == 6000 and acc.llm_cache_write_tokens == 6000
    assert acc.by_model["claude-opus-4-8"][:3] == [2, 12900, 500]
    assert acc.by_model["claude-opus-4-8"][4:6] == [6000, 6000]
    out = _synth_accounting("claude-opus-4-8")
    assert out["synth_input_tokens"] == 12900
    assert out["synth_cached_input_tokens"] == 6000
    assert out["synth_cache_write_tokens"] == 6000
    assert out["synth_cost_usd"] == pytest.approx(
        ca.turn_cost_usd(
            "claude-opus-4-8", input_tokens=400, output_tokens=300, cache_creation_5m_tokens=6000
        )
        + ca.turn_cost_usd(
            "claude-opus-4-8", input_tokens=500, output_tokens=200, cache_read_tokens=6000
        ),
        rel=1e-6,
    )
    assert _synth_accounting("nobody")["synth_input_tokens"] == 0
    snap = acc.drain()
    assert snap.llm_cache_read_tokens == 6000 and acc.llm_cache_read_tokens == 0


def test_output_fields_reach_the_ledger_row() -> None:
    from server.memory.api.models_ask import MemoryAskOutput
    from server.observability.event_args import project_result

    out = MemoryAskOutput(
        answer="a",
        source="synthesized",
        synth_calls=2,
        synth_input_tokens=12900,
        synth_cached_input_tokens=6000,
        synth_cache_write_tokens=6000,
        synth_cost_usd=0.0123,
    )
    row = project_result(out)
    assert row["out_synth_calls"] == "2"
    assert row["out_synth_cached_input_tokens"] == "6000"
    assert row["out_synth_cost_usd"] == "0.0123"
