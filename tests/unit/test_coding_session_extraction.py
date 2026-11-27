"""A coding plugin's session capture keeps reusable codebase knowledge, not the
session's own edits: the extractor's coding-session rules, the verifier's
codebase paragraph and the code-location rule."""

from __future__ import annotations

import json

import pytest

from server.llm.client import LLMResult
from server.memory.app import pipeline, pipeline_state
from server.memory.app.extract import ExtractionResult, extract_from_messages
from server.memory.app.extract.prompts import _extraction_base_prompt
from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS
from server.memory.domain import durable_facts
from server.memory.domain.extraction_prompts import (
    _CODING_SESSION_PROMPT_BLOCK,
    CODING_SESSION_ANCHOR,
    DURABILITY_VERIFIER_SYSTEM_PROMPT,
)
from server.settings import settings
from tests.unit.llm_fake import ScriptedLLM

_MESSAGES = [
    {"role": "user", "content": "Make the JSON provider serialize timedelta as seconds."},
    {"role": "assistant", "content": "`_default` in src/json/provider.py has a new branch."},
]


@pytest.mark.parametrize("compact", [False, True])
def test_the_coding_rules_end_the_prompt_of_a_coding_session_only(compact: bool) -> None:
    assert CODING_SESSION_ANCHOR not in _extraction_base_prompt(compact)
    prompt = _extraction_base_prompt(compact, agent_learnings=True, coding_session=True)
    assert prompt.endswith(_CODING_SESSION_PROMPT_BLOCK)


def test_the_coding_rules_keep_codebase_knowledge_and_leave_out_the_session_edits() -> None:
    block = _CODING_SESSION_PROMPT_BLOCK
    for kept in (
        "how a behaviour is reached",
        "a relation between definitions in different files",
        "an invariant, constraint or pitfall",
        "which test constrains a definition",
        "the cause of a bug",
    ):
        assert kept in block, kept
    for left_out in (
        "what this session changed or added",
        "exists only\n    in this working tree",
        "what one lookup answers",
        "Leave\nline numbers out",
    ):
        assert left_out in block, left_out
    assert '{"facts": []} is the right answer' in block


async def _calls_for_no_facts(monkeypatch: pytest.MonkeyPatch, coding_session: bool) -> ScriptedLLM:
    llm = ScriptedLLM([json.dumps({"facts": []})], repeat_last=True)
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


@pytest.mark.asyncio
async def test_a_conversation_with_no_facts_is_still_asked_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = await _calls_for_no_facts(monkeypatch, coding_session=False)
    assert len(llm.calls) == 2
    assert CODING_SESSION_ANCHOR not in llm.calls[0].system


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
        state = pipeline_state.WorkflowState(
            org="o",
            namespace="n",
            agent="a",
            session_id="s",
            messages=[{"role": "user", "content": "hi"}],
            extra_tags=tags,
            extraction_instructions=instructions,
        )
        await pipeline._REGISTRY["loom.llm_extract"](state, {})
    assert seen == [True, True, False, False]


def test_the_verifier_keeps_codebase_knowledge_and_drops_the_session_edits() -> None:
    prompt = DURABILITY_VERIFIER_SYSTEM_PROMPT
    assert "could reuse instead of reading the code again" in prompt
    assert "kept whoever worked it out and from however many lookups" in prompt
    assert "DROP a fact that reports what this session edited or added" in prompt
    assert "until the edit is committed it exists in one working tree" in prompt
    assert "DROP a fact one lookup answers" in prompt
    assert "and a codebase fact of the kind kept above" in prompt


@pytest.mark.parametrize(
    "text",
    [
        "The scoring code without I/O is in server/tracker/domain/dupcheck.py.",
        "`Blueprint.register` is in src/flask/blueprints.py.",
        "The JSON tests are in `tests/test_json.py`",
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
        "`send` in src/client.py is the only caller of `backoff`, whose tests are in "
        "tests/test_backoff.py.",
        "The handler lives in server/x.py and takes the key lock before it reads.",
    ],
)
def test_a_claim_that_also_says_where_a_definition_is_may_be_durable(text: str) -> None:
    assert durable_facts.non_durable_class({"value": text}) is None
