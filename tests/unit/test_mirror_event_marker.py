"""The ledger captures the logical mark before a writer consumes the row."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from server.auth import tenancy
from server.core import request_tail, request_work
from server.core.diagnostics import private_work
from server.identity.app import policy_witness
from server.identity.domain.policy_rules import PolicyRule
from server.observability import cost_accumulator, events, ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("private", [False, True])
async def test_logical_mark_reaches_normal_and_waited_event_rows(
    monkeypatch, marked, held, private
):
    rows = []
    queued = []
    rules = [
        PolicyRule.model_validate(
            {
                "policy_id": "prior-call",
                "effect": "require",
                "actions": ["memory.set"],
                "condition": {
                    "op": "formerly",
                    "operation": ["knowledge.get"],
                    "within": "1h",
                },
            }
        )
    ]

    async def load_rules(org, namespace):
        assert (org, namespace) == ("org-a", "team")
        return rules

    async def write(table, row):
        assert table == "loom.agent_events"
        rows.append(row)

    async def insert(table, data, *, settings, required_columns):
        assert table == "loom.agent_events"
        assert required_columns == frozenset({"mirror_marked"})
        assert settings["async_insert"] == settings["wait_for_async_insert"] == 1
        rows.extend(data)

    def queue(event):
        queued.append(event)
        rows.append(event.to_row())

    monkeypatch.setattr(tenancy, "attributable", lambda org: True)
    monkeypatch.setattr(events, "get_client", lambda: SimpleNamespace(insert=insert))
    monkeypatch.setattr(events, "stamp_row", lambda row: row)
    monkeypatch.setattr(events, "for_this_cell", lambda event: event)
    monkeypatch.setattr(policy_witness, "_policies_on", lambda: True)
    monkeypatch.setattr(policy_witness, "_rules", load_rules)
    monkeypatch.setattr(policy_witness, "_write", write)
    monkeypatch.setattr(ledger, "get_emitter", lambda: SimpleNamespace(emit=queue))
    monkeypatch.setattr(ledger, "_event_hold", policy_witness.hold_event if held else None)
    monkeypatch.setattr(
        cost_accumulator,
        "current",
        lambda: SimpleNamespace(
            cost_usd=0.125,
            embed_tokens=13,
            llm_input_tokens=17,
            llm_output_tokens=19,
            llm_calls=2,
            dominant_model=lambda: "test-model",
        ),
    )
    local_token = request_work._current.set(None)
    tail = request_tail._Tail()
    tail_token = request_tail._tail.set(tail)
    try:
        request_work.begin()
        if marked:
            assert request_work.mark_mirrored()
        with private_work() if private else nullcontext():
            ledger.emit_agent_event(
                op_name="knowledge.get",
                identity={
                    "org": "org-a",
                    "namespace": "team",
                    "agent": "reader",
                    "user_id": "user-a",
                    "session_id": "session-a",
                },
                request_id="request-a",
                latency_ms=23,
                success=True,
                error_code="",
                # A caller cannot classify its own row, in either direction.
                raw_args={"mirror_marked": not marked},
                subject="subject-a",
                consumer="test-client",
                traffic_class="interactive",
            )
            if held or marked:
                assert rows == []
            request_work.begin()
            assert not request_work.mirror_marked()
            await request_tail.settle(tail)
        assert len(rows) == 1
        assert len(queued) == (0 if held or marked else 1)
        row = rows[0]
        assert row.get("mirror_marked", 0) == int(marked)
        assert type(row.get("mirror_marked", 0)) is int
        assert row["org"] == "org-a"
        assert row["namespace"] == "team"
        assert row["agent"] == "reader"
        assert row["user_id"] == "user-a"
        assert row["session_id"] == "session-a"
        assert row["subject"] == "subject-a"
        assert row["request_id"] == "request-a"
        assert row["operation"] == "knowledge.get"
        assert row["latency_ms"] == 23
        assert row["success"] is True
        assert row["upstream_cost_usd"] == 0.125
        assert row["embedding_tokens"] == 13
        assert row["input_tokens"] == 17
        assert row["output_tokens"] == 19
        assert row["model"] == "test-model"
        assert row["llm_calls"] == 2
        assert "mirror_marked" not in row["args"]
    finally:
        request_tail._tail.reset(tail_token)
        request_work._current.reset(local_token)
