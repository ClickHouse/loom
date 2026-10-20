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


def test_the_candidate_id_is_one_per_agent_and_key() -> None:
    a = assistant_rules.candidate_id("o", "ns", "bot", "assistant_rule_x")
    assert a == assistant_rules.candidate_id("o", "ns", "bot", "assistant_rule_x")
    assert a != assistant_rules.candidate_id("o", "ns", "other-bot", "assistant_rule_x")
    assert a != assistant_rules.candidate_id("o", "ns2", "bot", "assistant_rule_x")


def test_the_rationale_quotes_the_assistant_turn() -> None:
    assert assistant_rules.rationale(_rule_fact(), _MESSAGES) == (
        f'Stated in an assistant turn: "{_RULE} Someone will reply within a day."'
    )
    # A fact citing only the user's turn falls back to its evidence span.
    cited_user = _rule_fact(source_message_indices=[0], evidence=_RULE)
    assert assistant_rules.rationale(cited_user, _MESSAGES) == (
        f'Stated in an assistant turn: "{_RULE}"'
    )
    long = [{"role": "assistant", "content": "word " * 200}]
    assert len(assistant_rules.rationale(_rule_fact(source_message_indices=[0]), long)) < 440


class _Queue:
    """learnings_repo's find_candidate / insert_row over a dict, newest version wins."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.inserts: list[dict[str, Any]] = []

    async def find(self, org: str, namespace: str, cid: str) -> dict[str, Any] | None:
        row = self.rows.get(cid)
        return dict(row) if row else None

    async def insert(self, row: dict[str, Any]) -> None:
        self.inserts.append(row)
        self.rows[str(row["candidate_id"])] = dict(row)


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _Queue:
    q = _Queue()
    monkeypatch.setattr(learnings_repo, "find_candidate", q.find)
    monkeypatch.setattr(learnings_repo, "insert_row", q.insert)
    return q


def _state(user_id: str, session_id: str) -> WorkflowState:
    return WorkflowState(
        org="o",
        namespace="ns",
        agent="support-bot",
        user_id=user_id,
        session_id=session_id,
        messages=list(_MESSAGES),
    )


@pytest.mark.asyncio
async def test_a_rule_is_filed_once_and_each_new_session_adds_a_supporter(queue: _Queue) -> None:
    assert (
        await assistant_learnings.file_assistant_rules(_state("alice", "s1"), [_rule_fact()]) == 1
    )
    (row,) = queue.rows.values()
    assert row == {
        "candidate_id": assistant_rules.candidate_id(
            "o", "ns", "support-bot", "assistant_rule_refund_escalation"
        ),
        "org": "o",
        "namespace": "ns",
        "agent": "support-bot",
        "memory_key": "assistant_rule_refund_escalation",
        "memory_type": "semantic",
        "proposed_value": json.dumps("The assistant escalates any refund over $500 to a human."),
        "supporter_count": 1,
        "confidence_avg": 1.0,
        "supporters": ["o/ns/support-bot/alice/s1/assistant_rule_refund_escalation"],
        "supporter_users": ["alice"],
        "rationale": f'Stated in an assistant turn: "{_RULE} Someone will reply within a day."',
        "source_kind": "assistant_turn",
        "seeded_from_thread_id": "s1",
        "seeded_from_msg_id": None,
        "status": "pending",
        "proposed_at": row["proposed_at"],
        "version": row["version"],
    }

    # The same session restating it changes nothing.
    assert (
        await assistant_learnings.file_assistant_rules(_state("alice", "s1"), [_rule_fact()]) == 0
    )
    # Another session of the same user, then another user: two more supporters.
    await assistant_learnings.file_assistant_rules(_state("alice", "s2"), [_rule_fact()])
    uncertain = _rule_fact(judge_verdict="UNCERTAIN")
    await assistant_learnings.file_assistant_rules(_state("bob", "s3"), [uncertain])
    (row,) = queue.rows.values()
    assert row["status"] == "pending"
    assert row["supporter_count"] == 3
    assert row["supporter_users"] == ["alice", "bob"]
    assert [s.split("/")[3:5] for s in row["supporters"]] == [
        ["alice", "s1"],
        ["alice", "s2"],
        ["bob", "s3"],
    ]
    assert row["confidence_avg"] == pytest.approx((1.0 + 1.0 + 0.75) / 3)
    assert row["rationale"].startswith("Stated in an assistant turn:")
    assert len(queue.inserts) == 3


@pytest.mark.asyncio
async def test_a_decided_candidate_takes_no_more_supporters(queue: _Queue) -> None:
    await assistant_learnings.file_assistant_rules(_state("alice", "s1"), [_rule_fact()])
    (cid,) = queue.rows
    queue.rows[cid]["status"] = "rejected"
    assert await assistant_learnings.file_assistant_rules(_state("bob", "s2"), [_rule_fact()]) == 0
    assert queue.rows[cid]["supporter_count"] == 1


@pytest.mark.asyncio
async def test_a_failed_queue_write_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(*_a: Any, **_kw: Any) -> None:
        raise RuntimeError("Code: 210. NETWORK_ERROR")

    monkeypatch.setattr(learnings_repo, "find_candidate", broken)
    state = _state("alice", "s1")
    assert await assistant_learnings.file_assistant_rules(state, [_rule_fact()]) == 0
    assert state.errors == ["learnings_queue_failed:assistant_rule_refund_escalation"]


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

    monkeypatch.setattr(assistant_learnings, "file_assistant_rules", fake_file)
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
async def test_promoting_a_rule_writes_the_agents_memory_for_every_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import learnings as h

    calls: list[Any] = []

    async def fake_set(set_input: Any) -> None:
        calls.append(set_input)

    monkeypatch.setattr("server.memory.app.write.handle_memory_set", fake_set)
    row = {
        "candidate_id": "6f1c1d7e-0000-5000-8000-000000000001",
        "agent": "support-bot",
        "memory_key": "assistant_rule_refund_escalation",
        "memory_type": "semantic",
        "proposed_value": json.dumps("The assistant escalates any refund over $500 to a human."),
        "source_kind": "assistant_turn",
        "seeded_from_thread_id": "s1",
        "confidence_avg": 1.0,
    }
    await h._do_promote("o", "ns", row, "admin@example.com", "policy confirmed")

    (set_input,) = calls
    assert (set_input.agent, set_input.user_id, set_input.session_id) == ("support-bot", "-", "-")
    assert set_input.value == "The assistant escalates any refund over $500 to a human."
    assert set_input.source_trace == "conversation:s1"
    assert "source-kind:assistant_turn" in set_input.tags
