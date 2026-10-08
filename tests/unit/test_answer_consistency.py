"""memory.ask audits its answer against its citations.

Pure parts (verdict parsing, the Consistency Score, conflict-pair folding,
the prompt note) plus the ask() flow with faked retrieval and LLMs: the
verdict rides the wire, a CONTRADICTED cheap answer escalates once and is
re-audited, an entailed one does not, and every judge failure is open.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchOutput, _MemorySearchHit
from server.memory.app import answer_consistency as ac

# ---------------------------------------------------------------- pure parts


def test_parse_verdict_tolerates_prose_and_clamps() -> None:
    v = ac.parse_verdict(
        'Sure. {"label": "Contradicted", "p_entail": -0.2, "p_contradict": 1.7, '
        '"contradicted_key": "m2", "span": "PR 117959 was merged"} thanks'
    )
    assert v.label == ac.CONTRADICTED and v.contradicted
    assert (v.p_entail, v.p_contradict) == (0.0, 1.0)
    assert v.score == 0.0
    assert v.contradicted_key == "m2" and "117959" in v.span


def test_parse_verdict_label_only_sets_probabilities() -> None:
    assert ac.parse_verdict('{"label": "entailed"}').score == 1.0
    assert ac.parse_verdict('{"label": "neutral"}').score == 0.5
    assert ac.parse_verdict('{"label": "contradicted"}').score == 0.0


def test_parse_verdict_unknown_is_not_ran() -> None:
    for bad in ("", "no json here", '{"label": "maybe"}', "[1,2]", '{"p_entail": 1}'):
        v = ac.parse_verdict(bad)
        assert not v.ran and v.label == "" and v.score == 0.0


def test_consistency_score_is_dsmart_formula() -> None:
    v = ac.Verdict(label=ac.NEUTRAL, p_entail=0.6, p_contradict=0.1)
    assert v.score == round((1 + 0.6 - 0.1) / 2, 3)


def test_build_user_prompt_shows_cited_only() -> None:
    m = SimpleNamespace(memory_key="m1", content_excerpt="x" * 5000, valid_at="2026-09-01 00:00:00")
    text = ac.build_user_prompt("q", "a", [m])
    assert "[m1] [2026-09-01]" in text and len(text) < 2000


@pytest.mark.asyncio
async def test_check_fails_open() -> None:
    class Boom:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    v = await ac.check(
        Boom(), "q", "answer", [SimpleNamespace(memory_key="m", content_excerpt="c")]
    )
    assert not v.ran
    assert not (await ac.check(None, "q", "a", [])).ran


# ---------------------------------------------------------------- ask() flow


class _FakeLLM:
    provider = "fake"

    def __init__(self, model: str, replies: list[str]) -> None:
        self.model = model
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **kw: Any) -> Any:
        self.calls.append((system, user))
        text = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        return SimpleNamespace(text=text, model=self.model)


def _hit(key: str, excerpt: str) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=0.2,
        similarity=0.9,
        rank_score=0.9,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings

    e = SimpleNamespace()
    e.search_output = MemorySearchOutput(
        results=[_hit("m1", "PR 117959 is still open, waiting on CI.")],
        embedding_model="fake-embed",
    )
    e.strong = _FakeLLM("strong", ["PR 117959 is open, waiting on CI [m1]."])
    e.cheap = _FakeLLM("cheap", ["PR 117959 was merged yesterday [m1]."])

    async def fake_search(search_input: Any) -> MemorySearchOutput:
        return e.search_output

    async def ratio(org: str, ns: str) -> float:
        return 0.0

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_budget_ratio", ratio)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.strong)
    monkeypatch.setattr(
        llm_client, "get_llm_client_for_model", lambda m: e.cheap if m == "cheap" else e.strong
    )
    monkeypatch.setattr(settings, "memory_ask_synth_model", "strong")
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "cheap")
    monkeypatch.setattr(settings, "memory_ask_consistency_check", True)
    # The judge runs `deferred` by default; the inline-path tests pin `inline`
    # here and the deferred tests set the mode themselves.
    monkeypatch.setattr(settings, "memory_ask_consistency_mode", "inline")
    monkeypatch.setattr(settings, "memory_ask_recency_weight", 0.0)
    monkeypatch.setattr(settings, "memory_ask_code_enrichment", False)
    monkeypatch.setattr(settings, "memory_ask_code_staleness", False)
    monkeypatch.setattr(settings, "memory_ask_identifier_injection", False)
    return e


def _ask() -> MemoryAskInput:
    return MemoryAskInput(org="o", namespace="ns", agent="*", query="is PR 117959 merged?")


@pytest.mark.asyncio
async def test_entailed_answer_does_not_escalate_and_rides_the_wire(env, monkeypatch) -> None:
    from server.memory.app.ask.handler import handle_memory_ask
    from server.memory.domain import synth_policy

    monkeypatch.setattr(
        synth_policy,
        "decide",
        lambda f, **k: SimpleNamespace(tier="cheap", reason="easy", cheap=True),
    )
    env.cheap._replies = [
        "PR 117959 is open and waiting on CI [m1].",
        '{"label":"entailed","p_entail":0.9,"p_contradict":0.0}',
    ]
    out = await handle_memory_ask(_ask())
    assert out.escalated is False and out.route_tier == "cheap"
    assert out.consistency == "entailed" and out.consistency_score == 0.95
    assert env.strong.calls == []


@pytest.mark.asyncio
async def test_contradicted_strong_answer_is_reported_not_escalated(env, monkeypatch) -> None:
    """Single-tier deployments get the verdict and the note; there is no
    stronger model to escalate to."""
    from server.memory.app.ask.handler import handle_memory_ask
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    env.strong._replies = [
        "PR 117959 was merged [m1].",
        '{"label":"contradicted","p_entail":0.1,"p_contradict":0.8,"contradicted_key":"m1","span":"was merged"}',
    ]
    out = await handle_memory_ask(_ask())
    assert out.route_tier == "strong" and out.escalated is False
    assert out.consistency == "contradicted"
    assert out.consistency_note == "contradicts [m1]: was merged"
    assert len(env.strong.calls) == 2


@pytest.mark.asyncio
async def test_check_off_leaves_wire_empty(env, monkeypatch) -> None:
    from server.memory.app.ask.handler import handle_memory_ask
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_consistency_check", False)
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    out = await handle_memory_ask(_ask())
    assert out.consistency == "" and out.consistency_score == 0.0
    assert len(env.strong.calls) == 1


@pytest.mark.asyncio
async def test_contradicted_cheap_answer_is_reported_without_resynthesis(env, monkeypatch) -> None:
    """A contradiction verdict is reported only; the cheap answer stands.

    Escalating rarely helps: 5 of 6 contradicted answers stayed wrong under the
    strong model because the memories themselves were stale."""
    from server.memory.app.ask.handler import handle_memory_ask
    from server.memory.domain import synth_policy

    monkeypatch.setattr(
        synth_policy,
        "decide",
        lambda f, **k: SimpleNamespace(tier="cheap", reason="easy", cheap=True),
    )
    env.cheap._replies = [
        "PR 117959 was merged yesterday [m1].",
        '{"label":"contradicted","p_entail":0.05,"p_contradict":0.9,"contradicted_key":"m1","span":"was merged yesterday"}',
    ]
    out = await handle_memory_ask(_ask())
    assert out.escalated is False and out.route_tier == "cheap"
    assert (
        out.consistency == "contradicted"
        and out.consistency_note == "contradicts [m1]: was merged yesterday"
    )
    assert env.strong.calls == [] and len(env.cheap.calls) == 2


# ---------------------------------------------------------------------------
# The judge can run after the response.
# ---------------------------------------------------------------------------


async def _drain_background_tasks() -> None:
    from server.memory.app import after_write

    await after_write.drain()


@pytest.mark.asyncio
async def test_deferred_mode_answers_first_and_lands_the_verdict_on_the_ledger(
    env, monkeypatch
) -> None:
    from server.memory.app.ask.handler import handle_memory_ask
    from server.memory.domain import synth_policy
    from server.observability import ledger
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_consistency_mode", "deferred")
    monkeypatch.setattr(
        synth_policy,
        "decide",
        lambda f, **k: SimpleNamespace(tier="cheap", reason="easy", cheap=True),
    )
    env.cheap._replies = [
        "PR 117959 is open and waiting on CI [m1].",
        '{"label":"entailed","p_entail":0.9,"p_contradict":0.0}',
    ]
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ledger, "emit_agent_event", lambda **kw: rows.append(kw))

    out = await handle_memory_ask(_ask())
    # The answer left without waiting on the judge: one cheap call so far
    # (synthesis), empty wire fields.
    assert out.answer.startswith("PR 117959 is open")
    assert out.consistency == "" and out.consistency_score == 0.0 and out.consistency_note == ""
    assert len(env.cheap.calls) == 1

    await _drain_background_tasks()
    # The judge ran after the response and its verdict is a ledger row keyed
    # to the request, with the label and score the inline mode would have put
    # on the wire.
    assert len(env.cheap.calls) == 2
    verdicts = [kw for kw in rows if kw.get("op_name") == "memory.ask.consistency"]
    assert len(verdicts) == 1
    row = verdicts[0]
    assert row["kind"] == "verdict" and row["success"] is True and row["error_code"] == ""
    assert row["result"] == {"consistency": "entailed", "consistency_score": 0.95}
    assert row["identity"]["org"] == "o" and row["identity"]["namespace"] == "ns"


@pytest.mark.asyncio
async def test_request_pins_inline_over_a_deferred_setting(env, monkeypatch) -> None:
    from server.memory.app.ask.handler import handle_memory_ask
    from server.memory.domain import synth_policy
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_consistency_mode", "deferred")
    monkeypatch.setattr(
        synth_policy,
        "decide",
        lambda f, **k: SimpleNamespace(tier="cheap", reason="easy", cheap=True),
    )
    env.cheap._replies = [
        "PR 117959 is open and waiting on CI [m1].",
        '{"label":"entailed","p_entail":0.9,"p_contradict":0.0}',
    ]
    inp = _ask().model_copy(update={"consistency": "inline"})
    out = await handle_memory_ask(inp)
    assert out.consistency == "entailed" and out.consistency_score == 0.95
    assert len(env.cheap.calls) == 2


@pytest.mark.asyncio
async def test_request_pins_deferred_over_an_inline_setting(env, monkeypatch) -> None:
    from server.memory.app.ask.handler import handle_memory_ask
    from server.memory.domain import synth_policy
    from server.observability import ledger
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_consistency_mode", "inline")
    monkeypatch.setattr(
        synth_policy,
        "decide",
        lambda f, **k: SimpleNamespace(tier="cheap", reason="easy", cheap=True),
    )
    env.cheap._replies = [
        "PR 117959 is open and waiting on CI [m1].",
        '{"label":"contradicted","p_entail":0.0,"p_contradict":1.0,"contradicted_key":"m1"}',
    ]
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ledger, "emit_agent_event", lambda **kw: rows.append(kw))
    out = await handle_memory_ask(_ask().model_copy(update={"consistency": "deferred"}))
    assert out.consistency == "" and len(env.cheap.calls) == 1
    await _drain_background_tasks()
    verdicts = [kw for kw in rows if kw.get("op_name") == "memory.ask.consistency"]
    assert verdicts and verdicts[0]["result"]["consistency"] == "contradicted"


@pytest.mark.asyncio
async def test_deferred_mode_is_a_noop_while_the_check_is_off(env, monkeypatch) -> None:
    from server.memory.app.ask.handler import handle_memory_ask
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_consistency_check", False)
    monkeypatch.setattr(settings, "memory_ask_consistency_mode", "deferred")
    out = await handle_memory_ask(_ask())
    assert out.consistency == "" and len(env.cheap.calls) == 0


def test_the_deployment_default_is_deferred() -> None:
    # Inline judging added ~680 ms of serial latency to most asks, and no
    # client read the wire label, so the verdict is computed off the hot path.
    from server.settings import Settings

    assert Settings.model_fields["memory_ask_consistency_mode"].default == "deferred"


@pytest.mark.asyncio
async def test_default_mode_answers_first_and_a_bench_pin_still_gets_the_label(
    env, monkeypatch
) -> None:
    """Under the shipped default the wire label is empty and the verdict lands
    on the ledger; a bench that reads the label pins `inline` per request."""
    from server.memory.app.ask.handler import handle_memory_ask
    from server.memory.domain import synth_policy
    from server.observability import ledger
    from server.settings import Settings, settings

    monkeypatch.setattr(
        settings,
        "memory_ask_consistency_mode",
        Settings.model_fields["memory_ask_consistency_mode"].default,
    )
    monkeypatch.setattr(
        synth_policy,
        "decide",
        lambda f, **k: SimpleNamespace(tier="cheap", reason="easy", cheap=True),
    )
    env.cheap._replies = [
        "PR 117959 is open and waiting on CI [m1].",
        '{"label":"entailed","p_entail":0.9,"p_contradict":0.0}',
    ]
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(ledger, "emit_agent_event", lambda **kw: rows.append(kw))
    out = await handle_memory_ask(_ask())
    assert out.consistency == "" and len(env.cheap.calls) == 1
    await _drain_background_tasks()
    assert [kw["op_name"] for kw in rows if kw.get("kind") == "verdict"] == [
        "memory.ask.consistency"
    ]
    # The replay bench's request shape: the label rides the wire again.
    from bench.route_replay import Frozen, Question, ask_input_for

    fz = Frozen(
        q=Question(qid="1", query="is PR 117959 merged?", org="o", namespace="ns", reference=""),
        search={},
    )
    env.cheap._replies = [
        "PR 117959 is open and waiting on CI [m1].",
        '{"label":"entailed","p_entail":0.9,"p_contradict":0.0}',
    ]
    out2 = await handle_memory_ask(ask_input_for(fz, 15))
    assert out2.consistency == "entailed"


def test_a_verdict_row_without_request_args_still_names_its_label(monkeypatch) -> None:
    """The deferred verdict row is emitted with no raw_args; its result must
    still be projected, or out_consistency is missing from every verdict row
    under the `deferred` default."""
    from server.observability import events as ev
    from server.observability import ledger

    emitted: list[Any] = []
    monkeypatch.setattr(ev, "get_emitter", lambda: SimpleNamespace(emit=emitted.append))
    monkeypatch.setattr(ledger, "get_emitter", lambda: SimpleNamespace(emit=emitted.append))
    ledger.emit_agent_event(
        op_name="memory.ask.consistency",
        identity={"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"},
        request_id="r1",
        latency_ms=5,
        success=True,
        error_code="",
        kind="verdict",
        result={"consistency": "contradicted", "consistency_score": 0.1},
    )
    assert emitted and emitted[0].args["out_consistency"] == "contradicted"
    assert emitted[0].args["out_consistency_score"] == "0.1"
