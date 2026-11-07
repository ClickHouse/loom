"""GenAI generation spans: stage labelling, usage, and coverage guards.

The contract these spans rely on in Langfuse: a span carrying
`gen_ai.request.model` is stored as `type=GENERATION` with `usage` and
`calculatedTotalCost` populated, and the Langfuse `traceId` is the OTel
`trace_id` verbatim. These tests pin the loom side of that contract.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from server.observability import genai

ROOT = Path(__file__).resolve().parents[2]


class _FakeSpan:
    def __init__(self) -> None:
        self.attrs: dict[str, Any] = {}

    def set_attribute(self, k: str, v: Any) -> None:
        self.attrs[k] = v


def test_stage_label_is_bounded() -> None:
    """An unknown stage collapses to `other` rather than minting a series.

    Same cardinality discipline as every other label in this package: the stage
    reaches a span attribute and would otherwise be caller-controlled.
    """
    with genai.llm_stage("hyde"):
        assert genai.current_stage() == "hyde"
    with genai.llm_stage("something-nobody-registered"):
        assert genai.current_stage() == "other"
    # Restored on exit, so the next call is not mislabelled.
    assert genai.current_stage() == "other"


@pytest.mark.asyncio
async def test_staged_decorator_resets_the_label() -> None:
    """The decorator must reset, not just set.

    A bare `_stage.set()` at function top leaks the label to whatever the same
    task calls next, silently mis-attributing the following generation — which
    is why this is a decorator and not an assignment.
    """

    @genai.staged("iterative_judge")
    async def _labelled() -> str:
        return genai.current_stage()

    assert await _labelled() == "iterative_judge"
    assert genai.current_stage() == "other"


def test_usage_and_cost_land_on_the_span(monkeypatch: pytest.MonkeyPatch) -> None:
    span = _FakeSpan()
    gen = genai._Generation(span, "gpt-4o-mini")
    gen.note_attempt()
    gen.note_attempt()
    gen.record_usage(1000, 500, priced=True)

    assert span.attrs["gen_ai.usage.input_tokens"] == 1000
    assert span.attrs["gen_ai.usage.output_tokens"] == 500
    # Retries are otherwise invisible: tenacity does up to 4 attempts and only
    # the last one's usage comes back.
    assert span.attrs["gen_ai.request.attempts"] == 2
    # Cost must match the accumulator's table exactly, or the Langfuse number and
    # agent_events.upstream_cost_usd will disagree for the same call.
    from server.observability.cost_accumulator import llm_cost_usd

    assert span.attrs["gen_ai.usage.cost"] == pytest.approx(llm_cost_usd("gpt-4o-mini", 1000, 500))


@pytest.mark.parametrize(
    ("module", "ok_body"),
    [
        (
            "server.llm.openai",
            {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 3}},
        ),
        (
            "server.llm.anthropic",
            {"content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 3}},
        ),
    ],
)
@pytest.mark.asyncio
async def test_providers_count_each_retry_attempt_on_the_span(
    monkeypatch: pytest.MonkeyPatch, module: str, ok_body: dict[str, Any]
) -> None:
    """A call that succeeds on its second attempt says so on its generation:
    tenacity returns only the last attempt's usage, so without the count a
    retried call looks exactly like a slow one."""
    import contextlib
    import importlib

    import httpx
    from tenacity import wait_none

    mod = importlib.import_module(module)
    span = _FakeSpan()

    @contextlib.contextmanager
    def _span(**kw: Any) -> Any:
        yield genai._Generation(span, kw["model"])

    class _Resp:
        status_code = 200
        text = ""

        def json(self) -> dict[str, Any]:
            return ok_body

    class _Client:
        calls = 0

        async def post(self, *_a: Any, **_kw: Any) -> _Resp:
            _Client.calls += 1
            if _Client.calls == 1:
                raise httpx.ConnectError("refused")
            return _Resp()

    monkeypatch.setattr(genai, "generation_span", _span)
    monkeypatch.setattr(mod, "_http_client", lambda: _Client())
    monkeypatch.setattr(mod, "_raise_for_status", lambda r, _label: None)
    monkeypatch.setattr("server.clients._retry.wait_exponential", lambda **_kw: wait_none())
    cls = mod.OpenAILLMClient if module.endswith("openai") else mod.AnthropicLLMClient
    client = cls.__new__(cls)
    client.model = "gpt-4o-mini" if module.endswith("openai") else "claude-haiku-4-5"
    client._api_key = "k"

    await client.complete("s", "u", max_tokens=16)

    assert _Client.calls == 2
    assert span.attrs["gen_ai.request.attempts"] == 2


def test_unpriced_model_is_marked_unknown_not_zero() -> None:
    """A $0 cost on a real model reads as "free", which is worse than a gap.

    `PRICE_PER_1K_TOKENS` silently defaults to (0.0, 0.0), so any model missing
    from it would otherwise publish a confident zero.
    """
    span = _FakeSpan()
    genai._Generation(span, "some-model-nobody-priced").record_usage(10, 5, priced=False)
    assert "gen_ai.usage.cost" not in span.attrs
    assert span.attrs["gen_ai.usage.cost_unknown"] is True


def test_every_model_a_setting_defaults_to_is_priced() -> None:
    """A default model missing from the price table bills $0 on every call."""
    import re

    from server.observability.cost_accumulator import is_priced
    from server.settings import Settings

    model_name = re.compile(r"^(claude-|gpt-|o\d|text-embedding-|gemini-)")
    unpriced = [
        f"{field}={name}"
        for field, info in Settings.model_fields.items()
        if isinstance(info.default, str)
        for name in (part.strip() for part in info.default.split(","))
        if model_name.match(name) and not is_priced(name)
    ]
    assert not unpriced, f"add these to PRICE_PER_1K_TOKENS: {unpriced}"


def test_generation_span_never_raises_without_otel() -> None:
    """Telemetry must not fail a completion, even with no tracer available."""
    with genai.generation_span(provider="openai", model="gpt-4o-mini") as gen:
        gen.note_attempt()
        gen.record_usage(1, 1, priced=True)


def test_every_llm_client_implementation_emits_a_generation() -> None:
    """Guard the coverage, not the instance.

    The whole point of instrumenting at the provider choke point is that every
    call site is covered by construction. A new provider that returns an
    `LLMResult` without opening a generation span would silently contribute no
    cost and no generation — the exact shape of bug this codebase keeps finding,
    so fail here instead of discovering it by querying Langfuse months later.
    """
    llm_dir = ROOT / "server" / "llm"
    offenders: list[str] = []
    for path in sorted(llm_dir.glob("*.py")):
        src = path.read_text()
        if "LLMResult(" not in src:
            continue  # not a provider implementation
        if path.name == "client.py":
            continue  # the Protocol + factories, not a caller of providers
        # `fake.py` is exempt: it makes no network call and synthesises usage as
        # len(text)//4, so emitting a generation would publish invented spend.
        if path.name == "fake.py":
            assert "generation_span" not in src, (
                "fake.py must NOT emit generations — its token counts are "
                "fabricated and would show as real spend"
            )
            continue
        # `cache.py` is exempt for the same reason: a hit is a
        # provider's earlier answer served without a call, so emitting a
        # generation would publish spend that did not happen; on a miss the
        # wrapped provider opens its own span.
        if path.name == "cache.py":
            assert "generation_span" not in src, (
                "cache.py must NOT emit generations — a served hit is not spend"
            )
            continue
        if "generation_span" not in src:
            offenders.append(path.name)
    assert not offenders, (
        f"these providers return an LLMResult without opening a generation "
        f"span, so their cost and tokens reach neither backend: {offenders}"
    )


def test_cache_hits_are_reported_as_cached_not_billed() -> None:
    """A cache hit must never look like spend.

    `_embed_one_raw` is the only place the provider's `usage` block is read, and
    it does not run on a hit, so a hit would report zero tokens and
    `embedding_tokens` would undercount. The tokens are counted locally, but they
    go on `loom.embed.cached_tokens` with a cost of 0: `report_embed` feeds
    `agent_events.embedding_tokens`, which `insights/app/cost.py` sums into a billing
    report, so counting a hit as billed would invent spend that never happened.
    """
    span = _FakeSpan()
    genai._Embedding(span, "text-embedding-3-small").record_usage(
        billed_tokens=0, cached_tokens=400, cache_hits=1, cache_misses=0
    )
    assert span.attrs["gen_ai.usage.input_tokens"] == 0
    assert span.attrs["gen_ai.usage.cost"] == 0.0
    assert span.attrs["loom.embed.cached_tokens"] == 400
    # Not a provider cache read: under a `gen_ai.usage.*` cache key both tools
    # would subtract it from the billed input.
    assert not [k for k in span.attrs if k.startswith("gen_ai.usage.cache")]
    assert span.attrs["loom.embed.cost_saved_usd"] > 0.0
    assert span.attrs["loom.embed.cache_hits"] == 1


def test_embedders_do_not_feed_cached_tokens_into_the_cost_accumulator() -> None:
    """Guard the invariant, not just the current call sites.

    The tempting "fix" for the cache-hit gap is to call `report_embed` with a
    synthesized count. That silently inflates every namespace's cost report and
    the 24h LLM budget cap that `entity_extractor` enforces, so pin it: the only
    `report_embed` calls may be the two that read a real provider `usage` block.
    """
    src = (ROOT / "server" / "embedding" / "openai.py").read_text()
    for line in src.splitlines():
        if "report_embed(" in line and "import" not in line:
            assert line.strip() == "report_embed(model, tokens)", (
                f"report_embed must only ever be passed a token count parsed "
                f"from the provider response; got: {line.strip()!r}"
            )


def test_every_llm_complete_call_site_is_stage_labelled() -> None:
    """No caller may reach a provider without saying which leg it is.

    An unlabelled call lands in the `other` bucket, and `other` is not a
    diagnosis — the entity_extractor worker sat there producing the single
    largest share of all generations while looking like noise. Every new caller
    must pick a stage, so this fails at the point the caller is added rather
    than months later on a dashboard.
    """
    exempt = {
        # The provider implementations themselves, and the fake.
        "server/llm/openai.py",
        "server/llm/anthropic.py",
        "server/llm/fake.py",
        "server/llm/client.py",
    }
    unlabelled: list[str] = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in exempt:
            continue
        src = path.read_text()
        # Repository `complete(...)` methods are unrelated and happen to share
        # the name; require an LLM-ish receiver.
        calls = re.findall(r"await (?:\w+\.)?(\w+)(?:\(\))?\.complete\(", src)
        if not any(c in ("llm", "client", "get_llm_client") for c in calls):
            continue
        if "llm_stage(" not in src and "@staged(" not in src:
            unlabelled.append(rel)
    assert not unlabelled, (
        f"these modules call an LLM provider without ever setting a stage, so "
        f"their generations report as `other`: {unlabelled}"
    )


def test_search_llm_helpers_are_all_stage_labelled() -> None:
    """Every helper that calls a provider must carry a stage.

    Without one the generation is named after the model, so a latency spike
    cannot be attributed to HyDE vs rerank vs judge — which is the question the
    span exists to answer.
    """
    src = (ROOT / "server" / "memory" / "app" / "search_llm.py").read_text()
    # Public + private async helpers that issue a completion.
    callers = re.findall(r"^(?:@staged\([^)]*\)\n)?async def (\w+)", src, re.M)
    labelled = set(re.findall(r'@staged\("[a-z_]+"\)\nasync def (\w+)', src))
    unlabelled = [
        name
        for name in callers
        if name not in labelled and re.search(rf"async def {name}\b.*?\.complete\(", src, re.S)
    ]
    # Only assert over helpers that really do call .complete()
    truly_unlabelled = []
    for name in unlabelled:
        body = src.split(f"async def {name}", 1)[1]
        nxt = body.find("\nasync def ")
        if ".complete(" in (body[:nxt] if nxt > 0 else body):
            truly_unlabelled.append(name)
    assert not truly_unlabelled, (
        f"these search-LLM helpers call a provider without a @staged label, so "
        f"their generations are unattributable: {truly_unlabelled}"
    )


def test_cache_hit_is_not_an_embedding_observation() -> None:
    """A cache hit made no provider call, so it is a SPAN, not an EMBEDDING.

    Marking every embed an embedding would put cache hits in Langfuse as
    embedding calls with 0 tokens and $0, as `traced(..., model=...)` would without
    the same rule. Empty generations inflate the count and drag every
    per-generation average down by exactly the cache hit rate.
    """
    span = _FakeSpan()
    genai._Embedding(span, "text-embedding-3-small").record_usage(
        billed_tokens=0, cached_tokens=400, cache_hits=1, called_provider=False
    )
    assert span.attrs["langfuse.observation.type"] == "span"

    span2 = _FakeSpan()
    genai._Embedding(span2, "text-embedding-3-small").record_usage(
        billed_tokens=400, cache_misses=1, called_provider=True
    )
    assert span2.attrs["langfuse.observation.type"] == "embedding"


def test_embedding_span_defers_observation_type() -> None:
    """It cannot be set at span start: cache-hit status is not known until after.

    A hard-coded "generation" at span creation is what produced the $0 rows, so
    guard that the span helper does not reintroduce it.
    """
    src = (ROOT / "server" / "observability" / "genai.py").read_text()
    body = src.split("def embedding_span(", 1)[1].split("\ndef ", 1)[0]
    assert 'langfuse.observation.type", "generation"' not in body, (
        "embedding_span must not declare the observation type up front — a cache "
        "hit is not a generation, and that is unknown until the call returns"
    )


def test_billed_tokens_come_from_the_provider_not_an_estimate() -> None:
    """The span's cost and the ClickHouse billing figure must share one input.

    `embed()` routes through `cached_embed`, so the provider's `usage` block is
    only reachable if `_embed_one_raw` hands it back. Passing a local
    `_count_tokens` estimate as `billed_tokens` made the two backends disagree for
    the same call — and `_count_tokens` silently degrades to `len(text)//4 + 1`
    when tiktoken is missing, so the divergence could be large and invisible.
    """
    src = (ROOT / "server" / "embedding" / "openai.py").read_text()
    body = src.split("async def embed(self, text: str)", 1)[1].split("\n    async def", 1)[0]
    assert "billed_tokens=0 if was_hit else self._last_billed_tokens" in body, (
        "billed_tokens must be the provider's usage count, not _count_tokens"
    )
    # The estimate may only feed the cache-savings figure.
    assert "cached_tokens=self._count_tokens(text) if was_hit else 0" in body


def _recording_tracer(monkeypatch: pytest.MonkeyPatch) -> Any:
    from opentelemetry import trace as _trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(_trace, "get_tracer", lambda name, *a, **k: provider.get_tracer(name))
    return exporter


@pytest.mark.parametrize("which", ["generation", "embedding"])
def test_a_failed_provider_call_is_an_error_span_without_its_message(
    monkeypatch: pytest.MonkeyPatch, which: str
) -> None:
    """Error views key on span status: a failed LLM or embed call must read as
    ERROR there, naming the exception type but not its text (a provider
    reply can quote the request)."""
    from opentelemetry.trace import StatusCode

    exporter = _recording_tracer(monkeypatch)
    cm = (
        genai.generation_span(provider="openai", model="gpt-4o-mini")
        if which == "generation"
        else genai.embedding_span(provider="openai", model="text-embedding-3-small")
    )
    with pytest.raises(RuntimeError), cm:
        raise RuntimeError("provider said: secret prompt text")
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "RuntimeError"
    assert "secret" not in (span.status.description or "")
    assert not span.events
    assert "gen_ai.latency_ms" in span.attributes


def test_a_cancelled_provider_call_is_not_an_error_span(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from opentelemetry.trace import StatusCode

    exporter = _recording_tracer(monkeypatch)
    with pytest.raises(asyncio.CancelledError), genai.generation_span(provider="openai", model="m"):
        raise asyncio.CancelledError
    (span,) = exporter.get_finished_spans()
    assert span.status.status_code is StatusCode.UNSET


def test_provider_spans_carry_the_genai_semantic_convention_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HyperDX's LLM view and Langfuse key on these: the provider under
    `gen_ai.provider.name`, the operation as a convention value (`chat`,
    `embeddings`), and the cascade leg apart from it under `loom.llm.stage`."""
    exporter = _recording_tracer(monkeypatch)
    with (
        genai.llm_stage("hyde"),
        genai.generation_span(provider="openai", model="gpt-4o-mini") as g,
    ):
        g.record_usage(1000, 50, priced=True, cached_input_tokens=600, cache_write_5m_tokens=100)
    with (
        genai.llm_stage("hyde"),
        genai.embedding_span(provider="openai", model="text-embedding-3-small") as e,
    ):
        e.record_usage(billed_tokens=30, cache_misses=1)
    chat, embed = exporter.get_finished_spans()
    assert chat.name == "llm.hyde"
    assert chat.attributes["gen_ai.provider.name"] == "openai"
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["loom.llm.stage"] == "hyde"
    assert chat.attributes["langfuse.observation.type"] == "generation"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 1000
    assert chat.attributes["gen_ai.usage.cache_read.input_tokens"] == 600
    assert chat.attributes["gen_ai.usage.cache_creation.input_tokens"] == 100
    assert embed.attributes["gen_ai.operation.name"] == "embeddings"
    assert embed.attributes["langfuse.observation.type"] == "embedding"
    for span in (chat, embed):
        assert "gen_ai.system" not in span.attributes
        assert "gen_ai.operation.kind" not in span.attributes
