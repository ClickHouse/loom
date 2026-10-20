"""Rules an assistant states about itself: parsed as `about="agent"`, kept out
of the memory write, and filed as learnings queue candidates an admin promotes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.llm import client as llm_client_module
from server.memory.app import assistant_learnings, pipeline
from server.memory.app.extract import extract_from_messages
from server.memory.app.extract.parse import _parse_fact_list
from server.memory.app.extract.prompts import _extraction_base_prompt
from server.memory.app.pipeline_state import WorkflowState
from server.memory.domain import assistant_rules
from server.memory.domain.extraction_prompts import AGENT_LEARNINGS_ANCHOR
from server.memory.store import learnings as learnings_repo
from server.settings import settings
from tests.unit.llm_fake import ScriptedLLM

_RULE = "I always escalate any refund over $500 to a human."
_MESSAGES = [
    {"role": "user", "content": "Can you refund my $800 order?"},
    {"role": "assistant", "content": f"{_RULE} Someone will reply within a day."},
]


def _rule_fact(**extra: Any) -> dict[str, Any]:
    return {
        "key": "assistant_rule_refund_escalation",
        "value": "The assistant escalates any refund over $500 to a human.",
        "about": "agent",
        "source_message_indices": [1],
        **extra,
    }


def test_the_parser_reads_whose_fact_it_is() -> None:
    raw = json.dumps(
        {
            "facts": [
                _rule_fact(),
                {"key": "sister_coffee", "value": "Her sister drinks decaf.", "about": "Other"},
                {"key": "user_city", "value": "The user lives in Lyon.", "about": "robot"},
                {"key": "user_dog", "value": "The user has a dog."},
            ]
        }
    )
    assert [f.about for f in _parse_fact_list(raw)] == ["agent", "other", "user", "user"]


@pytest.mark.parametrize("compact", [False, True])
def test_the_prompt_asks_for_assistant_rules_only_when_on(compact: bool) -> None:
    assert AGENT_LEARNINGS_ANCHOR not in _extraction_base_prompt(compact)
    assert AGENT_LEARNINGS_ANCHOR in _extraction_base_prompt(compact, agent_learnings=True)


def test_the_fake_provider_reads_the_same_anchor() -> None:
    from server.llm import fake

    assert fake._AGENT_RULES_ANCHOR == AGENT_LEARNINGS_ANCHOR


async def _extract(monkeypatch: pytest.MonkeyPatch, *, agent_learnings: bool) -> list[Any]:
    reply = json.dumps({"facts": [_rule_fact()]})
    llm = ScriptedLLM([reply], repeat_last=True)
    monkeypatch.setattr(settings, "memory_extract_coverage_sweep_auto_min_uncovered", 0)
    monkeypatch.setattr("server.memory.app.extract.extract._extractor_client", lambda **_: llm)
    result = await extract_from_messages(
        _MESSAGES, judge=False, coverage_sweep=False, agent_learnings=agent_learnings
    )
    assert (AGENT_LEARNINGS_ANCHOR in llm.calls[0].system) is agent_learnings
    return result.facts


@pytest.mark.asyncio
async def test_an_agent_fact_stays_one_only_when_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = await _extract(monkeypatch, agent_learnings=True)
    off = await _extract(monkeypatch, agent_learnings=False)
    assert [f.about for f in on] == ["agent"]
    # Without the block, the model's mark is ignored: the fact is the user's, as before.
    assert [f.about for f in off] == ["user"]


@pytest.mark.asyncio
async def test_the_fake_provider_files_rules_under_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "llm_model", "fake-echo-1")
    llm_client_module.reset()
    result = await extract_from_messages(
        _MESSAGES, judge=False, coverage_sweep=False, agent_learnings=True
    )
    rules = [f for f in result.facts if f.about == "agent"]
    assert [f.value for f in rules] == [f"Assistant rule: {_RULE}"]
    assert all("escalate any refund" not in f.value for f in result.facts if f.about != "agent")


def test_agent_learnings_follow_the_request_then_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "memory_extract_agent_learnings", True)
    assert pipeline._agent_learnings_on(WorkflowState()) is True
    assert pipeline._agent_learnings_on(WorkflowState(agent_learnings=False)) is False
    monkeypatch.setattr(settings, "memory_extract_agent_learnings", False)
    assert pipeline._agent_learnings_on(WorkflowState()) is False
    assert pipeline._agent_learnings_on(WorkflowState(agent_learnings=True)) is True
    # A coding session's assistant narrates its work: never.
    coding = WorkflowState(agent_learnings=True, extra_tags=["source:claude-code"])
    assert pipeline._agent_learnings_on(coding) is False


_VALUE = "The assistant escalates any refund over $500 to a human."


def _cid(value: str = _VALUE, agent: str = "support-bot") -> str:
    return assistant_rules.candidate_id("o", "ns", agent, "assistant_rule_refund_escalation", value)


def test_the_candidate_id_is_one_per_agent_key_and_rule_text() -> None:
    assert _cid() == _cid("the assistant  escalates any refund over $500 to a human")
    assert _cid() != _cid(agent="other-bot")
    # Another rule under the same key is a sibling, never a supporter of the first.
    assert _cid() != _cid("The assistant refunds anything without asking.")


def test_the_rationale_quotes_the_cited_evidence_first() -> None:
    assert assistant_rules.rationale(_rule_fact(evidence=_RULE), _MESSAGES) == (
        f'Stated in an assistant turn: "{_RULE}"'
    )
    # No evidence span: the cited assistant turn.
    assert assistant_rules.rationale(_rule_fact(), _MESSAGES) == (
        f'Stated in an assistant turn: "{_RULE} Someone will reply within a day."'
    )
    long = [{"role": "assistant", "content": "word " * 200}]
    assert len(assistant_rules.rationale(_rule_fact(source_message_indices=[0]), long)) < 440


def test_a_rule_keeps_its_key_whatever_the_value_names() -> None:
    from server.memory.app.extract.extract import _settle_facts
    from server.memory.app.extract.models import ExtractedFact

    # An identifier in a user fact's value re-keys it (`pr1234_...`); a rule keeps
    # the key the extractor gave, so every session files it under one key.
    rule = ExtractedFact(
        memory_key="assistant_rule_ticket",
        value="The assistant escalates ticket PR-1234 refunds to a human.",
        about="agent",
        source_message_indices=[1],
    )
    user = ExtractedFact(
        memory_key="user_rule_ticket",
        value="The user filed ticket PR-1234.",
        source_message_indices=[0],
    )
    out = _settle_facts(
        [rule, user],
        _MESSAGES,
        observation_date="2026-01-01",
        existing_memories=None,
        speaker_attribution=True,
    )
    keys = {f.about: f.memory_key for f in out}
    assert keys["agent"] == "assistant_rule_ticket"
    assert keys["user"] != "user_rule_ticket"


class _Queue:
    """learnings_repo over dicts: the candidate rows (newest version wins) and the
    supporter rows, one per (candidate, user, session)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.inserts: list[dict[str, Any]] = []
        self.supporters: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def find(self, org: str, namespace: str, cid: str) -> dict[str, Any] | None:
        row = self.rows.get(cid)
        return dict(row) if row else None

    async def insert(self, row: dict[str, Any]) -> None:
        self.inserts.append(row)
        cid = str(row["candidate_id"])
        if cid not in self.rows or row["version"] >= self.rows[cid]["version"]:
            self.rows[cid] = dict(row)

    async def insert_supporter(self, row: dict[str, Any]) -> None:
        self.supporters[(row["candidate_id"], row["user_id"], row["session_id"])] = dict(row)

    def support(self, cid: str) -> list[tuple[str, str]]:
        return sorted((u, s) for c, u, s in self.supporters if c == cid)


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _Queue:
    q = _Queue()
    monkeypatch.setattr(learnings_repo, "find_candidate", q.find)
    monkeypatch.setattr(learnings_repo, "insert_row", q.insert)
    monkeypatch.setattr(learnings_repo, "insert_supporter", q.insert_supporter)
    return q


def _state(user_id: str, session_id: str, **extra: Any) -> WorkflowState:
    return WorkflowState(
        org="o",
        namespace="ns",
        agent="support-bot",
        user_id=user_id,
        session_id=session_id,
        messages=list(_MESSAGES),
        **extra,
    )


async def _file(state: WorkflowState, fact: dict[str, Any] | None = None) -> int:
    return await assistant_learnings.file_assistant_rules(state, [fact or _rule_fact()])


@pytest.mark.asyncio
async def test_a_rule_is_filed_once_and_each_session_adds_one_supporter_row(queue: _Queue) -> None:
    assert await _file(_state("alice", "s1")) == 1
    (row,) = queue.rows.values()
    assert row == {
        "candidate_id": _cid(),
        "org": "o",
        "namespace": "ns",
        "agent": "support-bot",
        "filed_by_user": "alice",
        "memory_key": "assistant_rule_refund_escalation",
        "memory_type": "semantic",
        "proposed_value": json.dumps(_VALUE),
        "supporter_count": 0,
        "confidence_avg": 1.0,
        "supporters": [],
        "rationale": f'Stated in an assistant turn: "{_RULE} Someone will reply within a day."',
        "source_kind": "assistant_turn",
        "seeded_from_thread_id": "s1",
        "seeded_from_msg_id": None,
        "status": "pending",
        "proposed_at": row["proposed_at"],
        "version": 1,
    }
    await _file(_state("alice", "s1"))  # the same session again: the same supporter row
    await _file(_state("alice", "s2"))
    await _file(_state("bob", "s3"), _rule_fact(judge_verdict="UNCERTAIN"))
    # The candidate row was written once; support lives in its own rows.
    assert len(queue.inserts) == 1
    assert queue.support(_cid()) == [("alice", "s1"), ("alice", "s2"), ("bob", "s3")]
    assert queue.supporters[(_cid(), "bob", "s3")]["confidence"] == 0.75


@pytest.mark.asyncio
async def test_a_different_rule_under_one_key_is_a_sibling_candidate(queue: _Queue) -> None:
    await _file(_state("mallory", "s1"), _rule_fact(value="The assistant refunds anything."))
    await _file(_state("alice", "s2"))
    assert set(queue.rows) == {_cid("The assistant refunds anything."), _cid()}
    assert queue.support(_cid()) == [("alice", "s2")]


@pytest.mark.asyncio
async def test_a_supporter_racing_a_review_keeps_the_decision(
    queue: _Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _file(_state("alice", "s1"))
    pending = dict(queue.rows[_cid()])
    queue.rows[_cid()] = {**pending, "status": "rejected", "version": 10**18}

    async def stale_find(*_a: Any) -> dict[str, Any]:
        return pending  # read before the review landed

    monkeypatch.setattr(learnings_repo, "find_candidate", stale_find)
    await _file(_state("bob", "s2"))
    assert queue.rows[_cid()]["status"] == "rejected"
    assert len(queue.inserts) == 1  # support never writes a candidate row


@pytest.mark.asyncio
async def test_a_decided_candidate_takes_no_more_supporters(queue: _Queue) -> None:
    await _file(_state("alice", "s1"))
    queue.rows[_cid()]["status"] = "promoted"
    assert await _file(_state("bob", "s2")) == 0
    assert queue.support(_cid()) == [("alice", "s1")]


@pytest.mark.asyncio
async def test_a_pinned_token_files_for_its_own_agent(queue: _Queue) -> None:
    await _file(_state("alice", "s1", learning_agent="signed-bot"))
    (row,) = queue.rows.values()
    assert row["agent"] == "signed-bot"
    assert row["candidate_id"] == _cid(agent="signed-bot")


@pytest.mark.asyncio
async def test_a_failed_queue_write_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("Code: 210. NETWORK_ERROR")

    monkeypatch.setattr(learnings_repo, "find_candidate", broken)
    state = _state("alice", "s1")
    assert await _file(state) == 0
    assert state.errors == ["learnings_queue_failed:assistant_rule_refund_escalation"]


def _auth(**extra: Any) -> Any:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    return AuthInfo(
        subject="user:alice",
        org="o",
        namespaces=(),
        roles=(Role.WRITER,),
        is_service_account=False,
        **extra,
    )


def test_the_pin_comes_from_the_token() -> None:
    from server.auth.context import current_auth_var

    token = current_auth_var.set(_auth(read_user_id="alice", agent_principal="agent:signed-bot"))
    try:
        assert assistant_learnings.request_pin() == assistant_learnings.LearningsPin(
            pinned=True, agent="signed-bot"
        )
    finally:
        current_auth_var.reset(token)
    token = current_auth_var.set(_auth())
    try:
        assert assistant_learnings.request_pin() == assistant_learnings.LearningsPin()
    finally:
        current_auth_var.reset(token)
    # A deferred replay has no token: the pin stored at enqueue time.
    stored = assistant_learnings.LearningsPin(pinned=True, agent="signed-bot")
    with assistant_learnings.replay_pin(stored):
        assert assistant_learnings.request_pin() == stored


@pytest.mark.parametrize(("setting", "applied"), [(False, None), (True, True)])
@pytest.mark.asyncio
async def test_a_pinned_token_cannot_turn_filing_on(
    monkeypatch: pytest.MonkeyPatch, setting: bool, applied: bool | None
) -> None:
    from server.auth.context import current_auth_var
    from server.memory.api.models_bulk import (
        MemorySetFromMessagesInput,
        MemorySetFromMessagesOutput,
    )
    from server.memory.app.extract import handler

    seen: list[Any] = []

    async def fake_inner(input: Any) -> MemorySetFromMessagesOutput:
        seen.append(input.agent_learnings)
        return MemorySetFromMessagesOutput()

    monkeypatch.setattr(settings, "memory_extract_agent_learnings", setting)
    monkeypatch.setattr(handler, "_set_from_messages", fake_inner)
    body = MemorySetFromMessagesInput(
        org="o",
        namespace="ns",
        agent="any-bot",
        user_id="alice",
        messages=[{"role": "user", "content": "hi"}],
        agent_learnings=True,
    )
    token = current_auth_var.set(_auth(read_user_id="alice", agent_principal="agent:signed-bot"))
    try:
        out = await handler.handle_memory_set_from_messages(body)
    finally:
        current_auth_var.reset(token)
    assert seen == [applied]
    assert out.ignored_args == ([] if setting else ["agent_learnings"])


def test_the_transport_keeps_an_ops_own_ignored_args() -> None:
    from server.transport.arg_check import attach

    out = attach({"ignored_args": ["agent_learnings"]}, {"ignored_args": ["typo"]})
    assert out["ignored_args"] == ["agent_learnings", "typo"]


@pytest.mark.asyncio
async def test_the_write_step_files_rules_and_never_writes_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filed: list[list[dict[str, Any]]] = []
    written: list[Any] = []

    async def fake_file(state: WorkflowState, facts: list[dict[str, Any]]) -> int:
        filed.append(facts)
        return len(facts)

    async def fake_bulk_set(bulk_input: Any) -> Any:
        written.append(bulk_input)

        class _Out:
            written: list[Any] = []

        return _Out()

    async def no_forget(state: WorkflowState, facts: list[dict[str, Any]]) -> Any:
        return facts, set(), set()

    async def keep_policy(state: WorkflowState) -> bool:
        return False

    monkeypatch.setattr(pipeline, "file_assistant_rules", fake_file)
    monkeypatch.setattr("server.memory.app.bulk_set.handle_memory_bulk_set", fake_bulk_set)
    monkeypatch.setattr(pipeline, "_forget_plan", no_forget)
    monkeypatch.setattr(pipeline, "_newest_wins", keep_policy)
    state = _state("alice", "s1")
    user_fact = {"key": "user_order_value", "value": "The user's order was $800.", "about": "user"}
    state.extracted_facts = [_rule_fact(), user_fact]

    await pipeline._step_bulk_write(state, {})

    assert [[f["key"] for f in batch] for batch in filed] == [["assistant_rule_refund_escalation"]]
    assert state.learning_candidates == 1
    (bulk_input,) = written
    assert [e.memory_key for e in bulk_input.entries] == ["user_order_value"]
    assert bulk_input.user_id == "alice"


@pytest.mark.asyncio
async def test_the_list_counts_a_rules_users_and_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime

    from server.memory.api.models_learnings import LearningsListInput
    from server.memory.app import learnings as h

    base = {
        "memory_type": "semantic",
        "proposed_value": json.dumps(_VALUE),
        "supporter_count": 0,
        "confidence_avg": 1.0,
        "supporters": [],
        "rationale": "r",
        "status": "pending",
        "proposed_at": datetime(2026, 1, 1),
        "decided_at": None,
        "decided_by": "",
        "decision_reason": "",
    }
    rows = [
        {
            **base,
            "candidate_id": _cid(),
            "memory_key": "assistant_rule_refund_escalation",
            "source_kind": "assistant_turn",
            "agent": "support-bot",
        },
        {
            **base,
            "candidate_id": "c-2",
            "memory_key": "policy",
            "source_kind": "memory_supporters",
            "agent": "",
            "supporter_count": 4,
        },
    ]
    asked: list[list[str]] = []

    async def fake_list(*_a: Any, **_kw: Any) -> list[dict[str, Any]]:
        return rows

    async def fake_stats(org: str, ns: str, ids: list[str]) -> list[dict[str, Any]]:
        asked.append(ids)
        pairs = [
            (datetime(2026, 1, 1), "alice", "s1"),
            (datetime(2026, 1, 2), "alice", "s2"),
            (datetime(2026, 1, 3), "bob", "s3"),
        ]
        return [
            {"candidate_id": _cid(), "users": 2, "sessions": 3, "confidence": 0.9, "pairs": pairs}
        ]

    monkeypatch.setattr(learnings_repo, "list_by_status", fake_list)
    monkeypatch.setattr(learnings_repo, "supporter_stats", fake_stats)
    out = await h.handle_learnings_list(LearningsListInput(org="o", namespace="ns"))

    assert asked == [[_cid()]]  # only the assistant_turn rows are counted
    rule, other = out.rows
    assert (rule.supporter_count, rule.supporter_users, rule.supporter_sessions) == (2, 2, 3)
    assert rule.confidence_avg == 0.9
    assert rule.supporters[2] == "o/ns/support-bot/bob/s3/assistant_rule_refund_escalation"
    assert rule.agent == "support-bot"
    assert (other.supporter_count, other.supporter_users, other.supporter_sessions) == (4, 0, 0)


@pytest.mark.asyncio
async def test_promoting_a_rule_writes_the_agents_memory_for_every_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import learnings as h
    from server.memory.domain.provenance import class_from_trace

    calls: list[Any] = []

    async def fake_set(set_input: Any) -> None:
        calls.append(set_input)

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", fake_set)
    row = {
        "candidate_id": _cid(),
        "agent": "support-bot",
        "memory_key": "assistant_rule_refund_escalation",
        "memory_type": "semantic",
        "proposed_value": json.dumps(_VALUE),
        "source_kind": "assistant_turn",
        "seeded_from_thread_id": "s1",
        "confidence_avg": 1.0,
    }
    await h._do_promote("o", "ns", row, "admin@example.com", "policy confirmed")

    (set_input,) = calls
    assert (set_input.agent, set_input.user_id, set_input.session_id) == ("support-bot", "-", "-")
    assert set_input.value == _VALUE
    assert set_input.source_trace == f"learning:{_cid()}"
    assert class_from_trace(set_input.source_trace) == "extracted"
    assert "source-kind:assistant_turn" in set_input.tags


def test_a_supporter_row_carries_every_column_of_loom_learnings_supporters() -> None:
    from tests.unit.schema_ddl import column_names

    assert column_names("learnings_supporters") == list(learnings_repo.SUPPORTER_COLUMNS)
