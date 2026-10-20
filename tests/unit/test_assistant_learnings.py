"""Rules an assistant states about itself: parsed as `about="agent"`, kept out
of the memory write, and filed as learnings queue candidates an admin promotes."""

from __future__ import annotations

import json
from datetime import datetime
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


_TICKET_MESSAGES = [
    {"role": "user", "content": "Ticket PR-1234 needs a refund."},
    {"role": "assistant", "content": "I escalate ticket PR-1234 refunds to a human."},
]


async def _extract(
    monkeypatch: pytest.MonkeyPatch, fact: dict[str, Any], messages: list[dict[str, Any]], on: bool
) -> list[Any]:
    llm = ScriptedLLM([json.dumps({"facts": [fact]})], repeat_last=True)
    monkeypatch.setattr(settings, "memory_extract_coverage_sweep_auto_min_uncovered", 0)
    monkeypatch.setattr("server.memory.app.extract.extract._extractor_client", lambda **_: llm)
    result = await extract_from_messages(
        messages, judge=False, coverage_sweep=False, agent_learnings=on
    )
    assert (AGENT_LEARNINGS_ANCHOR in llm.calls[0].system) is on
    return result.facts


@pytest.mark.asyncio
async def test_an_agent_fact_stays_one_only_when_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = _rule_fact(
        key="assistant_rule_ticket",
        value="The assistant escalates ticket PR-1234 refunds to a human.",
        evidence="I escalate ticket PR-1234 refunds to a human.",
    )
    (on,) = await _extract(monkeypatch, ticket, _TICKET_MESSAGES, True)
    (off,) = await _extract(monkeypatch, ticket, _TICKET_MESSAGES, False)
    # On, a rule keeps the extractor's key; an identifier in it re-keys a user fact.
    assert (on.about, on.memory_key) == ("agent", "assistant_rule_ticket")
    # Off, the model's mark is ignored and the fact gets every user-fact pass: the
    # identifier re-keys it and its evidence is graded against the turns.
    assert off.about == "user"
    assert off.memory_key.startswith("pr1234_")
    assert off.grounding
    assert not on.grounding


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
    def on(**state: Any) -> bool:
        return pipeline._agent_learnings_on(WorkflowState(**{"learning_agent": "bot", **state}))

    monkeypatch.setattr(settings, "memory_extract_agent_learnings", True)
    assert on() is True
    assert on(agent_learnings=False) is False
    # No agent to file for (a pinned token that names none): never.
    assert on(learning_agent="") is False
    monkeypatch.setattr(settings, "memory_extract_agent_learnings", False)
    assert on() is False
    assert on(agent_learnings=True) is True
    # A coding session's assistant narrates its work: never.
    assert on(agent_learnings=True, extra_tags=["source:claude-code"]) is False


_VALUE = "The assistant escalates any refund over $500 to a human."


def _cid(value: str = _VALUE, agent: str = "support-bot") -> str:
    return assistant_rules.candidate_id("o", "ns", agent, "assistant_rule_refund_escalation", value)


def test_the_candidate_id_is_one_per_agent_key_and_rule_text() -> None:
    assert _cid() == _cid("the assistant  escalates any refund over $500 to a human")
    assert _cid() != _cid(agent="other-bot")
    # Another rule under the same key is a sibling, never a supporter of the first.
    assert _cid() != _cid("The assistant refunds anything without asking.")


def test_the_rationale_quotes_the_user_turn_then_the_cited_evidence() -> None:
    assert assistant_rules.rationale(_rule_fact(evidence=_RULE), _MESSAGES) == (
        f'User: "Can you refund my $800 order?"\nAssistant: "{_RULE}"'
    )
    # No evidence span: the cited assistant turn.
    assert assistant_rules.rationale(_rule_fact(), _MESSAGES) == (
        'User: "Can you refund my $800 order?"\n'
        f'Assistant: "{_RULE} Someone will reply within a day."'
    )
    # Both quotes are bounded; no user turn, no user line.
    long = [{"role": "user", "content": "ask " * 200}, {"role": "assistant", "content": "w " * 400}]
    asked, said = assistant_rules.rationale(_rule_fact(), long).split("\n")
    assert len(asked) < 215 and len(said) < 420
    alone = [{"role": "assistant", "content": _RULE}]
    assert assistant_rules.rationale(_rule_fact(source_message_indices=[0]), alone) == (
        f'Assistant: "{_RULE}"'
    )


class _Queue:
    """learnings_repo over dicts: the candidate rows (newest version wins) and the
    supporter rows, one per (candidate, user, session)."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.inserts: list[dict[str, Any]] = []
        self.supporters: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.order: list[str] = []

    async def find(self, org: str, namespace: str, cid: str) -> dict[str, Any] | None:
        row = self.rows.get(cid)
        return dict(row) if row else None

    async def insert(self, row: dict[str, Any]) -> None:
        self.inserts.append(row)
        self.order.append("candidate")
        cid = str(row["candidate_id"])
        if cid not in self.rows or row["version"] >= self.rows[cid]["version"]:
            self.rows[cid] = dict(row)

    async def exists(self, org: str, ns: str, cid: str, user: str, session: str) -> bool:
        return (cid, user, session) in self.supporters

    async def insert_supporter(self, row: dict[str, Any]) -> None:
        self.order.append("supporter")
        self.supporters[(row["candidate_id"], row["user_id"], row["session_id"])] = dict(row)

    def support(self, cid: str) -> list[tuple[str, str]]:
        return sorted((u, s) for c, u, s in self.supporters if c == cid)


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _Queue:
    q = _Queue()
    monkeypatch.setattr(learnings_repo, "find_candidate", q.find)
    monkeypatch.setattr(learnings_repo, "insert_row", q.insert)
    monkeypatch.setattr(learnings_repo, "supporter_exists", q.exists)
    monkeypatch.setattr(learnings_repo, "insert_supporter", q.insert_supporter)
    return q


def _state(user_id: str, session_id: str, **extra: Any) -> WorkflowState:
    return WorkflowState(
        **{
            "org": "o",
            "namespace": "ns",
            "agent": "support-bot",
            "learning_agent": "support-bot",
            "user_id": user_id,
            "session_id": session_id,
            "messages": list(_MESSAGES),
            **extra,
        }
    )


async def _file(state: WorkflowState, fact: dict[str, Any] | None = None) -> int:
    return await assistant_learnings.file_assistant_rules(state, [fact or _rule_fact()])


@pytest.mark.asyncio
async def test_a_rule_is_filed_once_and_each_session_adds_one_supporter_row(queue: _Queue) -> None:
    assert await _file(_state("alice", "s1")) == 1
    # The filer's supporter row first: the earliest one names who filed it.
    assert queue.order == ["supporter", "candidate"]
    (row,) = queue.rows.values()
    assert row == {
        "candidate_id": _cid(),
        "org": "o",
        "namespace": "ns",
        "agent": "support-bot",
        "memory_key": "assistant_rule_refund_escalation",
        "memory_type": "semantic",
        "proposed_value": json.dumps(_VALUE),
        "supporter_count": 0,
        "confidence_avg": 1.0,
        "supporters": [],
        "rationale": (
            'User: "Can you refund my $800 order?"\n'
            f'Assistant: "{_RULE} Someone will reply within a day."'
        ),
        "source_kind": "assistant_turn",
        "seeded_from_thread_id": "s1",
        "seeded_from_msg_id": None,
        "status": "pending",
        "proposed_at": row["proposed_at"],
        "version": 1,
    }
    first = dict(queue.supporters[(_cid(), "alice", "s1")])
    await _file(_state("alice", "s1"))  # the same session again: its row is left as it is
    assert queue.supporters[(_cid(), "alice", "s1")] == first
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
async def test_a_rule_files_for_the_resolved_agent_and_nothing_without_one(queue: _Queue) -> None:
    await _file(_state("alice", "s1", learning_agent="signed-bot"))
    (row,) = queue.rows.values()
    assert row["agent"] == "signed-bot"
    assert row["candidate_id"] == _cid(agent="signed-bot")
    # '' (a pinned token naming no agent): nothing is filed, never the body's agent.
    assert await _file(_state("bob", "s2", learning_agent="")) == 0
    assert len(queue.rows) == 1 and queue.support(_cid()) == []


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


@pytest.mark.parametrize(
    ("auth", "agent", "pinned"),
    [
        # An end-user token its issuer signed for an agent.
        (dict(read_user_id="alice", agent_principal="agent:signed-bot"), "signed-bot", True),
        # An end-user token with no `agent`, and a token minted with `read_user_id`:
        # both pinned, neither names an agent, so nothing is filed.
        (dict(read_user_id="alice", read_scope="own_user"), "", True),
        (dict(read_user_id="alice"), "", True),
        # An unpinned token files for the body's agent.
        ({}, "body-bot", False),
    ],
)
def test_the_learning_agent_comes_from_the_token(
    auth: dict[str, Any], agent: str, pinned: bool
) -> None:
    from server.auth.context import current_auth_var

    token = current_auth_var.set(_auth(**auth))
    try:
        assert assistant_learnings.learning_agent("body-bot") == agent
        assert assistant_learnings.token_pinned() is pinned
    finally:
        current_auth_var.reset(token)


def test_a_replay_files_for_the_agent_its_request_resolved() -> None:
    with assistant_learnings.replay_learning_agent(""):
        assert assistant_learnings.learning_agent("body-bot") == ""
    with assistant_learnings.replay_learning_agent("signed-bot"):
        assert assistant_learnings.learning_agent("body-bot") == "signed-bot"
    # A payload queued before the agent was stored: the body's, as before.
    with assistant_learnings.replay_learning_agent(None):
        assert assistant_learnings.learning_agent("body-bot") == "body-bot"


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
    from types import SimpleNamespace

    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.transport.arg_check import attach, header_value

    out = attach({"ignored_args": ["agent_learnings"]}, {"ignored_args": ["typo"]})
    assert out["ignored_args"] == ["agent_learnings", "typo"]
    op = SimpleNamespace(input_model=MemorySetFromMessagesOutput)
    result = MemorySetFromMessagesOutput(ignored_args=["agent_learnings"])
    assert header_value(op, {"typo": 1}, result) == "agent_learnings,typo"
    assert header_value(op, {"typo": 1}, {"ignored_args": ["agent_learnings"]}) == (
        "agent_learnings,typo"
    )
    assert header_value(op, {}) == ""


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


_PAIRS = [
    (datetime(2026, 1, d), u, s)
    for d, u, s in ((1, "alice", "s1"), (2, "alice", "s2"), (3, "bob", "s3"))
]


@pytest.mark.asyncio
async def test_the_list_counts_a_rules_users_and_sessions(monkeypatch: pytest.MonkeyPatch) -> None:

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
        return [
            {"candidate_id": _cid(), "users": 2, "sessions": 3, "confidence": 0.9, "pairs": _PAIRS}
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
async def test_promoting_a_rule_writes_the_agents_memory_with_the_supporters_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings as h
    from server.memory.domain.provenance import class_from_trace

    row = {
        "candidate_id": _cid(),
        "agent": "support-bot",
        "memory_key": "assistant_rule_refund_escalation",
        "memory_type": "semantic",
        "proposed_value": json.dumps(_VALUE),
        "source_kind": "assistant_turn",
        "seeded_from_thread_id": "s1",
        "seeded_from_msg_id": None,
        "status": "pending",
        "supporters": [],
        "supporter_count": 0,
        "confidence_avg": 1.0,
        "rationale": "r",
        "proposed_at": None,
    }
    calls: list[Any] = []
    status_rows: list[dict[str, Any]] = []

    async def fake_find(*_a: Any) -> dict[str, Any]:
        return row

    async def fake_insert(r: dict[str, Any]) -> None:
        status_rows.append(r)

    async def fake_stats(*_a: Any) -> list[dict[str, Any]]:
        return [
            {"candidate_id": _cid(), "users": 2, "sessions": 3, "confidence": 0.9, "pairs": _PAIRS}
        ]

    async def fake_set(set_input: Any) -> None:
        calls.append(set_input)

    monkeypatch.setattr(learnings_repo, "find_candidate", fake_find)
    monkeypatch.setattr(learnings_repo, "insert_row", fake_insert)
    monkeypatch.setattr(learnings_repo, "supporter_stats", fake_stats)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", fake_set)
    await h.handle_learnings_review(
        LearningsReviewInput(
            org="o", namespace="ns", candidate_id=_cid(), decision="promote", approver="admin"
        )
    )

    (set_input,) = calls
    assert (set_input.agent, set_input.user_id, set_input.session_id) == ("support-bot", "-", "-")
    assert set_input.value == _VALUE
    assert set_input.confidence == 0.9  # what the reviewer saw
    assert set_input.source_trace == f"learning:{_cid()}"
    assert class_from_trace(set_input.source_trace) == "extracted"
    assert "source-kind:assistant_turn" in set_input.tags
    # The status row keeps the candidate's own columns: no supporter names on it.
    (status,) = status_rows
    assert status["status"] == "promoted" and status["supporters"] == []
    assert status["agent"] == "support-bot"


def test_a_supporter_row_carries_every_column_of_loom_learnings_supporters() -> None:
    from tests.unit.schema_ddl import column_names

    assert column_names("learnings_supporters") == list(learnings_repo.SUPPORTER_COLUMNS)
