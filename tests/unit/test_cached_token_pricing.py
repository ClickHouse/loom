"""The cost ledger prices cached tokens at the provider's cache
rates — Anthropic reads 0.1x / 5-minute writes 1.25x / 1-hour writes 2x,
OpenAI reads 0.5x — while the token columns keep counting the whole prompt."""

from __future__ import annotations

import contextlib
from typing import Any

import pytest

from server.observability import cost_accumulator as cost

OPUS_IN, OPUS_OUT = cost.PRICE_PER_1K_TOKENS["claude-opus-4-8"]
MINI_IN, MINI_OUT = cost.PRICE_PER_1K_TOKENS["gpt-4o-mini"]


def test_cache_read_price_is_provider_specific() -> None:
    assert cost.cache_read_price_per_1k("claude-opus-4-8") == pytest.approx(OPUS_IN * 0.1)
    assert cost.cache_read_price_per_1k("gpt-4o-mini") == pytest.approx(MINI_IN * 0.5)
    assert cost.cache_read_price_per_1k("claude-fable-5-1") == 0.00025  # flat override
    assert cost.cache_read_price_per_1k("nobody-9") == 0.0
    assert cost.is_openai_model("gpt-4o") and cost.is_openai_model("o3-mini")
    assert not cost.is_openai_model("claude-sonnet-5")


def test_report_llm_prices_the_split_and_counts_the_whole_prompt() -> None:
    acc = cost.begin_op()
    # an opus synthesis: 6,000 uncached + 1,400 cached read + 0 written, 300 out
    cost.report_llm("claude-opus-4-8", 6000, 300, cache_read=1400)
    assert acc.llm_input_tokens == 7400 and acc.llm_output_tokens == 300 and acc.llm_calls == 1
    expected = 6.0 * OPUS_IN + 1.4 * OPUS_IN * 0.1 + 0.3 * OPUS_OUT
    assert acc.cost_usd == pytest.approx(expected)
    before = cost.llm_cost_usd("claude-opus-4-8", 7400, 300)  # the uncached rate
    assert before - acc.cost_usd == pytest.approx(1.4 * OPUS_IN * 0.9)
    assert acc.by_model["claude-opus-4-8"][:4] == [1, 7400, 300, pytest.approx(expected)]


def test_cache_writes_bill_above_the_input_rate() -> None:
    acc = cost.begin_op()
    cost.report_llm("claude-opus-4-8", 0, 0, cache_write_5m=1000, cache_write_1h=1000)
    assert acc.llm_input_tokens == 2000 and acc.llm_calls == 1
    assert acc.cost_usd == pytest.approx(OPUS_IN * 1.25 + OPUS_IN * 2.0)


def test_openai_cached_share_bills_at_half() -> None:
    acc = cost.begin_op()
    # extraction: 21,500 prompt of which 20,000 cached, 400 out
    cost.report_llm("gpt-4o-mini", 1500, 400, cache_read=20000)
    assert acc.llm_input_tokens == 21500
    assert acc.cost_usd == pytest.approx(1.5 * MINI_IN + 20.0 * MINI_IN * 0.5 + 0.4 * MINI_OUT)


def test_a_caller_without_a_breakdown_is_priced_as_before() -> None:
    acc = cost.begin_op()
    cost.report_llm("claude-opus-4-8", 7400, 300)
    assert acc.cost_usd == pytest.approx(cost.llm_cost_usd("claude-opus-4-8", 7400, 300))
    assert cost.turn_cost_usd(
        "claude-opus-4-8", input_tokens=7400, output_tokens=300
    ) == pytest.approx(acc.cost_usd)


class _Response:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.status_code = 200
        self.text = ""

    def json(self) -> dict:
        return self._data

    def raise_for_status(self) -> None:
        pass


class _StubHTTP:
    def __init__(self, data: dict) -> None:
        self._data = data

    async def post(self, url: str, **kwargs: Any) -> _Response:
        return _Response(self._data)


class _Gen:
    def __init__(self, sink: dict) -> None:
        self.sink = sink

    def record_usage(self, tokens_in: int, tokens_out: int, **kw: Any) -> None:
        self.sink.update(tokens_in=tokens_in, tokens_out=tokens_out, **kw)

    def record_content(self, **kw: Any) -> None:
        pass

    def note_attempt(self) -> None:
        pass


async def test_anthropic_client_passes_the_split_to_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.llm import anthropic as anthropic_mod

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    data = {
        "content": [{"type": "text", "text": "hi"}],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 5,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 100},
        },
    }
    monkeypatch.setattr(anthropic_mod, "_http_client", lambda: _StubHTTP(data))
    span: dict = {}

    @contextlib.contextmanager
    def _span(**kw: Any):
        yield _Gen(span)

    monkeypatch.setattr("server.observability.genai.generation_span", _span)
    acc = cost.begin_op()
    result = await anthropic_mod.AnthropicLLMClient(model="claude-opus-4-8").complete(
        "be terse", "say hello", max_tokens=64
    )
    assert result.tokens_in == 1300 and result.cached_input_tokens == 900
    assert acc.llm_input_tokens == 1300 and acc.llm_output_tokens == 5
    assert acc.cost_usd == pytest.approx(
        0.1 * OPUS_IN
        + 0.9 * OPUS_IN * 0.1
        + 0.2 * OPUS_IN * 1.25
        + 0.1 * OPUS_IN * 2.0
        + 0.005 * OPUS_OUT
    )
    assert span["tokens_in"] == 1300 and span["cached_input_tokens"] == 900
    assert span["cache_write_5m_tokens"] == 200 and span["cache_write_1h_tokens"] == 100


async def test_openai_client_passes_the_cached_share_to_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.llm import openai as openai_mod

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    data = {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {
            "prompt_tokens": 21500,
            "completion_tokens": 400,
            "prompt_tokens_details": {"cached_tokens": 20000},
        },
    }
    monkeypatch.setattr(openai_mod, "_http_client", lambda: _StubHTTP(data))
    span: dict = {}

    @contextlib.contextmanager
    def _span(**kw: Any):
        yield _Gen(span)

    monkeypatch.setattr("server.observability.genai.generation_span", _span)
    acc = cost.begin_op()
    result = await openai_mod.OpenAILLMClient(model="gpt-4o-mini").complete(
        "sys", "user", max_tokens=64
    )
    assert result.tokens_in == 21500 and result.cached_input_tokens == 20000
    assert acc.llm_input_tokens == 21500
    assert acc.cost_usd == pytest.approx(1.5 * MINI_IN + 20.0 * MINI_IN * 0.5 + 0.4 * MINI_OUT)
    assert span["cached_input_tokens"] == 20000


def test_span_cost_agrees_with_the_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import genai

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, Any] = {}

        def set_attribute(self, k: str, v: Any) -> None:
            self.attrs[k] = v

    g = genai._Generation.__new__(genai._Generation)
    g._span = _Span()
    g._model = "claude-opus-4-8"
    g._attempts = 0
    g.record_usage(7400, 300, priced=True, cached_input_tokens=1400)
    acc = cost.begin_op()
    cost.report_llm("claude-opus-4-8", 6000, 300, cache_read=1400)
    assert g._span.attrs["gen_ai.usage.cost"] == pytest.approx(acc.cost_usd)
    assert g._span.attrs["gen_ai.usage.input_tokens"] == 7400


def test_gpt41_family_is_priced_and_an_unlisted_model_is_named_once(caplog) -> None:
    """The table knows the GPT-4.1 family; a model the table lacks still bills
    $0 but is named once per process at WARNING, so an unpriced model does not
    go unnoticed."""
    assert cost.llm_cost_usd("gpt-4.1-mini", 1_000_000, 100_000) == pytest.approx(0.4 + 0.16)
    assert cost.llm_cost_usd("gpt-4.1", 1_000_000, 0) == pytest.approx(2.0)
    assert cost.llm_cost_usd("gpt-4.1-nano", 0, 1_000_000) == pytest.approx(0.4)
    cost._UNPRICED_SEEN.discard("gpt-9-turbo-xl")
    with caplog.at_level("WARNING"):
        assert cost.llm_cost_usd("gpt-9-turbo-xl", 10_000, 1_000) == 0.0
        assert cost.llm_cost_usd("gpt-9-turbo-xl", 10_000, 1_000) == 0.0
        assert cost.embed_cost_usd("gpt-9-turbo-xl", 500) == 0.0
    hits = [r for r in caplog.records if "no price for model 'gpt-9-turbo-xl'" in r.getMessage()]
    assert len(hits) == 1  # once per process, not once per call
    with caplog.at_level("WARNING"):
        cost.llm_cost_usd("", 10, 10)
        cost.llm_cost_usd("gpt-4o-mini", 10, 10)
    assert not [r for r in caplog.records if "no price for model ''" in r.getMessage()]


def test_gpt5x_is_priced_and_the_41_and_5x_cached_reads_use_their_list_rates() -> None:
    """gpt-5.4 / 5.5 / 5.4-mini at list
    price; cached input for the 4.1 family is 0.25x and for 5.x 0.1x of the
    input rate, not OpenAI's generic 0.5x (which stays right for gpt-4o-mini)."""
    assert cost.llm_cost_usd("gpt-5.4", 1_000_000, 100_000) == pytest.approx(2.5 + 1.5)
    assert cost.llm_cost_usd("gpt-5.5", 1_000_000, 0) == pytest.approx(5.0)
    assert cost.llm_cost_usd("gpt-5.4-mini", 0, 1_000_000) == pytest.approx(4.5)
    assert cost.cache_read_price_per_1k("gpt-5.4") == pytest.approx(0.00025)
    assert cost.cache_read_price_per_1k("gpt-4.1") == pytest.approx(0.0005)
    assert cost.cache_read_price_per_1k("gpt-4.1-mini") == pytest.approx(0.0001)
    assert cost.cache_read_price_per_1k("gpt-4o-mini") == pytest.approx(0.00015 * 0.5)


def test_an_unpriced_model_on_the_llm_path_is_named_once(caplog) -> None:
    """LLM calls price through `turn_cost_usd` (add_llm), not `llm_cost_usd`;
    an unlisted model there must still be named once at WARNING."""
    cost._UNPRICED_SEEN.discard("gpt-10-unlisted")
    with caplog.at_level("WARNING"):
        acc = cost._Accumulator()
        acc.add_llm("gpt-10-unlisted", 1000, 100)
        acc.add_llm("gpt-10-unlisted", 1000, 100)
        assert cost.turn_cost_usd("gpt-10-unlisted", input_tokens=5, output_tokens=5) == 0.0
    hits = [r for r in caplog.records if "no price for model 'gpt-10-unlisted'" in r.getMessage()]
    assert len(hits) == 1
