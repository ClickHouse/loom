"""Handler-path tests for memory.ask (all externals faked — no network).

Retrieval is faked by monkeypatching `handle_memory_search` at its source
module (the service imports it by name at call time), the LLM by patching
`get_llm_client`, and the cost-cap gate by patching `namespace_over_cap`.
Covers the four branches: retrieval abstains -> abstained; allow_synthesis
False -> retrieval_only; happy path -> synthesized; over-cap -> retrieval_only
carrying "budget_capped".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit


class _FakeLLM:
    provider = "fake"
    model = "fake-llm"

    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        return SimpleNamespace(text=self._text)


def _hit(key: str, *, excerpt: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.8,
        rank_score=0.8,
    )


def _search_output(
    hits: list[_MemorySearchHit], *, abstain: bool = False, reason: str = ""
) -> MemorySearchOutput:
    return MemorySearchOutput(
        results=hits,
        embedding_model="fake-embed",
        abstain=abstain,
        abstain_reason=reason,
    )


@pytest.fixture
def ask_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget

    env = SimpleNamespace()
    env.search_output = _search_output([])
    env.search_inputs = []
    env.over_cap = False
    env.llm = _FakeLLM("Your favorite color is blue [m1].")

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        env.search_inputs.append(search_input)
        return env.search_output

    async def fake_over_cap(org: str, ns: str) -> bool:
        return env.over_cap

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: env.llm)
    return env


def _input(**overrides: Any) -> MemoryAskInput:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "ns",
        "agent": "*",
        "query": "what is my favorite color",
    }
    base.update(overrides)
    return MemoryAskInput(**base)


async def _run(**overrides: Any):
    from server.memory.app.ask.handler import handle_memory_ask

    return await handle_memory_ask(_input(**overrides))


async def test_retrieval_abstains_returns_abstained(ask_env) -> None:
    ask_env.search_output = _search_output([], abstain=True, reason="nothing above the noise floor")
    out = await _run()
    assert out.source == "abstained"
    assert out.abstain is True
    assert out.answer == ""
    assert out.abstain_reason == "nothing above the noise floor"
    assert out.memory_keys == []
    # No synthesis was attempted.
    assert ask_env.llm.calls == []


async def test_empty_results_returns_abstained(ask_env) -> None:
    ask_env.search_output = _search_output([])  # not flagged abstain, just empty
    out = await _run()
    assert out.source == "abstained"
    assert out.abstain is True


async def test_allow_synthesis_false_returns_retrieval_only(ask_env) -> None:
    ask_env.search_output = _search_output(
        [_hit("m1", excerpt="favorite color is blue"), _hit("m2", excerpt="favorite food is sushi")]
    )
    out = await _run(allow_synthesis=False)
    assert out.source == "retrieval_only"
    assert out.abstain is False
    assert out.answer == ""
    assert out.memory_keys == ["m1", "m2"]
    assert [h.memory_key for h in out.results] == ["m1", "m2"]
    # retrieval_only never builds the client.
    assert ask_env.llm.calls == []


async def test_happy_path_synthesizes_with_citations(ask_env) -> None:
    ask_env.search_output = _search_output(
        [_hit("m1", excerpt="favorite color is blue"), _hit("m2", excerpt="favorite food is sushi")]
    )
    out = await _run()
    assert out.source == "synthesized"
    assert out.abstain is False
    assert out.answer == "Your favorite color is blue [m1]."
    # Citations = the memory_keys the answer referenced.
    assert out.memory_keys == ["m1"]
    # Raw sources still surfaced.
    assert [h.memory_key for h in out.results] == ["m1", "m2"]
    assert len(ask_env.llm.calls) == 1


async def test_synthesis_without_markers_cites_all_grounding(ask_env) -> None:
    ask_env.llm = _FakeLLM("Blue, and sushi.")  # no [key] markers emitted
    ask_env.search_output = _search_output(
        [_hit("m1", excerpt="color is blue"), _hit("m2", excerpt="food is sushi")]
    )
    out = await _run()
    assert out.source == "synthesized"
    assert out.memory_keys == ["m1", "m2"]


async def test_over_cap_returns_retrieval_only_budget_capped(ask_env) -> None:
    ask_env.over_cap = True
    ask_env.search_output = _search_output([_hit("m1", excerpt="favorite color is blue")])
    out = await _run()
    assert out.source == "retrieval_only"
    assert out.abstain is False
    assert out.answer == ""
    assert out.abstain_reason == "budget_capped"
    assert out.memory_keys == ["m1"]
    # The whole point: capped must NOT silently look like empty synthesis, and
    # must never build/call the client.
    assert out.source != "synthesized"
    assert ask_env.llm.calls == []


async def test_identity_sentinels_not_forwarded_as_filters(ask_env) -> None:
    # user_id / run_id left at the identity SENTINEL ("-") must reach the
    # search as None (no filter), not as a literal "-".
    ask_env.search_output = _search_output([_hit("m1", excerpt="x")])
    await _run(allow_synthesis=False)
    si = ask_env.search_inputs[-1]
    assert si.user_id is None
    assert si.run_id is None
    # Unset scope stays unset so the search handler applies the deployment
    # default ("this"); the ask model does not pin all_mine.
    assert si.namespace_scope is None
    assert si.search_mode == "rrf"  # the wire default, on both models
    # …but NOT pinned: an unpinned ask must let memory_search_default_mode
    # decide, exactly like an unpinned memory.search.
    assert "search_mode" not in si.model_fields_set
    assert si.auto_touch is False


async def test_a_pinned_search_mode_on_the_ask_reaches_the_search_pinned(ask_env) -> None:
    ask_env.search_output = _search_output([_hit("m1", excerpt="x")])
    await _run(allow_synthesis=False, search_mode="hybrid")
    si = ask_env.search_inputs[-1]
    assert si.search_mode == "hybrid" and "search_mode" in si.model_fields_set


async def test_a_pinned_lever_switch_on_the_ask_reaches_the_search_pinned(ask_env) -> None:
    ask_env.search_output = _search_output([_hit("m1", excerpt="x")])
    await _run(allow_synthesis=False)
    assert "rrf_adaptive_lexical" not in ask_env.search_inputs[-1].model_fields_set
    await _run(allow_synthesis=False, rrf_adaptive_lexical=False)
    si = ask_env.search_inputs[-1]
    assert si.rrf_adaptive_lexical is False and "rrf_adaptive_lexical" in si.model_fields_set


async def test_grounding_absent_with_results_synthesizes_with_hedge(ask_env) -> None:
    """entity_absent (identifier-grounding) + on-topic results must SYNTHESIZE
    (with an LLM hedge) rather than return silence — the cosine guard can't
    tell 'genuine content, id not cited' from 'wrong entity', so the LLM judges."""
    ask_env.search_output = _search_output(
        [_hit("m1", excerpt="Arrow flattened Tuple subcolumn read from a nullable struct")],
        abstain=True,
        reason="entity_absent:109741",
    )
    ask_env.search_output.abstain_code = "entity_absent"
    out = await _run(query="what do we know about PR 109741")
    assert out.source == "synthesized"
    assert out.abstain is False
    assert ask_env.llm.calls, "should have synthesized, not abstained"
    system_prompt = ask_env.llm.calls[0][0]
    assert "109741" in system_prompt  # hedge names the ungrounded identifier


async def test_non_grounding_abstain_with_rows_runs_synthesis(ask_env) -> None:
    """A weak-retrieval abstain (not entity_absent) with rows in
    hand goes to the synthesizer, which keeps its own right to decline."""
    ask_env.search_output = _search_output(
        [_hit("m1", excerpt="unrelated")],
        abstain=True,
        reason="below threshold",
    )
    ask_env.search_output.abstain_code = "below_threshold"
    out = await _run()
    assert out.source == "synthesized"
    assert ask_env.llm.calls != []


async def test_synth_model_setting_routes_to_per_model_client(ask_env, monkeypatch) -> None:
    """LOOM_MEMORY_ASK_SYNTH_MODEL routes synthesis through the per-model
    client factory (a stronger synth model, independent of extraction)."""
    import server.llm.client as llm_client
    from server.settings import settings

    captured: dict[str, str] = {}

    def fake_for_model(model):
        captured["model"] = model
        return ask_env.llm

    monkeypatch.setattr(llm_client, "get_llm_client_for_model", fake_for_model)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "gpt-4o", raising=False)
    ask_env.search_output = _search_output([_hit("m1", excerpt="blue")])
    out = await _run()
    assert out.source == "synthesized"
    assert captured.get("model") == "gpt-4o"  # used the dedicated synth model


async def test_identifier_injection_surfaces_missing_entity(ask_env, monkeypatch) -> None:
    """A query naming PR 109741 where recall returned a DIFFERENT PR (entity_absent
    abstain) must inject 109741's memory (exact substring) into synthesis and
    answer it — not hedge — because loom's lexical leg can't match bare numbers."""
    import server.memory.store as mr

    async def fake_find(**kw):
        assert "109741" in kw["identifiers"]
        return [
            {
                "memory_key": "pr_109741",
                "memory_type": "semantic",
                "content_excerpt": "PR #109741 fixes Arrow flattened Tuple subcolumn read",
                "created_at": "2026-09-01 00:00:00",
            }
        ]

    monkeypatch.setattr(mr, "find_by_identifier", fake_find)
    ask_env.search_output = _search_output(
        [_hit("other_pr", excerpt="PR #109726 unrelated change")],
        abstain=True,
        reason="entity_absent:109741",
    )
    ask_env.search_output.abstain_code = "entity_absent"
    out = await _run(query="what do we know about PR 109741")
    assert out.source == "synthesized"
    # The injected memory reached the synthesizer (its content is in the prompt).
    synth_user = ask_env.llm.calls[0][1]
    assert "109741" in synth_user and "Arrow flattened Tuple" in synth_user
    # Injection covered the id, so NO hedge clause was added to the system prompt.
    assert "not cited verbatim" not in ask_env.llm.calls[0][0]


async def test_identifier_injection_still_hedges_when_truly_absent(ask_env, monkeypatch) -> None:
    """If the identifier genuinely isn't in the corpus (injection finds nothing),
    the honest hedge still fires."""
    import server.memory.store as mr

    async def fake_find(**kw):
        return []  # 117758 truly absent

    monkeypatch.setattr(mr, "find_by_identifier", fake_find)
    ask_env.search_output = _search_output(
        [_hit("rel", excerpt="a related but different issue")],
        abstain=True,
        reason="entity_absent:117758",
    )
    ask_env.search_output.abstain_code = "entity_absent"
    out = await _run(query="what do we know about issue 117758")
    assert out.source == "synthesized"
    assert "not cited verbatim" in ask_env.llm.calls[0][0]  # hedge fired


async def test_next_hint_is_set_only_when_the_ask_carries_a_run_id(ask_env) -> None:
    """Protocol nudge: under a run, the response says how to
    report which hits were relied on; without one it costs no tokens."""
    ask_env.search_output = _search_output([_hit("m1", excerpt="favorite color is blue")])
    plain = await _run(allow_synthesis=False)
    assert plain.next == ""
    under_run = await _run(allow_synthesis=False, run_id="run-7")
    assert under_run.next.startswith("run_id=run-7") and "memories_used" in under_run.next
    # the run_id also scoped retrieval (existing behaviour, still true)
    assert ask_env.search_inputs[-1].run_id == "run-7"
    # an abstained ask has nothing to report on
    ask_env.search_output = _search_output([], abstain=True, reason="nothing")
    assert (await _run(run_id="run-7")).next == ""


async def test_memory_type_is_forwarded_to_retrieval(ask_env) -> None:
    """None keeps reflections out of the answer; "reflection" asks over them
    only. The filter is retrieval's job, so ask just forwards."""
    ask_env.search_output = _search_output([_hit("reflection:r1", excerpt="pin the image")])
    await _run(allow_synthesis=False)
    assert ask_env.search_inputs[-1].memory_type is None
    await _run(allow_synthesis=False, memory_type="reflection")
    assert ask_env.search_inputs[-1].memory_type == "reflection"


# ---- two-tier synthesis cascade (LOOM_MEMORY_ASK_SYNTH_MODEL_CHEAP) ---------
# The handler builds a cheap client alongside the strong one when the setting
# names a cheap model, reads budget PRESSURE (ratio) instead of the binary cap,
# and the service routes / escalates per memory.domain.synth_policy.


def _easy_output(hits: list[_MemorySearchHit]) -> MemorySearchOutput:
    return MemorySearchOutput(
        results=hits,
        embedding_model="fake-embed",
        retrieval_confidence=0.9,
        retrieval_risk="low",
        route_agreement=0.8,
    )


@pytest.fixture
def cascade_env(ask_env, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.observability.budget as budget
    from server.settings import settings

    ask_env.strong = _FakeLLM("Your favorite color is blue [m1].")
    ask_env.strong.model = "strong-model"
    ask_env.cheap = _FakeLLM("Blue [m1].")
    ask_env.cheap.model = "cheap-model"
    ask_env.budget_ratio = 0.1

    def for_model(model: str | None) -> Any:
        return {"strong-model": ask_env.strong, "cheap-model": ask_env.cheap}[model]

    async def fake_ratio(org: str, ns: str) -> float:
        return ask_env.budget_ratio

    monkeypatch.setattr(llm_client, "get_llm_client_for_model", for_model)
    monkeypatch.setattr(budget, "namespace_budget_ratio", fake_ratio)
    monkeypatch.setattr(settings, "memory_ask_synth_model", "strong-model")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "cheap-model")
    ask_env.search_output = _easy_output([_hit("m1", excerpt="favorite color is blue")])
    return ask_env


async def test_cascade_off_reports_single_tier(ask_env) -> None:
    ask_env.search_output = _easy_output([_hit("m1", excerpt="favorite color is blue")])
    out = await _run()
    assert out.source == "synthesized"
    assert out.route_tier == "strong"
    assert out.route_reason == "single_tier"
    assert out.synth_model == "fake-llm"
    assert out.escalated is False


async def test_easy_question_answered_by_cheap_model(cascade_env) -> None:
    out = await _run()
    assert out.source == "synthesized"
    assert out.answer == "Blue [m1]."
    assert out.route_tier == "cheap"
    assert out.route_reason == "easy_question"
    assert out.synth_model == "cheap-model"
    assert out.escalated is False
    assert out.memory_keys == ["m1"]
    assert len(cascade_env.cheap.calls) == 1
    assert cascade_env.strong.calls == []


async def test_hard_question_goes_straight_to_strong(cascade_env) -> None:
    out = await _run(query="which colors and foods do I like")
    assert out.route_tier == "strong"
    assert out.route_reason == "breadth_query"
    assert out.synth_model == "strong-model"
    assert cascade_env.cheap.calls == []
    assert len(cascade_env.strong.calls) == 1


async def test_uncited_cheap_answer_escalates_once(cascade_env) -> None:
    cascade_env.cheap._text = "I think blue, but I am not sure."
    out = await _run()
    assert out.answer == "Your favorite color is blue [m1]."
    assert out.route_tier == "strong"
    assert out.route_reason == "easy_question:cheap_uncited"
    assert out.synth_model == "strong-model"
    assert out.escalated is True
    assert len(cascade_env.cheap.calls) == 1
    assert len(cascade_env.strong.calls) == 1


async def test_hedged_cheap_answer_escalates(cascade_env) -> None:
    cascade_env.cheap._text = "I have no memory specifically about that [m1]."
    out = await _run()
    assert out.escalated is True
    assert out.route_reason == "easy_question:cheap_hedged"


async def test_budget_pressure_routes_cheap_without_escalation(cascade_env) -> None:
    cascade_env.budget_ratio = 0.85
    cascade_env.cheap._text = "Not sure."  # would normally escalate
    out = await _run(query="which colors and foods do I like")  # would be strong
    assert out.route_tier == "cheap"
    assert out.route_reason == "budget_pressure"
    assert out.escalated is False
    assert out.answer == "Not sure."
    assert cascade_env.strong.calls == []


async def test_ratio_at_or_over_one_is_capped(cascade_env) -> None:
    cascade_env.budget_ratio = 1.0
    out = await _run()
    assert out.source == "retrieval_only"
    assert out.abstain_reason == "budget_capped"
    assert out.route_tier == ""
    assert cascade_env.cheap.calls == [] and cascade_env.strong.calls == []


async def test_same_cheap_and_strong_model_collapses_to_single_tier(
    cascade_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "strong-model")
    out = await _run()
    assert out.route_reason == "single_tier"
    assert out.synth_model == "strong-model"


def test_keys_cited_accepts_hash_and_backtick_markers() -> None:
    """gpt-4o-mini writes [#key] / [`key`] for a key it was shown as [key];
    43 of 60 cheap answers on a bench replay did, and read as uncited."""
    from server.memory.app.ask.result import _keys_cited

    mems = [_hit("user_task_deadline", excerpt="x"), _hit("other", excerpt="y")]
    assert _keys_cited("Deadline is 2026-09-06. [#user_task_deadline]", mems) == [
        "user_task_deadline"
    ]
    assert _keys_cited("See [`other`] and [# user_task_deadline ]", mems) == [
        "user_task_deadline",
        "other",
    ]
    assert _keys_cited("no markers here", mems) == []
    assert _keys_cited("dated [2026-09-03] only", mems) == []


async def test_synth_model_reports_the_model_that_answered(cascade_env) -> None:
    """A runtime fallback (LOOM_LLM_FALLBACK_MODEL) can serve a call from a
    different model than the client's nominal one; the result's model wins."""

    async def served_by_other(system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        return SimpleNamespace(text="Blue [m1].", model="fallback-model")

    cascade_env.cheap.complete = served_by_other  # type: ignore[method-assign]
    out = await _run()
    assert out.route_tier == "cheap"
    assert out.synth_model == "fallback-model"


async def test_a_contradicted_verdict_marks_the_cited_memory(ask_env, monkeypatch) -> None:
    """The CONTRADICTED verdict names a cited key; the handler
    records a `contradicted` usage event on that memory and nothing else."""
    import asyncio

    from server.memory.app import answer_consistency as ac
    from server.memory.store import usage as memory_repo_usage
    from server.settings import settings

    ask_env.search_output = _search_output([_hit("m1", excerpt="favorite color is blue")])
    monkeypatch.setattr(settings, "memory_ask_consistency_check", True, raising=False)
    # This test reads the verdict off the wire: pin the inline judge (the
    # default is `deferred`; the deferred funnel is
    # covered in test_answer_consistency).
    monkeypatch.setattr(settings, "memory_ask_consistency_mode", "inline", raising=False)

    async def fake_check(llm, question, answer, cited):
        return ac.Verdict(
            label="contradicted", p_entail=0.1, p_contradict=0.9, contradicted_key="m1", span="blue"
        )

    monkeypatch.setattr(ac, "check", fake_check)
    recorded: list[tuple] = []

    async def fake_record(org, ns_keys, *, event_type, agent="", ch=None):
        recorded.append((org, ns_keys, event_type, agent))
        return 1

    monkeypatch.setattr(memory_repo_usage, "record_usage_for_keys", fake_record)
    # A named reader: `*` is a measurement label and marks nothing.
    out = await _run(agent="reader")
    await asyncio.sleep(0)  # let the background credit tasks run
    assert out.consistency == "contradicted" and "m1" in out.consistency_note
    marks = [r for r in recorded if r[2] == "contradicted"]
    assert marks == [("o", {"ns": ["m1"]}, "contradicted", "reader")]


async def test_an_entailed_verdict_marks_nothing(ask_env, monkeypatch) -> None:
    import asyncio

    from server.memory.app import answer_consistency as ac
    from server.memory.store import usage as memory_repo_usage
    from server.settings import settings

    ask_env.search_output = _search_output([_hit("m1", excerpt="favorite color is blue")])
    monkeypatch.setattr(settings, "memory_ask_consistency_check", True, raising=False)

    async def fake_check(llm, question, answer, cited):
        return ac.Verdict(label="entailed", p_entail=0.95, p_contradict=0.02)

    monkeypatch.setattr(ac, "check", fake_check)
    recorded: list[str] = []

    async def fake_record(org, ns_keys, *, event_type, agent="", ch=None):
        recorded.append(event_type)
        return 1

    monkeypatch.setattr(memory_repo_usage, "record_usage_for_keys", fake_record)
    await _run()
    await asyncio.sleep(0)
    assert "contradicted" not in recorded


async def test_option_completion_lands_in_the_window_with_the_facts_clock(
    ask_env, monkeypatch
) -> None:
    """Participation Knowledge_updating 378: recall returned more rows than the
    synthesis window, a chunk carried the superseded 'corporate paralegal' turn,
    and the fact 'corporate attorney' was fetched by option completion — but
    appended past the window with created_at (the 2026 ingest clock) as its
    clock, so neither the reader nor the latest-statement verifier saw it. The
    fetched fact must be prepended and carry its own valid_at."""
    import server.memory.store as mr
    from server import settings

    monkeypatch.setattr(settings, "memory_ask_context_memories", 4, raising=False)

    async def fake_find(**kw):
        return [
            {
                "memory_key": "nolan_profession",
                "memory_type": "semantic",
                "content_excerpt": '"Nolan works as a corporate attorney at Pioneer Legal Group LLC." (#nolan_profession)',
                "created_at": "2026-09-12 18:42:45",
                "valid_at": "2024-10-05 08:00:00",
            }
        ]

    monkeypatch.setattr(mr, "find_by_identifier", fake_find)
    chunk = _hit(
        "chunk:s7:2",
        excerpt="[assistant @ 2024-10-05 07:55] Nice! [user @ 2024-10-05 07:56] My dad works as a corporate paralegal.",
    )
    filler = [_hit(f"f{i}", excerpt=f"the user's cousin number {i} likes hiking") for i in range(8)]
    ask_env.search_output = _search_output([chunk] + filler)
    ask_env.llm = _FakeLLM(
        "D) Corporate Paralegal — the user's latest direct statement [chunk:s7:2]"
    )
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: ask_env.llm)
    out = await _run(
        query=(
            "What is the position of my father?\nA) Corporate Accountant\nB) Corporate Attorney"
            "\nC) Corporate Consultant\nD) Corporate Paralegal"
        ),
        top_k=4,
    )
    assert out.source == "synthesized"
    assert "corporate attorney" in ask_env.llm.calls[0][1]  # the fact reached the reader
    assert out.answer.startswith("B) Corporate Attorney — the latest statement (2024-10-05 08:00)")


async def test_bridge_completion_brings_the_entitys_other_attributes(ask_env, monkeypatch) -> None:
    """Participation Post_processing 280: "the activity that lasts one day"
    retrieves `user_success_gala_duration`; the answer is that entity's
    location, a sibling key the query's words never reach. The siblings of
    the top hits' entities join the pool."""
    import server.memory.store as mr

    seen: dict[str, Any] = {}

    async def fake_prefix(**kw):
        # the first read is the top hits' bridge; the row hops that follow it
        # (fourth addendum) read the objects the bridged rows name
        if "prefixes" not in seen:
            seen.update(kw)
            return [
                {
                    "memory_key": "user_success_gala_location",
                    "memory_type": "semantic",
                    "content_excerpt": '"The Success Gala is happening in San Diego, CA." (#user_success_gala_location)',
                    "created_at": "2026-09-13 07:00:00",
                    "valid_at": "2024-10-03 09:00:00",
                }
            ]
        seen.setdefault("hops", []).append(list(kw.get("prefixes") or []))
        return []

    monkeypatch.setattr(mr, "find_by_key_prefix", fake_prefix)
    ask_env.search_output = _search_output(
        [
            _hit(
                "user_success_gala_duration",
                excerpt='"User found out that the Success Gala lasts only one day."',
            ),
            _hit("user_hobby", excerpt="the user likes hiking"),
        ]
    )
    out = await _run(
        query="Where is the activity that lasts one day held?\nA) San Diego, CA\nB) Boston, MA",
        top_k=2,
    )
    assert out.source == "synthesized"
    # `user` (from user_hobby) names no entity; the bare spelling rides along
    assert seen["prefixes"] == ["user_success_gala", "success_gala"]
    assert seen.get("hops") == [["san_diego"]]  # the row hop read the sibling's object once
    assert "San Diego" in ask_env.llm.calls[0][1]  # the sibling reached the reader


def test_bridge_prefixes_put_the_cued_attributes_entities_first() -> None:
    from server.memory.app.ask.completion import derive_key_prefixes

    # the three best hits keep their place; among the rest, the key carrying
    # the asked attribute (`_duration` for "lasts one day") comes first
    keys = [
        "user_fitfest_schedule",
        "user_trailcare_day_focus",
        "nolan_profession",
        "user_x_focus",
        "user_y_theme",
        "user_success_gala_duration",
    ]
    got = derive_key_prefixes(keys, "the activity that lasts one day")
    assert got[:2] == ["user_fitfest", "fitfest"]
    assert got.index("user_success_gala") < got.index("user_x")
    assert derive_key_prefixes(keys)[:2] == ["user_fitfest", "fitfest"]


def test_keys_cited_accepts_parenthesised_and_bare_hash_forms_but_only_window_keys() -> None:
    """gpt-4.1-mini wrote "(#key, #key2)" 659 times and a bare "#key" 661 times
    against 570 bracket forms over 1,986 LoCoMo answers; read as
    uncited, those escalated the cheap tier and blocked the precedent store."""
    from server.memory.app.ask.result import _keys_cited

    mems = [_hit("melanie_running_shoes", excerpt="x"), _hit("caroline_school_event", excerpt="y")]
    assert _keys_cited("Running shoes (#melanie_running_shoes, 2023-05-20).", mems) == [
        "melanie_running_shoes"
    ]
    assert _keys_cited("Both (#caroline_school_event, #melanie_running_shoes)", mems) == [
        "melanie_running_shoes",
        "caroline_school_event",
    ]  # grounding order, not answer order
    assert _keys_cited("Melanie got shoes #melanie_running_shoes.", mems) == [
        "melanie_running_shoes"
    ]
    assert _keys_cited("See #`caroline_school_event` for the event", mems) == [
        "caroline_school_event"
    ]
    # prose in parentheses and unknown hash tokens cite nothing
    assert _keys_cited("She ran (a lot) and posted #running on 2023-05-20.", mems) == []
    assert _keys_cited("No markers at all.", mems) == []


async def test_explicit_namespace_scope_passes_through_the_ask(ask_env) -> None:
    """An ask that says all_mine still gets the union; one that
    says nothing follows `memory_search_default_namespace_scope` downstream."""
    ask_env.search_output = _search_output([_hit("m1", excerpt="x")])
    await _run(allow_synthesis=False, namespace_scope="all_mine")
    assert ask_env.search_inputs[-1].namespace_scope == "all_mine"
    await _run(allow_synthesis=False, namespace_scope="this")
    assert ask_env.search_inputs[-1].namespace_scope == "this"


async def test_deep_pass_answers_the_scaffolded_question_not_the_stem(ask_env, monkeypatch) -> None:
    """When the temporal trigger fires the deep pass on a MemBench item, it
    must answer the same scaffolded question the first pass did: with the
    retrieval stem alone the reader never sees the options, no option program
    can read a letter, and the letterless answer replaces the lettered one."""
    from types import SimpleNamespace

    import server.memory.app.deep_evidence as mde

    async def fake_deep(**kw):
        return SimpleNamespace(
            block="RAW: [user @ 2024-10-01 08:00] my number is 65001367881",
            selected_keys=["maya_phone"],
            chars=60,
        )

    monkeypatch.setattr(mde, "deep_evidence_pass", fake_deep)
    ask_env.search_output = _search_output(
        [_hit("maya_phone", excerpt="Maya's phone number is 65001367881.")]
    )
    ask_env.llm = _FakeLLM("A) 33 — last six digits 367881 sum to 33 [maya_phone]")
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: ask_env.llm)
    q = (
        "Current date: 2024-10-05\nWhat is the sum of the last six digits of the contact number for the person who works as a farmer?"
        "\n\nChoose exactly one option and reply with its letter only.\nA) 33\nB) 30\nC) 31\nD) 36"
    )
    out = await _run(query=q, depth="raw", top_k=5)
    assert out.source == "synthesized" and out.deep_fired
    # every synthesis call — the deep one included — carried the option block
    # (one call under the deep-first order, two under the parallel start)
    assert len(ask_env.llm.calls) >= 1
    assert all("A) 33" in user and "D) 36" in user for _system, user in ask_env.llm.calls)
    # and the premise line stays off for a forced choice
    assert all("Premise:" not in system for system, _user in ask_env.llm.calls)
    assert out.answer.startswith("A) 33")


# ---- independent work starts before it is needed -------------


async def test_knowledge_lookup_overlaps_retrieval(ask_env, monkeypatch) -> None:
    """The knowledge plane's lookup starts before retrieval and is awaited only
    at synthesis, so retrieval begins while the lookup is still running."""
    import asyncio

    import server.memory.app.search as hsearch
    from server.memory.app.ask import pipeline

    order: list[str] = []
    inner_search = hsearch.handle_memory_search

    async def slow_knowledge(search_input, question, *, enabled=None):
        order.append("knowledge_start")
        await asyncio.sleep(0.05)
        order.append("knowledge_end")
        return "", 0

    async def observed_search(search_input):
        order.append("search_start")
        return await inner_search(search_input)

    monkeypatch.setattr(pipeline, "knowledge_passages", slow_knowledge)
    monkeypatch.setattr(hsearch, "handle_memory_search", observed_search)
    ask_env.search_output = _search_output([_hit("m1", excerpt="favorite color: blue")])
    out = await _run(decompose="off")
    assert out.source == "synthesized"
    # Retrieval started while the lookup was still running: the two overlap.
    assert order.index("search_start") < order.index("knowledge_end"), order


async def test_upfront_deep_selection_runs_beside_the_first_synthesis(ask_env, monkeypatch) -> None:
    """With depth="raw" the deep pass's trigger is known before the first
    synthesis, so its selection starts before that synthesis returns; the
    serial result is unchanged (same pool, same question)."""
    import asyncio

    from server.memory.app import deep_evidence as deep

    order: list[str] = []

    class _YieldingLLM(_FakeLLM):
        async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
            order.append("synth_start")
            await asyncio.sleep(0.02)
            order.append("synth_end")
            return await super().complete(system, user, max_tokens, **kw)

    async def fake_deep_pass(**kw):
        order.append("deep_start")
        assert kw["reason"] == "requested"
        await asyncio.sleep(0.01)
        order.append("deep_end")
        return deep.DeepEvidence()

    monkeypatch.setattr(deep, "deep_evidence_pass", fake_deep_pass)
    ask_env.llm = _YieldingLLM("Your favorite color is blue [m1].")
    import server.llm.client as llm_client

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: ask_env.llm)
    ask_env.search_output = _search_output([_hit("m1", excerpt="favorite color: blue")])
    out = await _run(decompose="off", depth="raw")
    assert out.source == "synthesized"
    assert order.count("deep_start") == 1, order  # the pre-started task is the one used
    assert order.index("deep_start") < order.index("synth_end"), order


def test_three_word_attributes_still_yield_the_entity_prefix() -> None:
    from server.memory.app.ask.completion import derive_key_prefixes

    got = derive_key_prefixes(
        ["user_run_for_good_one_day_event"], "the activity that lasts one day"
    )
    assert "user_run_for_good" in got and "run_for_good" in got


def test_kinship_and_row_object_prefixes() -> None:
    from types import SimpleNamespace as H

    from server.memory.app.ask.completion import kinship_prefixes, row_object_prefixes

    assert kinship_prefixes(
        ["[user @ 2024-10-01 08:00] My mom's phone number is 202-022-01042."]
    ) == ["user_mom", "user_mother"]
    rows = [
        H(content_excerpt="User read that basketball was created in the country of Soviet Union."),
        H(content_excerpt="The capital of Italy is Duluth."),
    ]
    got = row_object_prefixes(rows, exclude={"basketball"})
    assert got[:2] == ["soviet_union", "italy"]


def test_unmatched_prefixes_names_the_entities_no_key_starts_with() -> None:
    """`soviet_union_` matches no key while "The capital of Soviet Union is
    Russellville" sits under
    `capital_of_soviet`; the prefixes without a key go to the text fallback."""
    from server.memory.app.ask.completion import unmatched_prefixes

    rows = [
        {"memory_key": "basketball_creation"},
        {"memory_key": "italy_capital_duluth"},
        {"memory_key": "user_serve_connect_duration"},
    ]
    assert unmatched_prefixes(
        [
            "basketball",
            "soviet_union",
            "italy",
            "serve_and_connect",
            "japan",
            "user",
            "capital_of",
            "author_of",
            "user_adalbert_of",
        ],
        rows,
    ) == ["soviet_union", "japan"]  # `user_serve_connect_duration` covers serve_and_connect
    assert unmatched_prefixes([], rows) == []
    # a prefix whose rows carry none of the question's cue words is not satisfied:
    # `soviet_union_head_of_state` matched while the capital sat under `capital_soviet`
    rows2 = [
        {
            "memory_key": "soviet_union_head_of_state",
            "content_excerpt": "The head of state in Soviet Union is Elizabeth II.",
        },
        {
            "memory_key": "italy_capital_duluth",
            "content_excerpt": "The capital of Italy is Duluth.",
        },
    ]
    assert unmatched_prefixes(["soviet_union", "italy"], rows2, ["capital", "city"]) == [
        "soviet_union"
    ]
    assert unmatched_prefixes(["soviet_union", "italy"], rows2) == []
    # an option city is a candidate answer, not an entity to expand
    assert unmatched_prefixes(
        ["chicago", "soviet_union"], rows2, ["capital"], ["Chicago, IL", "Boston, MA"]
    ) == ["soviet_union"]


def test_question_cues_keep_the_asked_attribute_and_drop_the_entities() -> None:
    from server.memory.app.ask.completion import question_cues

    assert question_cues(
        "What is the capital city of the country where the sport associated with John Harkes has its origin?"
    ) == ["capital", "sport", "associated", "origin"]
    assert question_cues(
        "Current date: 2024-01-01\nWhat is the job title of the chairperson of Fatah?"
    ) == [
        "job",
        "title",
        "chairperson",
    ]
    assert question_cues("") == []


def test_assistant_rows_are_labelled_only_when_the_setting_is_on(monkeypatch) -> None:
    """An `assistant_*` key or an assistant speaker
    marks the row as the assistant's own statement; chunks and summaries are
    never labelled; the label is off unless the setting says so."""
    from types import SimpleNamespace

    from server.memory.app.ask import rendering

    assert rendering._is_assistant_row(
        SimpleNamespace(memory_key="assistant_dubai_trip_schedule", speaker="")
    )
    assert rendering._is_assistant_row(
        SimpleNamespace(memory_key="narita_taxi_cost", speaker="assistant")
    )
    assert not rendering._is_assistant_row(
        SimpleNamespace(memory_key="user_recent_trip", speaker="user")
    )
    assert not rendering._is_assistant_row(
        SimpleNamespace(memory_key="chunk:answer_1:0", speaker="assistant")
    )


def test_first_person_questions_put_assistant_rows_last() -> None:
    from server.memory.app.ask.question_shapes import _is_first_person_question

    assert _is_first_person_question("How much will I save by taking the train?")
    assert _is_first_person_question("What is the order of the three trips I took?")
    assert _is_first_person_question("Which bike did my brother fix?")
    assert not _is_first_person_question("What is the capital of Italy?")
    assert not _is_first_person_question("Who directed The Beatles?")


def test_completion_rows_join_at_the_window_edge() -> None:
    from server.memory.app.ask.completion import _insert_at_window

    ranked = [f"r{i}" for i in range(40)]
    out = _insert_at_window(ranked, ["b1", "b2"], 25)
    assert out[25:27] == ["b1", "b2"] and out[:25] == ranked[:25] and out[27:] == ranked[25:]
    # a pool shorter than the window: the rows simply follow it
    assert _insert_at_window(["r0", "r1"], ["b1"], 25) == ["r0", "r1", "b1"]
    assert _insert_at_window([], ["b1"], 25) == ["b1"]
