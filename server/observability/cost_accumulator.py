"""Per-request upstream-cost accumulator.

A contextvar-bound dataclass that lets embedders / LLM clients report
tokens + dollars during one HTTP op without threading state through every
call. The transport binds a fresh accumulator at op-start and drains the
totals into the emitted AgentEvent's `upstream_cost_usd` + token fields.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# (input, output) USD per 1k tokens. Unlisted models bill $0 (the .get()
# default below); embedders bill input only.
PRICE_PER_1K_TOKENS: dict[str, tuple[float, float]] = {
    "text-embedding-3-small": (0.00002, 0.0),
    "text-embedding-3-large": (0.00013, 0.0),
    "text-embedding-ada-002": (0.0001, 0.0),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4o": (0.0025, 0.01),
    "gpt-4o-2024-08-06": (0.0025, 0.01),
    # GPT-4.1 family, OpenAI list prices (per 1k: 4.1 $2/$8 per M, mini
    # $0.40/$1.60, nano $0.10/$0.40).
    "gpt-4.1": (0.002, 0.008),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-4.1-nano": (0.0001, 0.0004),
    # GPT-5.x, OpenAI list prices from developers.openai.com/api/docs/pricing
    # (standard tier, <272K context): 5.4 $2.50/$15 per M, 5.5 $5/$30,
    # 5.4-mini $0.75/$4.50.
    "gpt-5.4": (0.0025, 0.015),
    "gpt-5.5": (0.005, 0.030),
    "gpt-5.4-mini": (0.00075, 0.0045),
    # GPT-6 family, OpenAI list prices (standard tier, <272K context): 6.1 sol
    # and 6 sol $2/$10 per M, 6 luna $0.10/$0.50, 6 astra $10/$50.
    "gpt-6-luna": (0.0001, 0.0005),
    "gpt-6-sol": (0.002, 0.010),
    "gpt-6.1-sol": (0.002, 0.010),
    "gpt-6-astra": (0.010, 0.050),
    "claude-haiku-4-5": (0.001, 0.005),
    "claude-haiku-4-5-20251001": (0.001, 0.005),
    "claude-sonnet-4-6": (0.003, 0.015),
    "claude-opus-4-8": (0.005, 0.025),
    # Claude 5 family, Anthropic first-party list prices (per 1k tokens:
    # fable/mythos 5.x $10/$50 per M, opus 5.5 $4/$20, opus 5 $5/$25,
    # sonnet 5 $2/$10).
    "claude-fable-5-1": (0.010, 0.050),
    "claude-mythos-5-1": (0.010, 0.050),
    "claude-fable-5": (0.010, 0.050),
    "claude-opus-5-5": (0.004, 0.020),
    "claude-opus-5": (0.005, 0.025),
    "claude-opus-4-7": (0.005, 0.025),
    "claude-opus-4-6": (0.005, 0.025),
    "claude-sonnet-5": (0.002, 0.010),
    "claude-sonnet-5-5": (0.002, 0.010),
    "fake-ngram-3072": (0.0, 0.0),
    "fake-ngram-1024": (0.0, 0.0),
    "fake-ngram-512": (0.0, 0.0),
    "fake-echo-1": (0.0, 0.0),
}


# An unlisted model bills $0 (the table's .get() default). Each such model is
# named once per process at WARNING so a week of $0 rows cannot pass for cheap.
# Bounded: a model name can come from a request (`memory.ask`'s `mid_model`),
# and past this many names the rest are not named.
_UNPRICED_SEEN: set[str] = set()
_UNPRICED_NAMED_MAX = 64


def _note_unpriced(model: str) -> None:
    if not model or model in PRICE_PER_1K_TOKENS or model in _UNPRICED_SEEN:
        return
    if len(_UNPRICED_SEEN) >= _UNPRICED_NAMED_MAX:
        return
    _UNPRICED_SEEN.add(model)
    log.warning(
        "no price for model %r: its calls bill $0 in agent_events.upstream_cost_usd and on "
        "spans until PRICE_PER_1K_TOKENS learns it%s",
        model,
        "; further unpriced models are not named"
        if len(_UNPRICED_SEEN) == _UNPRICED_NAMED_MAX
        else "",
    )


def llm_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """USD for one LLM call. Pure — shared with the GenAI span emitter.

    Extracted so the cost on a Langfuse generation and the cost summed into
    `agent_events.upstream_cost_usd` come from ONE pricing table. Duplicating
    the arithmetic is how the two numbers would drift apart, and the whole point
    of putting cost on the span is that an operator can trust both.
    """
    _note_unpriced(model)
    in_price, out_price = PRICE_PER_1K_TOKENS.get(model, (0.0, 0.0))
    return (max(0, input_tokens) / 1000.0) * in_price + (max(0, output_tokens) / 1000.0) * out_price


def embed_cost_usd(model: str, tokens: int) -> float:
    """USD for one embedding call. Embedders bill input only."""
    _note_unpriced(model)
    in_price, _ = PRICE_PER_1K_TOKENS.get(model, (0.0, 0.0))
    return (max(0, tokens) / 1000.0) * in_price


# Prompt-cache pricing relative to the input rate: a 5-minute cache write is
# 1.25x, a 1-hour write 2.0x, a read 0.1x. Fable/Mythos 5.1 read at a flat
# $0.25 per M ($0.00025 per 1k) rather than 0.1x. OpenAI's automatic prefix
# cache bills a cached prompt token at 0.5x and has no write charge. Pricing
# cached tokens at the full input rate overstates spend (opus by ~9%,
# gpt-4o-mini by ~34% over a week).
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0
CACHE_READ_MULT = 0.1
OPENAI_CACHE_READ_MULT = 0.5
CACHE_READ_PER_1K_OVERRIDE: dict[str, float] = {
    "claude-fable-5-1": 0.00025,
    "claude-mythos-5-1": 0.00025,
    # OpenAI's 0.5x cached-input rate holds for gpt-4o(-mini) only: the 4.1
    # family reads cached tokens at 0.25x and the 5.x family at 0.1x of the
    # input rate (list page) — flat per-1k figures here.
    "gpt-4.1": 0.0005,
    "gpt-4.1-mini": 0.0001,
    "gpt-4.1-nano": 0.000025,
    "gpt-5.4": 0.00025,
    "gpt-5.5": 0.0005,
    "gpt-5.4-mini": 0.000075,
    # GPT-6: 6.1 sol reads cached input at $0.10 per M, 6 sol at $0.20.
    "gpt-6.1-sol": 0.0001,
    "gpt-6-sol": 0.0002,
}
_OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-", "text-embedding")


def is_openai_model(model: str) -> bool:
    """Provider by model name: the price table is keyed by model, and the
    cache-read multiplier differs by provider."""
    return str(model or "").startswith(_OPENAI_PREFIXES)


def cache_read_price_per_1k(model: str) -> float:
    """USD per 1k cached prompt tokens read: the flat override, else the
    provider's multiplier on the input rate."""
    override = CACHE_READ_PER_1K_OVERRIDE.get(model)
    if override is not None:
        return override
    in_price, _ = PRICE_PER_1K_TOKENS.get(model, (0.0, 0.0))
    mult = OPENAI_CACHE_READ_MULT if is_openai_model(model) else CACHE_READ_MULT
    return in_price * mult


def turn_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_5m_tokens: int = 0,
    cache_creation_1h_tokens: int = 0,
) -> float:
    """USD for one agent turn from the raw usage counters a Messages API
    response carries (`input_tokens` is the UNCACHED share; cache reads and
    writes are separate). Same price table as `llm_cost_usd`, so the
    agent-side series and loom's own upstream series price alike. $0 for an
    unlisted model — callers check `is_priced` and record that separately."""
    _note_unpriced(model)
    in_price, out_price = PRICE_PER_1K_TOKENS.get(model, (0.0, 0.0))
    read_price = cache_read_price_per_1k(model)
    return (
        max(0, input_tokens) / 1000.0 * in_price
        + max(0, cache_read_tokens) / 1000.0 * read_price
        + max(0, cache_creation_5m_tokens) / 1000.0 * in_price * CACHE_WRITE_5M_MULT
        + max(0, cache_creation_1h_tokens) / 1000.0 * in_price * CACHE_WRITE_1H_MULT
        + max(0, output_tokens) / 1000.0 * out_price
    )


def is_priced(model: str) -> bool:
    """Whether this model has a price. Unlisted models silently bill $0.

    Callers use this to mark a generation's cost as unknown rather than as
    zero — a $0 cost on a real model reads as "free", which is worse than
    absent.
    """
    return model in PRICE_PER_1K_TOKENS


@dataclass
class _Accumulator:
    embed_tokens: int = 0
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    # Provider prompt-cache counters across the op's LLM calls: prompt tokens served from
    # the provider's cache and tokens written to it. Both are subsets of `llm_input_tokens`.
    llm_cache_read_tokens: int = 0
    llm_cache_write_tokens: int = 0
    # Per-LLM-model (calls, input, output, cost, cache_read, cache_write) in
    # first-call order, so the event row can name the model that carried the
    # op (agent_events.model) and a cascade's escalation shows as two models
    # in one op.
    by_model: dict[str, list[float]] = field(default_factory=dict)

    def dominant_model(self) -> str:
        """The LLM model with the largest cost share; ties (and all-$0 fake
        models) fall to the most calls, then first called. '' when no LLM
        call was made — embedders are deliberately not counted here."""
        if not self.by_model:
            return ""
        best, best_key = "", (-1.0, -1.0)
        for model, tally in self.by_model.items():
            calls, cost = tally[0], tally[3]
            key = (cost, calls)
            if key > best_key:
                best, best_key = model, key
        return best

    def drain(self) -> _Accumulator:
        """Return a snapshot and zero this accumulator in place, for a caller
        that emits an event mid-context and must not be double-counted by a
        later reader of the same accumulator (worker passes awaited inside a
        request)."""
        snap = _Accumulator(
            embed_tokens=self.embed_tokens,
            llm_input_tokens=self.llm_input_tokens,
            llm_output_tokens=self.llm_output_tokens,
            cost_usd=self.cost_usd,
            llm_calls=self.llm_calls,
            llm_cache_read_tokens=self.llm_cache_read_tokens,
            llm_cache_write_tokens=self.llm_cache_write_tokens,
            by_model={k: list(v) for k, v in self.by_model.items()},
        )
        self.embed_tokens = self.llm_input_tokens = self.llm_output_tokens = 0
        self.cost_usd, self.llm_calls = 0.0, 0
        self.llm_cache_read_tokens = self.llm_cache_write_tokens = 0
        self.by_model = {}
        return snap

    def add_embed(self, model: str, tokens: int) -> None:
        if tokens <= 0:
            return
        self.embed_tokens += tokens
        self.cost_usd += embed_cost_usd(model, tokens)

    def add_charge(self, model: str, cost_usd: float) -> None:
        """One call of `model` at a set price, with no tokens (`report_charge`)."""
        self.cost_usd += cost_usd
        self.llm_calls += 1
        tally = self.by_model.setdefault(model or "", [0, 0, 0, 0.0, 0, 0])
        tally[0] += 1
        tally[3] += cost_usd

    def add_llm(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read: int = 0,
        cache_write_5m: int = 0,
        cache_write_1h: int = 0,
    ) -> None:
        """One completion. `input_tokens` is the UNCACHED prompt share; the
        cache counters are billed at their own rates (`turn_cost_usd`) while
        `llm_input_tokens` — the ledger's `input_tokens` column and the
        prompt-cache hit ratio's denominator — keeps counting the whole
        prompt, so token series are unaffected by cache pricing and only the
        dollars move."""
        cache_read, cache_write_5m, cache_write_1h = (
            max(0, cache_read),
            max(0, cache_write_5m),
            max(0, cache_write_1h),
        )
        total_in = max(0, input_tokens) + cache_read + cache_write_5m + cache_write_1h
        if total_in <= 0 and output_tokens <= 0:
            return
        self.llm_input_tokens += total_in
        self.llm_output_tokens += max(0, output_tokens)
        self.llm_cache_read_tokens += cache_read
        self.llm_cache_write_tokens += cache_write_5m + cache_write_1h
        cost = turn_cost_usd(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_5m_tokens=cache_write_5m,
            cache_creation_1h_tokens=cache_write_1h,
        )
        self.cost_usd += cost
        self.llm_calls += 1
        tally = self.by_model.setdefault(model or "", [0, 0, 0, 0.0, 0, 0])
        tally[0] += 1
        tally[1] += total_in
        tally[2] += max(0, output_tokens)
        tally[3] += cost
        tally[4] += cache_read
        tally[5] += cache_write_5m + cache_write_1h


_current: contextvars.ContextVar[_Accumulator | None] = contextvars.ContextVar(
    "loom_cost_accumulator",
    default=None,
)


def begin_op() -> _Accumulator:
    """Bind a fresh accumulator to the current context and return it."""
    acc = _Accumulator()
    _current.set(acc)
    return acc


def current() -> _Accumulator | None:
    return _current.get()


def report_embed(model: str, tokens: int) -> None:
    acc = _current.get()
    if acc is None:
        return
    acc.add_embed(model, tokens)


def report_charge(model: str, cost_usd: float) -> None:
    """Charge one model call at a set price, for a call that cannot be priced from its
    tokens (the assistant's answer cut before its usage arrived, or on a model with no
    price): it counts as one call of `model` costing `cost_usd`, with no tokens."""
    acc = _current.get()
    if acc is None:
        return
    acc.add_charge(model, cost_usd)


def report_llm(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
) -> None:
    """Report one completion to the bound accumulator. `input_tokens` is the
    uncached prompt share; pass the provider's cache counters separately so
    they are priced at the cache rates. A caller with no
    cache breakdown passes the whole prompt as `input_tokens`."""
    acc = _current.get()
    if acc is None:
        return
    acc.add_llm(
        model,
        input_tokens,
        output_tokens,
        cache_read=cache_read,
        cache_write_5m=cache_write_5m,
        cache_write_1h=cache_write_1h,
    )
