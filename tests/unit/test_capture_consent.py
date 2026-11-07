"""Per-namespace consent for capturing prompt/completion text.

This is the one helper in `server/observability/` that fails CLOSED. Everything
else there prefers losing telemetry to breaking a request; this prefers losing
telemetry to publishing text a human may have declined. The two failure directions
are not symmetric, and these tests pin that asymmetry.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.auth.fanout import Fanout
from server.observability import capture_consent, genai


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    """Reset both the consent cache AND the identity ContextVar.

    `set_identity` is deliberately a plain `set` with no reset — each request runs
    in its own context, so production has nothing to leak into. Under pytest,
    though, sync tests share one context, so a test that sets `capture_io=False`
    silently suppresses content capture for every test that runs after it. That
    exact leak made two tests in test_generation_content_and_prompts.py fail in the
    full suite while passing in isolation.
    """
    capture_consent.invalidate()
    genai._identity.set(None)
    yield
    capture_consent.invalidate()
    genai._identity.set(None)


def _global(monkeypatch: pytest.MonkeyPatch, level: str) -> None:
    """Set the operator's default capture level on both modules."""
    for mod in (capture_consent, genai):
        monkeypatch.setattr(mod.settings, "langfuse_capture_io_level", level)


@pytest.mark.asyncio
async def test_null_inherits_the_server_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """NULL must mean "inherit", not "false".

    A `Bool DEFAULT false` column would have opted every existing namespace out and
    read as a deliberate choice nobody made — which is why the column is nullable.
    """
    monkeypatch.setattr(capture_consent, "_load", _returning(None))
    _global(monkeypatch, "all")
    assert await capture_consent.resolve("o", "ns") == "all"
    capture_consent.invalidate()
    _global(monkeypatch, "query")
    assert await capture_consent.resolve("o", "ns") == "query"
    capture_consent.invalidate()
    _global(monkeypatch, "off")
    assert await capture_consent.resolve("o", "ns") == "off"


@pytest.mark.asyncio
async def test_explicit_false_beats_a_permissive_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user's opt-out must not be overridable by the operator's default.

    This is the whole point of the feature: one server-wide switch cannot express
    "Alice declined".
    """
    monkeypatch.setattr(capture_consent, "_load", _returning(False))
    _global(monkeypatch, "all")
    assert await capture_consent.resolve("o", "personal-alice") == "off"


@pytest.mark.asyncio
async def test_explicit_true_beats_a_restrictive_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_consent, "_load", _returning(True))
    _global(monkeypatch, "off")
    assert await capture_consent.resolve("o", "personal-bob") == "all"


@pytest.mark.asyncio
async def test_lookup_failure_does_not_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable ClickHouse must not fall back to the permissive global.

    Failing open here would publish query text on exactly the occasions we cannot
    check whether it was allowed. A privacy control that fails open is not one.
    """

    async def _boom(org: str, ns: str) -> bool | None:
        raise RuntimeError("clickhouse unreachable")

    # `_load` swallows its own exceptions and returns False; assert the real
    # implementation does that rather than mocking the behaviour we want.
    from server.core.ch import get_client as _real  # noqa: F401

    monkeypatch.setattr(
        "server.core.ch.get_client", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )
    _global(monkeypatch, "all")
    assert await capture_consent.resolve("o", "ns") == "off", (
        "a failed consent lookup must not capture"
    )


@pytest.mark.asyncio
async def test_result_is_cached_so_the_llm_path_stays_query_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One query per namespace per TTL, not one per request — let alone per call.

    `record_content` runs inside every provider call; a query there would add a
    ClickHouse round trip per generation, which is what the LLM-free read path work
    was specifically protecting.
    """
    calls: list[tuple[str, str]] = []

    async def _counting(org: str, ns: str) -> bool | None:
        calls.append((org, ns))
        return False

    monkeypatch.setattr(capture_consent, "_load", _counting)
    _global(monkeypatch, "all")
    for _ in range(25):
        assert await capture_consent.resolve("o", "ns") == "off"
    assert len(calls) == 1, f"expected 1 lookup, got {len(calls)}"


@pytest.mark.asyncio
async def test_invalidate_makes_a_ui_toggle_take_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writing namespace settings drops the cache, so a toggle is not TTL-delayed."""
    state = {"value": True}

    async def _dynamic(org: str, ns: str) -> bool | None:
        return state["value"]

    monkeypatch.setattr(capture_consent, "_load", _dynamic)
    _global(monkeypatch, "all")
    assert await capture_consent.resolve("o", "ns") == "all"
    state["value"] = False
    assert await capture_consent.resolve("o", "ns") == "all", "still cached"
    capture_consent.invalidate("o", "ns")
    assert await capture_consent.resolve("o", "ns") == "off", "toggle must apply"


@pytest.mark.asyncio
async def test_cache_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unbounded map keyed on caller-supplied namespace is a memory leak."""
    monkeypatch.setattr(capture_consent, "_load", _returning(False))
    monkeypatch.setattr(capture_consent, "_MAX_ENTRIES", 10)
    for i in range(40):
        await capture_consent.resolve("o", f"ns{i}")
    assert len(capture_consent._cache) <= 10


# --- enforcement in the generation path ---


def test_record_content_honours_a_namespace_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision reaches the provider via the identity ContextVar.

    Resolved once per request by the transport; `record_content` only reads a
    boolean, so no query happens inside the LLM call.
    """
    _global(monkeypatch, "all")

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, Any] = {}

        def set_attribute(self, k: str, v: Any) -> None:
            self.attrs[k] = v

    genai.set_identity(org="o", namespace="personal-alice", capture_level="off")
    span = _Span()
    genai._Generation(span, "gpt-4o-mini").record_content(
        system="SYS", user="what did alice earn", output="REPLY"
    )
    assert "langfuse.observation.input" not in span.attrs
    assert "langfuse.observation.output" not in span.attrs
    flat = repr(span.attrs)
    assert "alice earn" not in flat
    # Cost/token/prompt-version signals are NOT gated: they carry no user content,
    # and gating them would make a privacy choice also a cost-blindness choice.
    assert span.attrs["loom.prompt.sha"]

    genai.set_identity(org="o", namespace="personal-bob", capture_level="all")
    span2 = _Span()
    genai._Generation(span2, "gpt-4o-mini").record_content(
        system="SYS", user="bob's question", output="REPLY"
    )
    assert "langfuse.observation.input" in span2.attrs


def test_unresolved_consent_falls_back_to_the_server_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker or CLI path that never resolved consent must not invent one."""
    genai.set_identity(org="o", namespace="ns")  # no capture_level resolved
    _global(monkeypatch, "off")
    assert genai._may_capture("hyde") is False
    _global(monkeypatch, "all")
    assert genai._may_capture("hyde") is True


def test_consent_is_not_leaked_as_a_span_attribute() -> None:
    """`capture_io` lives in the identity dict but must not be stamped on spans.

    `stamp_identity` iterates an explicit key list; this guards that a future edit
    does not start echoing an internal decision onto every generation.
    """
    genai.set_identity(org="o", namespace="ns", capture_level="off")

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, Any] = {}

        def set_attribute(self, k: str, v: Any) -> None:
            self.attrs[k] = v

    span = _Span()
    genai.stamp_identity(span)
    assert not any("capture" in k for k in span.attrs)


def _returning(value: bool | None):
    async def _f(org: str, ns: str) -> bool | None:
        return value

    return _f


# --- tiering by what the prompt actually contains ---


def test_query_only_legs_record_at_the_default_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default records what a person typed, and nothing loom holds for them.

    `hyde` and friends see only the search text, so recording them exposes a query
    and no stored data. That is where searches most often go visibly wrong, so it is
    the leg worth having by default.
    """
    _global(monkeypatch, "query")
    genai._identity.set(None)
    for stage in (
        "hyde",
        "triple_extract",
        "verb_synonyms",
        "derived_intent",
    ):
        assert genai._may_capture(stage) is True, stage


def test_content_legs_need_an_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """These prompts EMBED data a user gave loom to hold.

    `inline_rerank` pastes up to 2KB of retrieved memories in so the model can order
    them; `extract` pastes the conversation the caller sent. Recording those is a
    materially different decision from recording a query, so the default level must
    not do it.
    """
    _global(monkeypatch, "query")
    genai._identity.set(None)
    for stage in (
        "inline_rerank",  # retrieved memory snippets
        "iterative_judge",  # retrieved memory excerpts
        "retrieval_judge",  # the query and the excerpts it returned
        "extract",  # conversation messages
        "judge_facts",
        "session_summary",  # conversation turns
        "compact",
        "title",
        "entity_extract",  # a memory's stored value
        "profile_fold",
    ):
        assert genai._may_capture(stage) is False, stage

    # …and they do record once someone opts in.
    _global(monkeypatch, "all")
    for stage in ("inline_rerank", "extract"):
        assert genai._may_capture(stage) is True, stage


def test_unclassified_stage_is_treated_as_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed on anything unlisted.

    A stage added later must not start emitting user data because nobody remembered
    to classify it. `other` is the label an unrecognised stage collapses to, so it
    is the concrete case.
    """
    _global(monkeypatch, "query")
    genai._identity.set(None)
    assert genai._stage_carries("some-stage-invented-next-year") == "content"
    assert genai._may_capture("some-stage-invented-next-year") is False
    assert genai._may_capture("other") is False


def test_namespace_off_beats_query_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """A person declining outranks the operator's default, on every leg."""
    _global(monkeypatch, "query")
    genai.set_identity(org="o", namespace="personal-alice", capture_level="off")
    assert genai._may_capture("hyde") is False
    assert genai._may_capture("inline_rerank") is False


def test_namespace_all_beats_an_off_server_level(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """And consenting outranks an operator who left it off globally."""
    _global(monkeypatch, "off")
    genai.set_identity(org="o", namespace="personal-bob", capture_level="all")
    assert genai._may_capture("hyde") is True
    assert genai._may_capture("inline_rerank") is True


def test_unrecognised_level_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in the env var must not become "record everything"."""
    _global(monkeypatch, "verbose")  # not a valid level
    genai._identity.set(None)
    assert genai._may_capture("hyde") is False
    assert genai._may_capture("inline_rerank") is False


def test_record_content_honours_the_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through the real recorder, not just the predicate."""
    _global(monkeypatch, "query")
    monkeypatch.setattr(genai.settings, "langfuse_generation_io_max_chars", 2000)
    genai._identity.set(None)

    class _Span:
        def __init__(self) -> None:
            self.attrs: dict[str, Any] = {}

        def set_attribute(self, k: str, v: Any) -> None:
            self.attrs[k] = v

    # A query-only leg records.
    with genai.llm_stage("hyde"):
        span = _Span()
        genai._Generation(span, "m").record_content(
            system="S", user="Question: what did I say about pay", output="O"
        )
    assert "langfuse.observation.input" in span.attrs

    # A leg carrying retrieved memories does not.
    with genai.llm_stage("inline_rerank"):
        span2 = _Span()
        genai._Generation(span2, "m").record_content(
            system="S", user="Query: pay\n\nSnippets:\n[0] salary is 100k", output="[]"
        )
    assert "langfuse.observation.input" not in span2.attrs
    assert "salary is 100k" not in repr(span2.attrs)
    # Cost/prompt-version signals survive either way.
    assert span2.attrs["loom.prompt.sha"]


# --- the retrieval judge, a worker path that sends a namespace's text ---


async def _judge_capture_level_seen(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Run one retrieval-judge pass; return the capture level its LLM call saw."""
    from server.llm import client as llm_client
    from server.observability import langfuse_scores, retrieval_judge
    from server.workers import cost_event

    seen: list[str] = []

    class _Client:
        async def complete(self, *a: Any, **kw: Any) -> Any:
            seen.append(genai.current_capture_level())
            return type("Out", (), {"text": "{}"})()

    monkeypatch.setattr(retrieval_judge.settings, "retrieval_judge_model", "")
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Client())
    monkeypatch.setattr(langfuse_scores, "post_score", lambda **kw: None)
    monkeypatch.setattr(cost_event, "emit_worker_cost_event_for", lambda **kw: None)
    await retrieval_judge._judge_one(
        {
            "trace_id": "0" * 32,
            "org": "o",
            "namespace": "personal-alice",
            "query": "what is my salary",
            "excerpts": ["salary is 100k"],
        }
    )
    return seen


@pytest.mark.asyncio
async def test_retrieval_judge_honours_a_namespace_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The judge prompt is the namespace's query plus its memory excerpts.

    It must ride the judged namespace's consent, not the operator default: with the
    server at "all" and the namespace opted out, the judge's generation records nothing.
    """
    monkeypatch.setattr(capture_consent, "_load", _returning(False))
    _global(monkeypatch, "all")
    assert await _judge_capture_level_seen(monkeypatch) == ["off"]


@pytest.mark.asyncio
async def test_retrieval_judge_fails_closed_when_consent_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(org: str, ns: str) -> str:
        raise RuntimeError("consent unavailable")

    monkeypatch.setattr(capture_consent, "resolve", _boom)
    _global(monkeypatch, "all")
    assert await _judge_capture_level_seen(monkeypatch) == ["off"]


# --- the MCP loom_search tool: its fan-out reads more namespaces than the transport resolved ---


async def _loom_search_capture_levels_seen(
    monkeypatch: pytest.MonkeyPatch, accessible: list[str]
) -> list[str]:
    """Run one loom_search; return the capture level each search leg's LLM call saw."""
    from server.llm import client as llm_client
    from server.memory.app.loom_search import fanout as loom_search_fanout
    from server.transport import mcp_call, mcp_outcome

    seen: list[str] = []

    class _Client:
        async def complete(self, *a: Any, **kw: Any) -> Any:
            seen.append(genai.current_capture_level())
            return type("Out", (), {"text": "{}"})()

    async def _search_leg(inp: Any) -> Any:
        # HyDE / rewrite / rerank inside the real handler go through this client.
        await llm_client.get_llm_client().complete(system="S", user=inp.query)
        raise RuntimeError("stub leg")  # the tool reports a failed leg and carries on

    async def _empty(*a: Any, **kw: Any) -> list[Any]:
        return []

    async def _failed(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("stub leg")

    async def _defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "o", "namespace": "personal-alice", "agent": "cursor"}

    async def _fanout(org: str, *_: object) -> Fanout:
        return Fanout(org=org, namespaces=tuple(accessible), cap=32)

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _Client())
    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    monkeypatch.setattr("server.auth.fanout.resolve_fanout", _fanout)
    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search_leg)
    monkeypatch.setattr(
        "server.knowledge.app.conversations.handle_conversation_search", _search_leg
    )
    monkeypatch.setattr(loom_search_fanout, "knowledge_with_repo_docs", _failed)
    monkeypatch.setattr(loom_search_fanout, "search_profile", _empty)
    monkeypatch.setattr(loom_search_fanout, "tracker_leg", _empty)
    for mod in (mcp_call, mcp_outcome):
        monkeypatch.setattr(mod, "_emit_mcp_event", lambda *a, **kw: None)
    monkeypatch.setattr("server.transport.post_rows.post_handler_rows", _empty)
    params = {"name": "loom_search", "arguments": {"query": "what is my salary"}}
    out = await mcp_call._call_tool(1, params, auth=None, client_name="Cursor")
    assert out["result"]["isError"] is False, out
    return seen


@pytest.mark.asyncio
async def test_loom_search_honours_a_namespace_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server at "all", the searched namespace opted out: every leg records nothing."""
    monkeypatch.setattr(capture_consent, "_load", _returning(False))
    _global(monkeypatch, "all")
    assert await _loom_search_capture_levels_seen(monkeypatch, []) == ["off", "off"]


@pytest.mark.asyncio
async def test_loom_search_fan_out_takes_the_most_restrictive_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The home namespace consents, a fanned-into one opted out: "off" wins."""

    async def _per_ns(org: str, ns: str) -> bool | None:
        return {"personal-alice": True, "team-payroll": False}.get(ns)

    monkeypatch.setattr(capture_consent, "_load", _per_ns)
    _global(monkeypatch, "all")
    seen = await _loom_search_capture_levels_seen(monkeypatch, ["personal-alice", "team-payroll"])
    assert seen == ["off", "off"]


@pytest.mark.asyncio
async def test_loom_search_fails_closed_when_consent_is_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(org: str, namespaces: list[str]) -> str:
        raise RuntimeError("consent unavailable")

    monkeypatch.setattr(capture_consent, "resolve_most_restrictive", _boom, raising=False)
    _global(monkeypatch, "all")
    assert await _loom_search_capture_levels_seen(monkeypatch, []) == ["off", "off"]


@pytest.mark.asyncio
async def test_most_restrictive_level_across_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _per_ns(org: str, ns: str) -> bool | None:
        return {"yes": True, "no": False}.get(ns)

    monkeypatch.setattr(capture_consent, "_load", _per_ns)
    _global(monkeypatch, "query")
    assert await capture_consent.resolve_most_restrictive("o", ["yes"]) == "all"
    assert await capture_consent.resolve_most_restrictive("o", ["yes", "unset"]) == "query"
    assert await capture_consent.resolve_most_restrictive("o", ["yes", "unset", "no"]) == "off"
    assert await capture_consent.resolve_most_restrictive("o", []) == "query"


@pytest.mark.parametrize("level", ["", "Query", "everything"])
def test_an_empty_or_unknown_server_level_captures_nothing(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """`LOOM_LANGFUSE_CAPTURE_IO_LEVEL=` (empty) resolves to "off", never to
    "query" (which records the user's own search text); consent is given, not
    assumed."""
    _global(monkeypatch, level)
    assert capture_consent.global_default() == "off"
    assert genai.current_capture_level() == "off"
