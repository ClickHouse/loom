"""GenAI semantic-convention spans for LLM and embedding calls.

One span per provider call, annotated so BOTH backends understand it without a
second instrumentation path:

  * ClickStack gets an ordinary child span in the trace waterfall, so
    "where did the latency go" finally includes the LLM legs.
  * Langfuse renders it as a **GENERATION** (an LLM call) or an
    **EMBEDDING** from `langfuse.observation.type`, and reads `gen_ai.usage.*`
    as token usage and `gen_ai.usage.cost` as cost.
  * HyperDX's LLM view finds it by the `gen_ai.*` keys and reads the provider
    (`gen_ai.provider.name`), the operation (`chat`, `embeddings`), the token
    split and the cost from the same attributes.

The keys, and the operation values, are the OpenTelemetry GenAI semantic
conventions'; the provider value is Loom's own provider name (`openai`,
`anthropic`, `gemini`, ...). The cascade leg that made the call is
`loom.llm.stage` and the span name (`llm.<stage>`, `embed.provider`).

That mapping is verified, not assumed: a probe span carrying
`gen_ai.request.model`, `gen_ai.usage.input_tokens/output_tokens` and
`gen_ai.usage.cost` came back from `GET /api/public/traces/<otel-hex>` as
`type=GENERATION` with `usage={input,output,total}` and
`calculatedTotalCost` populated, and the Langfuse `traceId` was the OTel
`trace_id` verbatim (observation id was the span id).

Why here and not in a Langfuse SDK wrapper: loom talks to providers with raw
`httpx`, so SDK auto-instrumentation would capture nothing, and the Langfuse v2
client pinned in pyproject has no context propagation — a trace handle cannot
reach `server/memory/app/search_llm.py`. Annotating the OTel span loom already
has in scope avoids both problems.

Cost comes from `cost_accumulator`'s pricing table via `llm_cost_usd`, so the
number on the generation and the number summed into
`agent_events.upstream_cost_usd` cannot drift apart.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import hashlib
import json
import logging
import pathlib
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any, ParamSpec, TypeVar

from server.observability.capture_consent import global_default
from server.settings import settings

log = logging.getLogger(__name__)

# The decorator must PRESERVE the decorated signature. An `Any`-returning
# decorator erases it, and strict mypy then reports the erasure downstream —
# `len(object)`, `int(object)` and similar at every consumer of a decorated
# helper.
_P = ParamSpec("_P")
_R = TypeVar("_R")

# Which cascade leg is calling. The provider layer has no idea why it was
# invoked, and `LLMClient.complete()` deliberately does not grow a `stage`
# parameter every client (and every wrapper: cache, fallback) would have to
# thread. A ContextVar, like `cost_accumulator`'s, set by the caller that knows
# the answer.
_stage: contextvars.ContextVar[str] = contextvars.ContextVar("loom_genai_stage", default="")

# Extra `loom.*` attributes for the generation made
# inside a `generation_attrs(...)` block — the coverage sweep's gate reason
# ("per_call", "namespace", "server", "list:<n>") and prompt form, so a
# Langfuse or ClickStack query can tell which rule fired without a build
# column. Contextvar, reset on exit, never leaks to the next call.
_gen_attrs: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "loom_gen_attrs", default=None
)


@contextmanager
def generation_attrs(**attrs: str) -> Iterator[None]:
    """Attach `loom.<key>` attributes to the generations made in this block."""
    token = _gen_attrs.set({k: str(v) for k, v in attrs.items() if v is not None})
    try:
        yield
    finally:
        _gen_attrs.reset(token)


def current_generation_attrs() -> dict[str, str]:
    return dict(_gen_attrs.get() or {})


# Bounded, like every other metric label in this package. An unknown stage
# becomes "other" rather than minting a new series per caller.
_KNOWN_STAGES: frozenset[str] = frozenset(
    {
        "provider_validation",
        "dupcheck_rerank",
        "hyde",
        "iterative_judge",
        "inline_rerank",
        "triple_extract",
        "verb_synonyms",
        "derived_intent",
        "extract",
        "extract_typed",
        "extract_relations",
        "extract_coverage_sweep",
        "judge_facts",
        # A code-linked memory's claim re-checked against its changed code.
        "code_link_revise",
        "consolidate_merge",
        # knowledge.synthesize: one map call per window, then
        # the fold(s) into the answer.
        "knowledge_synthesize_map",
        # knowledge.synthesize with datasets: the plan a model proposes for the calculation.
        "knowledge_synthesize_plan",
        "bulk_contextualize",
        "knowledge_synthesize_reduce",
        "entity_extract",
        "session_summary",
        "session_brief",
        "session_judge",
        "session_contrast",
        "compact",
        "title",
        "profile_fold",
        "structured_profile",
        # The sampled retrieval judge: the query and the excerpts it returned.
        "retrieval_judge",
        "code_answer",
        "ask_synthesize",
        "ask_synthesize_cheap",
        # The mid tier is its own series, like the other two.
        "ask_synthesize_mid",
        "ask_synthesize_strong",
        "code_describe",
        "code_summary",
        "code_query_expand",
        "code_hyde",  # The hypothetical-answer passage
        # memory.ask's auxiliary legs, each its own stage so the ask spend can be split by
        # helper instead of lumped under "other". A stage literal that is not in this set
        # fails tests/unit/test_llm_stage_registry.py.
        "ask_consistency",
        "ask_decompose",
        "ask_deep_select",
        "ask_premise",
        "ask_synthesize_deep",
        # The background conflict resolver's one call per marked row.
        "conflict_resolver",
        # `loom decisions extract`: per window of a document, the read of its decisions
        # (decision_extract) and the support check of what was read (decision_support).
        "decision_extract",
        "decision_support",
        # tracker.duplicate_check judge over the uncertain band
        "dupcheck_judge",
        "observe_extract.event",
        "observe_extract.notes",
        "period_digest",
        "other",
    }
)


# Tenant + session identity for the active request, so provider spans can be
# attributed without threading arguments through every call site.
#
# The request root span gets these from `otel.set_loom_attributes`, but child
# spans do not inherit attributes in OTel, so without this every `llm.*` and
# `embed.*` span has an empty `loom.operation` and "which namespace is burning
# LLM spend" is unanswerable from traces.
# `None`, not `{}`: a mutable ContextVar default is one shared object across
# every context, so any accidental in-place write would leak one request's tenant
# into every other request that had not set its own.
_identity: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "loom_genai_identity", default=None
)


def set_identity(
    *,
    org: str = "",
    namespace: str = "",
    agent: str = "",
    user_id: str = "",
    session_id: str = "",
    operation: str = "",
    capture_level: str = "",
) -> None:
    """Record the active request's identity for provider spans to stamp.

    Set once per request from the transport layer. Deliberately a plain `set`
    with no reset: each request runs in its own context, so there is nothing to
    leak into, and a contextmanager here would have to wrap the whole handler.
    """
    ident = {
        k: v
        for k, v in (
            ("org", org),
            ("namespace", namespace),
            ("agent", agent),
            ("user_id", user_id),
            ("session_id", session_id),
            ("operation", operation),
        )
        if v and v != "-"
    }
    # Capture level for this request, resolved once by the transport so the LLM
    # path never queries for it. Absent means "the transport did not resolve it",
    # which `_capture_level` falls back to the operator level for — a worker or CLI
    # path is not a consent decision either way.
    if capture_level in ("off", "query", "all"):
        ident["capture_level"] = capture_level
    _identity.set(ident)


def current_operation() -> str:
    """The wire operation the transport stamped on this request ("" outside
    a request). Lets a shared handler (memory.search under memory.ask /
    context.pack / the MCP search tool) label telemetry with the op the
    caller actually invoked."""
    ident = _identity.get()
    return str((ident or {}).get("operation") or "")


def request_org() -> str:
    """The org of the request this context serves, as the transport recorded
    it once the token was verified; "" outside a request. A worker's identity
    is not one: a pass stamps the org of the row it last worked on, which says
    nothing about whose the next text is."""
    ident = _identity.get() or {}
    op = str(ident.get("operation") or "")
    org = str(ident.get("org") or "")
    if not op or op.startswith("worker.") or org == "*":
        return ""
    return org


def request_identity() -> dict[str, str]:
    """The identity the transport recorded for the request this context serves
    (its org, namespace and user_id), under `request_org`'s rule; {} outside one."""
    return dict(_identity.get() or {}) if request_org() else {}


def set_worker_identity(name: str, *, org: str = "", namespace: str = "") -> None:
    """Identity for a background pass: ``operation=worker.<name>`` plus tenant.

    Without it worker generations (`llm.code_describe`, `llm.code_summary`, the
    embedding worker's `embed.provider`) would carry no loom.org/namespace/operation
    and sit at trace root as nameless traces in Langfuse.

    Merges rather than replaces, and yields to a request: `code.refresh` with
    `wait=true` runs the ingest pass INSIDE an HTTP request, and that request's
    identity (its consent-resolved capture_level, its user/session) must win —
    the caller paid for those generations. A worker-set identity (operation
    already ``worker.*``) or no identity at all is overwritten/merged freely, so
    a per-repo call can add org/namespace after the supervisor set the name.
    """
    current = _identity.get() or {}
    op = current.get("operation", "")
    if op and not op.startswith("worker."):
        return
    ident = dict(current)
    ident["operation"] = f"worker.{name}"
    if org and org != "-":
        ident["org"] = org
    if namespace and namespace != "-":
        ident["namespace"] = namespace
    _identity.set(ident)


# What each stage's prompt actually CONTAINS — the basis for tiered capture.
#
# "query"   : nothing but the user's own search text. Recording it exposes what
#             someone typed into a search box, and nothing they had stored.
# "content" : the prompt EMBEDS retrieved memory text or conversation turns. On
#             `inline_rerank` that is up to 2KB of stored memories pasted in so the
#             model can order them; on `extract` it is the conversation the caller
#             sent. Recording those publishes data a user gave loom to HOLD, which is
#             a materially different decision from recording their query.
#
# Anything unlisted is "content" — fail closed. A stage added later must not start
# emitting user data because nobody remembered to classify it.
_QUERY_ONLY_STAGES: frozenset[str] = frozenset(
    {
        "hyde",
        "triple_extract",
        "verb_synonyms",
        "derived_intent",
        # The decomposition leg reads only the question.
        "ask_decompose",
    }
)


def _stage_carries(stage: str) -> str:
    """`"query"` or `"content"` for a cascade leg."""
    return "query" if stage in _QUERY_ONLY_STAGES else "content"


def _capture_level() -> str:
    """`"off"` | `"query"` | `"all"` for this request.

    Namespace consent wins over the server level: an explicit `False` means off for
    every leg, an explicit `True` means all of them. Unset falls back to the
    operator's level.

    Fails CLOSED on an unrecognised level, unlike the rest of this package. Every
    other helper here prefers losing telemetry to breaking a request; this one
    prefers losing telemetry to publishing text a user may not have agreed to. The
    two failure directions are not symmetric.
    """
    ident = _identity.get()
    if ident and ident.get("capture_level") in ("off", "query", "all"):
        return str(ident["capture_level"])
    return global_default()


def narrow_capture_level(level: str) -> None:
    """Lower this request's capture level to `level` when that is more
    restrictive (`off` < `query` < `all`); never raises it. A handler that
    reads more namespaces than the one the transport resolved consent for
    calls this before any provider call."""
    order = ("off", "query", "all")
    wanted = level if level in order else "off"
    ident = dict(_identity.get() or {})
    if order.index(wanted) < order.index(_capture_level()):
        ident["capture_level"] = wanted
        _identity.set(ident)


def current_capture_level() -> str:
    """This request's resolved capture level, for callers outside this module.

    The level is resolved once per request by the transports (`http.py`,
    `mcp.py`) from a 60s cache and carried on the identity ContextVar, so this
    is a free read. Exposed because query-text consent is not a Langfuse-only
    question: `loom.search_trace_log` stores the raw query too, and it must
    honour the same answer rather than keep a second, divergent notion of it.
    """
    return _capture_level()


def _may_capture(stage: str) -> bool:
    """Whether THIS leg's prompt/completion text may be recorded.

    One boolean is the wrong shape: a single switch would force a choice between no
    debugging at all and shipping stored memories plus conversation turns to a third
    party. Tiering by payload gives the common debugging case (a bad hypothetical
    answer or rewrite) without ever emitting held data on a default.
    """
    level = _capture_level()
    if level == "off":
        return False
    if level == "all":
        return True
    return _stage_carries(stage) == "query"


def _stamp_identity(span: Any) -> None:
    """Put tenant + Langfuse identity on a provider span."""
    ident = _identity.get()
    if not ident:
        return
    for key, attr in (
        ("operation", "loom.operation"),
        ("org", "loom.org"),
        ("namespace", "loom.namespace"),
        ("agent", "loom.agent"),
        ("user_id", "loom.user_id"),
    ):
        if ident.get(key):
            span.set_attribute(attr, ident[key])
    # Langfuse's own keys. It groups observations into sessions and users by
    # these, and they must be on the observation itself — OTel children do not
    # inherit the root span's attributes.
    if ident.get("session_id"):
        span.set_attribute("langfuse.session.id", ident["session_id"])
    if ident.get("user_id"):
        span.set_attribute("langfuse.user.id", ident["user_id"])
    tags = [f"org:{ident[k]}" for k in ("org",) if ident.get(k)]
    tags += [f"namespace:{ident[k]}" for k in ("namespace",) if ident.get(k)]
    if tags:
        # Langfuse reads tags as a JSON array of strings. `admin.py` and
        # docs/features/integrations/langfuse.md query by these two tag shapes, so
        # keep the `key:value` form.
        span.set_attribute("langfuse.trace.tags", tags)
    if ident.get("operation"):
        # Also on the children: Langfuse names the trace from whichever span it
        # sees first, and a batch that reaches it before the root would produce
        # a correct trace with an empty name.
        span.set_attribute("langfuse.trace.name", ident["operation"])


@contextlib.contextmanager
def llm_stage(name: str) -> Iterator[None]:
    """Label the cascade leg for any provider call made inside this block."""
    token = _stage.set(name if name in _KNOWN_STAGES else "other")
    try:
        yield
    finally:
        _stage.reset(token)


def current_stage() -> str:
    return _stage.get() or "other"


def staged(
    stage: str,
) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Decorator form of `llm_stage`, for labelling a whole helper.

    Used instead of wrapping each call site in a `with`: the search-LLM helpers
    are one-call functions, so the decorator is a single line per function and
    leaves their bodies untouched. It must be a decorator rather than a bare
    `_stage.set()` at function top — a set without a reset leaks the label to
    whatever the same task calls next, silently mis-attributing the following
    generation.
    """

    def deco(fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        @functools.wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with llm_stage(stage):
                return await fn(*args, **kwargs)

        return wrapper

    return deco


def _truncate(text: str, limit: int) -> str:
    """Bound a field with an explicit marker.

    A silently clipped prompt is worse than a short one: it reads as the whole
    thing, and the missing tail is exactly where a malformed instruction usually
    is.
    """
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…[truncated {len(text) - limit} chars]"


def _prompt_identity(system: str) -> tuple[str, str, int | None]:
    """`(sha, langfuse_prompt_name, langfuse_prompt_version)` for a system prompt.

    Identity is derived from the text ACTUALLY sent to the provider, not from a
    registry the hot path has to import. That means it covers prompts built
    inline (several are assembled by `_build_synthesis_prompt`-style helpers) and
    can never drift from what was really used — the failure mode a hand-maintained
    stage→constant table has.

    The name/version come from `server/observability/langfuse_prompts.lock.json`, written by
    `scripts/codegen/push_langfuse_prompts.py`. Reading a committed lockfile keeps this a
    local dict lookup: fetching prompt versions from Langfuse at runtime would put
    a third party in the path of every LLM call.

    Returns `version=None` when the prompt is not in the lockfile — an edited
    prompt that has not been re-pushed, or one never registered. Callers must NOT
    stamp a link in that case: a generation labelled with the wrong prompt version
    is worse than one with no label, because it sends you to the wrong diff.
    """
    sha = hashlib.sha256(system.encode("utf-8", "replace")).hexdigest()[:12]
    entry = _prompt_lockfile().get(sha)
    if not entry:
        return sha, "", None
    version = entry.get("version")
    return sha, str(entry.get("name") or ""), (int(version) if isinstance(version, int) else None)


@functools.lru_cache(maxsize=1)
def _prompt_lockfile() -> dict[str, dict[str, Any]]:
    """Load the sha→(name, version) map once. Missing file is fine."""
    try:
        # Beside this module, NOT in `deploy/`. The Dockerfile copies `server`
        # and `sdk` only — a lockfile in `deploy/` would never reach the image,
        # and every prompt link would be silently absent in the deployed build
        # while working locally.
        path = pathlib.Path(__file__).with_name("langfuse_prompts.lock.json")
        if not path.exists():
            return {}
        data = json.loads(path.read_text())
        prompts = data.get("prompts")
        return prompts if isinstance(prompts, dict) else {}
    except Exception:  # noqa: BLE001 — a bad lockfile must not break LLM calls
        log.warning("langfuse prompt lockfile unreadable; prompt links disabled")
        return {}


class _Generation:
    """Handle for recording usage onto an in-flight generation span."""

    __slots__ = ("_span", "_model", "_attempts")

    def __init__(self, span: Any, model: str) -> None:
        self._span = span
        self._model = model
        self._attempts = 0

    def record_content(self, *, system: str = "", user: str = "", output: str = "") -> None:
        """Attach bounded prompt/completion text, and link the prompt version.

        Three facts about the Langfuse OTLP contract, each established by probing
        the live API rather than from docs, and each capable of silently deleting
        the observation:

        1. `langfuse.observation.input` accepts a JSON object and PRESERVES its
           structure, so system and user arrive as separate fields instead of one
           concatenated blob. (`gen_ai.prompt`/`completion` and
           `input.value`/`output.value` also work, but flatten to a string.)
        2. `langfuse.prompt.version` must be an **int**. Sent as a string, the
           entire observation is DROPPED — no error, no partial record.
        3. `langfuse.prompt.name` without a version also DROPS the observation.
           So the two are set together or not at all, which is why this is one
           method and not two.

        An unknown prompt name degrades safely: the observation survives with no
        link. `loom.prompt.sha` is always set, so prompt-version grouping still
        works in ClickStack even for prompts Langfuse has never seen.
        """
        if self._span is None:
            return
        try:
            sha, name, version = _prompt_identity(system)
            self._span.set_attribute("loom.prompt.sha", sha)
            for _k, _v in current_generation_attrs().items():
                self._span.set_attribute(f"loom.{_k}", _v)
            # Both, or neither — see (2) and (3) above.
            if name and version is not None:
                self._span.set_attribute("langfuse.prompt.name", name)
                self._span.set_attribute("langfuse.prompt.version", int(version))

            # Tiered by what THIS leg's prompt contains, then by namespace consent.
            # A query-only leg records at the default level; a leg that embeds
            # retrieved memories or conversation turns needs an explicit opt-in.
            if not _may_capture(current_stage()):
                return
            limit = max(0, int(settings.langfuse_generation_io_max_chars))
            if not limit:
                return
            self._span.set_attribute(
                "langfuse.observation.input",
                json.dumps(
                    {
                        "system": _truncate(system, limit),
                        "user": _truncate(user, limit),
                    }
                ),
            )
            self._span.set_attribute("langfuse.observation.output", _truncate(output, limit))
        except Exception:  # noqa: BLE001 — telemetry must not fail a completion
            pass

    def note_attempt(self) -> None:
        """Count a provider attempt. Retries are otherwise invisible."""
        self._attempts += 1

    def record_usage(
        self,
        tokens_in: int,
        tokens_out: int,
        *,
        priced: bool,
        cached_input_tokens: int = 0,
        cache_write_5m_tokens: int = 0,
        cache_write_1h_tokens: int = 0,
    ) -> None:
        """`tokens_in` is the whole prompt (the provider's total);
        `cached_input_tokens` the share read from the provider's prompt cache
        and the `cache_write_*` counts the share written to it (Anthropic
        only). The span's `gen_ai.usage.cost` prices the shares at the cache
        rates so it agrees with the ledger row."""
        if self._span is None:
            return
        try:
            from server.observability.cost_accumulator import turn_cost_usd

            self._span.set_attribute("gen_ai.usage.input_tokens", int(tokens_in))
            self._span.set_attribute("gen_ai.usage.output_tokens", int(tokens_out))
            if cached_input_tokens:
                # Provider-side prompt-cache read, under the semantic-convention
                # key both Langfuse and HyperDX read as a cache read. It is part
                # of `input_tokens` (the provider's total), and both tools take
                # it out of the uncached share themselves; under any other key
                # Langfuse stores it as an extra usage type and counts it twice
                # in the total.
                self._span.set_attribute(
                    "gen_ai.usage.cache_read.input_tokens", int(cached_input_tokens)
                )
                if tokens_in > 0:
                    self._span.set_attribute(
                        "loom.llm.prompt_cache_hit_ratio",
                        round(cached_input_tokens / tokens_in, 4),
                    )
            w5, w1 = max(0, int(cache_write_5m_tokens)), max(0, int(cache_write_1h_tokens))
            if w5 + w1:
                # Also part of `input_tokens`; Langfuse subtracts it from the
                # uncached share as it does a cache read.
                self._span.set_attribute("gen_ai.usage.cache_creation.input_tokens", w5 + w1)
            if self._attempts:
                self._span.set_attribute("gen_ai.request.attempts", self._attempts)
            if priced:
                reads = max(0, int(cached_input_tokens))
                self._span.set_attribute(
                    "gen_ai.usage.cost",
                    turn_cost_usd(
                        self._model,
                        input_tokens=max(0, int(tokens_in) - reads - w5 - w1),
                        output_tokens=int(tokens_out),
                        cache_read_tokens=reads,
                        cache_creation_5m_tokens=w5,
                        cache_creation_1h_tokens=w1,
                    ),
                )
            else:
                # Deliberately absent rather than 0.0: an unpriced model billing
                # $0 reads as "free" on a cost panel, which is worse than a gap.
                self._span.set_attribute("gen_ai.usage.cost_unknown", True)
        except Exception:  # noqa: BLE001 — telemetry must not fail a completion
            pass


@contextlib.contextmanager
def generation_span(
    *,
    provider: str,
    model: str,
    operation: str = "chat",
) -> Iterator[_Generation]:
    """Wrap one provider call in a span Langfuse will read as a generation.

    The span covers the whole retry loop, so its duration is the latency the
    caller actually paid — both providers wrap the POST in `tenacity`
    `stop_after_attempt(4)` and return only the last attempt's usage, so timing
    a single attempt would under-report by up to 4x.

    Fake providers are labelled `gen_ai.provider.fake` and carry no cost:
    `FakeLLMClient` synthesises token counts as `len(text)//4`, so leaving them
    indistinguishable would put invented spend on a dashboard.
    """
    stage = current_stage()
    attrs: dict[str, Any] = {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        # The semantic-convention operation (`chat`), which both tools key their
        # LLM views on; the cascade leg that asked is `loom.llm.stage`, and the
        # span name.
        "gen_ai.operation.name": operation,
        "loom.llm.stage": stage,
        # Explicit, so Langfuse does not have to infer from `model` alone.
        "langfuse.observation.type": "generation",
    }
    if provider.startswith("fake"):
        attrs["gen_ai.provider.fake"] = True
    with _provider_span(f"llm.{stage}", attrs) as span:
        yield _Generation(span, model)


@contextlib.contextmanager
def _provider_span(name: str, attrs: dict[str, Any]) -> Iterator[Any]:
    """One provider-call span (None when OTel is off or the start failed):
    `attrs` plus the request identity at start, `gen_ai.latency_ms` at the
    end. A provider failure sets ERROR status and `error.type` with the
    exception's type only, not its message, which can quote the provider's
    reply; a cancellation is not the provider's error and leaves status
    unset."""
    span_cm: Any = None
    span: Any = None
    started = time.perf_counter()
    try:
        from opentelemetry import trace as _trace

        span_cm = _trace.get_tracer("loom.genai").start_as_current_span(
            name, record_exception=False, set_status_on_exception=False
        )
        span = span_cm.__enter__()
        for key, value in attrs.items():
            span.set_attribute(key, value)
        _stamp_identity(span)
    except Exception:  # noqa: BLE001 — no OTel, or a shape change: degrade quietly
        span_cm = None
        span = None
    try:
        yield span
    except Exception as e:
        if span is not None:
            with contextlib.suppress(Exception):
                from opentelemetry.trace import Status, StatusCode

                span.set_attribute("error.type", type(e).__name__)
                span.set_status(Status(StatusCode.ERROR, type(e).__name__))
        raise
    finally:
        if span is not None:
            with contextlib.suppress(Exception):
                span.set_attribute("gen_ai.latency_ms", int((time.perf_counter() - started) * 1000))
        if span_cm is not None:
            with contextlib.suppress(Exception):
                span_cm.__exit__(None, None, None)


class _Embedding:
    """Handle for recording usage onto an in-flight embedding span."""

    __slots__ = ("_span", "_model")

    def __init__(self, span: Any, model: str) -> None:
        self._span = span
        self._model = model

    def set_tier(self, tier: str) -> None:
        """Which cache tier served this embed: process | shared | miss.
        Empty when the embedder has no cache."""
        if self._span is None or not tier:
            return
        try:
            self._span.set_attribute("loom.embedding.cache_tier", str(tier))
        except Exception:  # noqa: BLE001 — telemetry never fails an embed
            pass

    def record_usage(
        self,
        *,
        billed_tokens: int = 0,
        cached_tokens: int = 0,
        cache_hits: int = 0,
        cache_misses: int = 0,
        batch_size: int = 1,
        called_provider: bool = True,
    ) -> None:
        """Record billed and cache-served usage as separate quantities.

        `billed_tokens` is what the provider actually charged for — it comes
        from the response `usage` block and is the only figure that feeds
        cost. `cached_tokens` is what the in-process LRU served instead, counted
        locally with tiktoken.

        The two are deliberately NOT summed into `gen_ai.usage.input_tokens`,
        and cached tokens are deliberately NOT sent to
        `cost_accumulator.report_embed`. That accumulator feeds
        `agent_events.embedding_tokens`, which `insights/app/cost.py` sums into a
        billing report — so counting a cache hit there would invent spend that
        never happened. Reported separately, the same numbers answer the more
        useful question (how much is the cache saving) without corrupting the
        cost figure.
        """
        if self._span is None:
            return
        try:
            from server.observability.cost_accumulator import embed_cost_usd

            # Only a call that actually reached the provider is an EMBEDDING
            # observation. Marking every embed one would put cache hits in Langfuse
            # as embeddings with 0 tokens and $0, inflating the count with empty
            # rows and dragging every per-call average down by the cache hit rate.
            # Set here rather than at span start because whether the cache served
            # the request is not known until it returns.
            self._span.set_attribute(
                "langfuse.observation.type",
                "embedding" if called_provider else "span",
            )
            self._span.set_attribute("gen_ai.usage.input_tokens", int(billed_tokens))
            self._span.set_attribute(
                "gen_ai.usage.cost", embed_cost_usd(self._model, billed_tokens)
            )
            if cached_tokens:
                # Served by Loom's own cache, so NOT part of `input_tokens` and not a
                # provider cache read: under a `gen_ai.usage.*` cache key both tools
                # would subtract it from the billed input.
                self._span.set_attribute("loom.embed.cached_tokens", int(cached_tokens))
                self._span.set_attribute(
                    "loom.embed.cost_saved_usd",
                    embed_cost_usd(self._model, cached_tokens),
                )
                # Cached counts are a LOCAL tiktoken estimate: a cache hit never
                # sees a provider `usage` block. Flagged so nobody reconciles
                # this against an invoice.
                self._span.set_attribute("loom.embed.cached_tokens_estimated", True)
            self._span.set_attribute("loom.embed.cache_hits", int(cache_hits))
            self._span.set_attribute("loom.embed.cache_misses", int(cache_misses))
            self._span.set_attribute("loom.embed.batch_size", int(batch_size))
        except Exception:  # noqa: BLE001 — telemetry must not fail an embed
            pass


@contextlib.contextmanager
def embedding_span(*, provider: str, model: str) -> Iterator[_Embedding]:
    """Wrap one embedder call in a span both tools read as an embedding call.

    Embeddings are instrumented at the embedder, not at the four `embed.compute`
    call sites: the batch path (`embed_many`) is driven by the embedding worker,
    which has no `traced()` block at all, so call-site instrumentation would miss
    the bulk of all embedding spend.
    """
    attrs: dict[str, Any] = {
        "gen_ai.provider.name": provider,
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.operation.name": "embeddings",
        # The leg that needed the vector, so embedding latency is attributable
        # to search vs write vs a worker backfill.
        "loom.llm.stage": current_stage(),
    }
    # `langfuse.observation.type` is deliberately NOT set here — see
    # `_Embedding.record_usage`. A cache hit made no provider call and is not
    # an embedding observation, and whether the cache served the request is
    # unknown until after it returns.
    #
    # Named "embed.provider", NOT "embed.compute": four call sites already open
    # a `traced("embed.compute")` block, and this span nests inside them. Two
    # different spans sharing one name makes the waterfall ambiguous and
    # silently mixes the outer block (which also covers cache lookup and row
    # assembly) into any latency query for the provider call itself.
    with _provider_span("embed.provider", attrs) as span:
        yield _Embedding(span, model)


__all__ = [
    "current_capture_level",
    "current_stage",
    "set_identity",
    "set_worker_identity",
    "embedding_span",
    "generation_span",
    "llm_stage",
    "staged",
]
