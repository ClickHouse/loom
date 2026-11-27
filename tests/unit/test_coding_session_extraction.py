"""A coding plugin's session capture keeps reusable codebase knowledge, not the
session's own edits: the extractor's coding-session rules, the calls it makes,
the verifier's prompt and the code-location rule."""

from __future__ import annotations

import json

import pytest

from server.llm.client import LLMResult
from server.memory.app import durability_verifier, pipeline, pipeline_state
from server.memory.app.extract import ExtractionResult, extract_from_messages
from server.memory.app.extract.prompts import _extraction_base_prompt
from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS
from server.memory.domain import durable_facts
from server.memory.domain.extraction_prompts import (
    _CODING_SESSION_PROMPT_BLOCK,
    _CODING_SESSION_RETRY_ADDENDUM,
    CODING_SESSION_ANCHOR,
)
from server.settings import settings
from tests.unit.llm_fake import ScriptedLLM

_MESSAGES = [
    {"role": "user", "content": "Make the JSON provider serialize timedelta as seconds."},
    {"role": "assistant", "content": "`_default` in src/json/provider.py has a new branch."},
]


@pytest.mark.parametrize("compact", [False, True])
def test_the_coding_rules_follow_the_base_prompt_of_a_coding_session_only(compact: bool) -> None:
    assert CODING_SESSION_ANCHOR not in _extraction_base_prompt(compact)
    prompt = _extraction_base_prompt(compact, agent_learnings=True, coding_session=True)
    assert prompt.endswith(_CODING_SESSION_PROMPT_BLOCK)


async def _calls_for_no_facts(
    monkeypatch: pytest.MonkeyPatch, coding_session: bool, *replies: str
) -> ScriptedLLM:
    llm = ScriptedLLM([*replies, '{"facts": []}'], repeat_last=True)
    monkeypatch.setattr(settings, "memory_extract_coverage_sweep_auto_min_uncovered", 0)
    monkeypatch.setattr("server.memory.app.extract.extract._extractor_client", lambda **_: llm)
    result = await extract_from_messages(
        _MESSAGES,
        operator_policy="",
        judge=False,
        coverage_sweep=False,
        coding_session=coding_session,
    )
    assert result.facts == []
    return llm


@pytest.mark.asyncio
async def test_a_coding_session_with_no_facts_is_not_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = await _calls_for_no_facts(monkeypatch, coding_session=True)
    assert len(llm.calls) == 1
    assert CODING_SESSION_ANCHOR in llm.calls[0].system


@pytest.mark.parametrize(
    "misfire",
    [
        # every fact object discarded: no key
        [json.dumps({"facts": [{"value": "The user deploys from the release branch."}]})] * 2,
        ["not json"],
    ],
)
@pytest.mark.asyncio
async def test_a_coding_session_misfire_is_asked_again_under_the_coding_rules(
    monkeypatch: pytest.MonkeyPatch, misfire: list[str]
) -> None:
    llm = await _calls_for_no_facts(monkeypatch, True, *misfire)
    first, retry = (call.system for call in llm.calls)
    assert retry == first + _CODING_SESSION_RETRY_ADDENDUM


@pytest.mark.asyncio
async def test_a_conversation_with_no_facts_is_still_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = await _calls_for_no_facts(monkeypatch, coding_session=False)
    assert len(llm.calls) == 2
    assert CODING_SESSION_ANCHOR not in llm.calls[0].system


def _capture(
    tags: list[str], instructions: str = "", messages: list[dict[str, object]] | None = None
) -> pipeline_state.WorkflowState:
    return pipeline_state.WorkflowState(
        org="o",
        namespace="n",
        agent="a",
        session_id="s",
        messages=messages or [{"role": "user", "content": "hi"}],
        extra_tags=tags,
        extraction_instructions=instructions,
    )


@pytest.mark.asyncio
async def test_only_a_plugin_session_capture_is_extracted_as_a_coding_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []

    async def _extract(messages: list[dict[str, object]], **kw: object) -> ExtractionResult:
        seen.append(bool(kw["coding_session"]))
        return ExtractionResult(
            facts=[],
            llm_result=LLMResult(text="", tokens_in=0, tokens_out=0, provider="p", model="m"),
            prompt_version="v",
        )

    monkeypatch.setattr("server.memory.app.extract.extract_from_messages", _extract)
    for tags, instructions in (
        (["source:claude-code"], ""),
        (["source:codex", "project:x"], ""),
        ([], ""),
        (["crystallized", "from_observation"], TRAJECTORY_EXTRACTION_INSTRUCTIONS),
    ):
        await pipeline._REGISTRY["loom.llm_extract"](_capture(tags, instructions), {})
    assert seen == [True, True, False, False]


@pytest.mark.asyncio
async def test_a_working_session_with_a_list_turn_and_no_facts_makes_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _inherit(org: str, namespace: str) -> None:
        return None

    report = "\n".join(
        f"- `step_{i}` in src/app/mod_{i}.py validates its input." for i in range(12)
    )
    messages: list[dict[str, object]] = [
        {"role": role, "content": text}
        for i in range(4)
        for role, text in (("user", f"Carry on with part {i}."), ("assistant", report))
    ]
    llm = ScriptedLLM([json.dumps({"facts": []})], repeat_last=True)
    monkeypatch.setattr("server.memory.app.coverage_sweep_gate.resolve", _inherit)
    monkeypatch.setattr("server.memory.app.extract.extract._extractor_client", lambda **_: llm)
    await pipeline._REGISTRY["loom.llm_extract"](
        _capture(["source:claude-code"], messages=messages), {}
    )
    assert len(llm.calls) == 1
    # The same turns as a conversation: the retry and the sweep follow the first pass.
    await pipeline._REGISTRY["loom.llm_extract"](_capture([], messages=messages), {})
    assert len(llm.calls) == 1 + 3


@pytest.mark.asyncio
async def test_only_a_session_capture_is_verified_by_the_codebase_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptedLLM([json.dumps({"verdicts": []})], repeat_last=True)
    monkeypatch.setattr(pipeline, "_verify_clients", lambda state: [llm])
    for state in (
        _capture(["source:claude-code"]),
        _capture(["from_observation"], TRAJECTORY_EXTRACTION_INSTRUCTIONS),
    ):
        state.extracted_facts = [{"key": "k", "value": "`a` in src/a.py calls `b` in src/b.py."}]
        await pipeline._REGISTRY["loom.verify_durable"](state, {})
    assert [call.system for call in llm.calls] == [
        durability_verifier.VERIFIER_SESSION_PROMPT,
        durability_verifier.VERIFIER_SYSTEM_PROMPT,
    ]
    assert llm.calls[0].system != llm.calls[1].system


@pytest.mark.parametrize(
    "text",
    [
        "The scoring code without I/O is in server/tracker/domain/dupcheck.py.",
        "`Blueprint.register` is in src/flask/blueprints.py.",
        "The JSON tests are in `tests/test_json.py`",
        "The function `register` is in src/flask/blueprints.py.",
    ],
)
def test_a_fact_that_is_one_code_location_is_not_durable(text: str) -> None:
    assert durable_facts.non_durable_class({"value": text}) == "session_action"


@pytest.mark.parametrize(
    "text",
    [
        "`DefaultJSONProvider` in src/flask/json/provider.py serializes other types through "
        "`_default` in the same file, and its docstring lists the supported types, which has "
        "to be kept in sync with `_default`. The JSON tests are in tests/test_json.py.",
        "`send` in src/client.py is the only caller of `backoff` whose tests are in "
        "tests/test_backoff.py.",
        "The tests that constrain `_default` in src/flask/json/provider.py are in "
        "tests/test_json.py.",
        "`send` is the only caller of `backoff` whose tests are in tests/test_backoff.py.",
        "The tests for `_default` are in tests/test_json.py.",
        "`_default`'s tests are in tests/test_json.py.",
        "The handler lives in server/x.py and takes the key lock before it reads.",
    ],
)
def test_a_claim_that_also_says_where_a_definition_is_may_be_durable(text: str) -> None:
    assert durable_facts.non_durable_class({"value": text}) is None
