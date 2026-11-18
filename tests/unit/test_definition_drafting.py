"""One model attempt per claim; later decisions and erasure outrank its completion."""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.core.errors import ValidationError
from server.llm.client import LLMResult
from server.memory.app import definition_drafts as drafts
from tests.unit.definition_queue_fake import install


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    value: dict[str, Any] = {"row": None, "calls": 0, "cap": False, "valid": True, "deleted": []}
    value["fence"] = install(monkeypatch)
    lock = asyncio.Lock()

    @asynccontextmanager
    async def claim(*a: Any, **kw: Any) -> Any:
        async with lock:
            yield True

    async def find(*a: Any, **kw: Any) -> Any:
        return value["row"]

    async def insert(row: Any) -> None:
        value["row"] = dict(row)

    async def delete(org: str, namespace: str, cid: str, version: int) -> None:
        value["deleted"].append(version)
        if value["row"] and int(value["row"]["version"]) <= version:
            value["row"] = None

    async def cap(*a: Any, **kw: Any) -> bool:
        assert kw["fail_open"] is False
        return value["cap"]

    async def none(*a: Any, **kw: Any) -> None:
        pass

    class Model:
        async def complete(self, system: str, user: str, **kw: Any) -> LLMResult:
            assert json.loads(user) == {"sql": "SELECT sum(amount) FROM sales"}
            value["calls"] += 1
            if value.get("hold"):
                from server.llm.customer import ModelCallsDisabled

                raise ModelCallsDisabled("held")
            if value.get("entered"):
                value["entered"].set()
                await value["resume"].wait()
            return LLMResult(
                text=value.get("answer", '{"term":"Revenue","body":"Sum of sales."}'),
                tokens_in=10,
                tokens_out=10,
                provider="fake",
                model="fake",
            )

    from server.core import leases
    from server.llm import client, customer

    monkeypatch.setattr(leases, "holding", claim)
    monkeypatch.setattr(drafts.learnings, "find_candidate", find)
    monkeypatch.setattr(drafts.learnings, "insert_row", insert)
    monkeypatch.setattr(drafts.learnings, "delete_candidate_through", delete)
    monkeypatch.setattr(drafts, "namespace_over_cap", cap)
    monkeypatch.setattr(drafts, "require_draft_fleet", none)
    monkeypatch.setattr(customer, "require", none)
    monkeypatch.setattr(client, "get_llm_client", Model)
    return value


async def run(state: dict[str, Any]) -> str:
    async def valid() -> bool:
        return bool(state["valid"])

    return await drafts.draft(
        "o",
        "n",
        kind="query_history",
        resource_id="source/hash",
        evidence={"sql": "SELECT sum(amount) FROM sales"},
        supporters=3,
        still_valid=valid,
    )


async def test_repeat_keeps_exact_candidate_without_a_model_call(state: Any) -> None:
    cid = await run(state)
    original = dict(state["row"])
    assert await run(state) == cid and state["calls"] == 1 and state["row"] == original
    proposal = json.loads(original["proposed_value"])
    assert proposal["rests_on"]["sql"] == "SELECT sum(amount) FROM sales"
    assert original["supporters"] == [] and original["status"] == "pending"


async def test_parallel_repeat_answers_claim_while_model_runs(state: Any) -> None:
    state.update(entered=asyncio.Event(), resume=asyncio.Event())
    first = asyncio.create_task(run(state))
    try:
        await state["entered"].wait()
        cid = await run(state)
        assert state["calls"] == 1 and state["row"]["proposed_value"] == "null"
    finally:
        state["resume"].set()
    assert await first == cid and state["calls"] == 1


@pytest.mark.parametrize("change", ["reject", "redact", "erase"])
async def test_late_model_result_does_not_restore_erased_or_decided_content(
    state: Any, change: str
) -> None:
    state.update(entered=asyncio.Event(), resume=asyncio.Event())
    task = asyncio.create_task(run(state))
    try:
        await state["entered"].wait()
        if change == "reject":
            state["row"].update(status="rejected", version=10**19)
        elif change == "redact":
            state["row"].update(proposed_value="", version=10**19)
        else:
            state["row"] = None
            state["valid"] = False
    finally:
        state["resume"].set()
    await task
    if change == "reject":
        assert state["row"]["status"] == "rejected"
    elif change == "redact":
        assert state["row"]["proposed_value"] == ""
    else:
        assert state["row"] is None and state["deleted"]


async def test_cap_refuses_before_claim_or_model(state: Any) -> None:
    state["cap"] = True
    with pytest.raises(ValidationError, match="spend cap"):
        await run(state)
    assert state["row"] is None and state["calls"] == 0


async def test_invalid_model_answer_keeps_claim_and_cannot_spend_twice(state: Any) -> None:
    state["answer"] = "not JSON"
    with pytest.raises(ValueError):
        await run(state)
    assert state["row"]["proposed_value"] == "null"
    await run(state)
    assert state["calls"] == 1


async def test_policy_hold_releases_only_the_unspent_claim_for_retry(state: Any) -> None:
    from server.llm.customer import ModelCallsDisabled

    state["hold"] = True
    with pytest.raises(ModelCallsDisabled):
        await run(state)
    assert state["row"] is None
    state["hold"] = False
    await run(state)
    assert json.loads(state["row"]["proposed_value"])["term"] == "Revenue"


async def test_generation_fences_both_claim_and_completion_without_holding_during_model(
    state: Any,
) -> None:
    state.update(entered=asyncio.Event(), resume=asyncio.Event())
    task = asyncio.create_task(run(state))
    try:
        await asyncio.wait_for(state["entered"].wait(), 1)
        assert state["fence"].holders == {}
    finally:
        state["resume"].set()
    await task
    reads = state["fence"].reads
    assert len(reads) == 2 and reads[0] == reads[1]
    assert json.loads(state["row"]["proposed_value"])["term"] == "Revenue"


async def test_claim_retains_evidence_time_across_delayed_validation(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime, timedelta

    original = datetime(2026, 1, 1)
    monkeypatch.setattr(drafts, "naive_utc_now_seconds", lambda: original)

    async def valid() -> bool:
        monkeypatch.setattr(drafts, "naive_utc_now_seconds", lambda: original + timedelta(days=1))
        state["fence"].erasures = [{"scope_type": "namespace", "scope_value": "n"}]
        return True

    with pytest.raises(ValidationError, match="eras"):
        await drafts.draft(
            "o",
            "n",
            kind="query_history",
            resource_id="source/hash",
            evidence={},
            supporters=3,
            still_valid=valid,
        )
    assert state["row"] is None and state["calls"] == 0
    assert state["fence"].reads[-1][3] == int(original.replace(tzinfo=UTC).timestamp())


async def test_generation_cannot_restore_a_claim_read_before_completed_erasure(
    state: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    find = drafts.learnings.find_candidate
    reads = 0

    async def stale(*args: Any, **kw: Any) -> Any:
        nonlocal reads
        row = await find(*args, **kw)
        reads += 1
        if reads == 2:
            state["row"] = None
            state["fence"].erasures = [{"scope_type": "namespace", "scope_value": "n"}]
        return row

    monkeypatch.setattr(drafts.learnings, "find_candidate", stale)
    with pytest.raises(ValidationError, match="eras"):
        await run(state)
    assert state["row"] is None and state["calls"] == 1


@pytest.mark.parametrize("send", [1, 2])
async def test_uncertain_generation_send_keeps_namespace_eraser_out(
    state: Any, monkeypatch: pytest.MonkeyPatch, send: int
) -> None:
    from unittest.mock import AsyncMock

    from server.core.errors import BackendUnavailableError
    from server.erasure.store import steps_ledgers

    insert = drafts.learnings.insert_row
    sends = 0

    async def uncertain(row: Any) -> None:
        nonlocal sends
        sends += 1
        await insert(row)
        if sends == send:
            raise TimeoutError("send completion unknown")

    deleted = AsyncMock()
    monkeypatch.setattr(drafts.learnings, "insert_row", uncertain)
    monkeypatch.setattr(steps_ledgers, "_delete_many", deleted)
    with pytest.raises(TimeoutError, match="completion unknown"):
        await run(state)
    with pytest.raises(BackendUnavailableError):
        await steps_ledgers.delete_learnings_queue_by_namespace("o", "n")
    deleted.assert_not_awaited()
    assert state["fence"].holders and state["calls"] == send - 1


@pytest.mark.parametrize("repeat_question", [False, True])
async def test_gap_model_receives_questions_without_automatic_persisted_copy(
    state: Any, monkeypatch: pytest.MonkeyPatch, repeat_question: bool
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.knowledge.api.models_gaps import KnowledgeGapsDraftInput
    from server.knowledge.app import gap_drafts
    from server.llm import client

    question = "private-question-canary revenue by account"
    gap_id = "gap-resource"
    body = question if repeat_question else "Revenue is the sum of completed sales."
    completion = AsyncMock(
        return_value=LLMResult(
            text=json.dumps(
                {"term": "Revenue", "body": body, "tables": ["sales"], "columns": ["sales.amount"]}
            ),
            tokens_in=10,
            tokens_out=10,
            provider="fake",
            model="fake",
        )
    )
    monkeypatch.setattr(client, "get_llm_client", lambda: SimpleNamespace(complete=completion))
    monkeypatch.setattr(gap_drafts.settings, "knowledge_gaps_enabled", True)
    monkeypatch.setattr(gap_drafts.settings, "knowledge_gaps_min_askers", 3)
    monkeypatch.setattr(
        gap_drafts.capture_consent, "level_uncached", AsyncMock(return_value="full")
    )
    monkeypatch.setattr(gap_drafts.gaps, "erasure_since", AsyncMock(return_value=False))
    monkeypatch.setattr(
        gap_drafts.repo, "gap_for_draft", AsyncMock(return_value={"head": question, "samples": []})
    )
    monkeypatch.setattr(gap_drafts.repo, "draft_queries", AsyncMock(return_value=[question]))
    result = await gap_drafts.handle_gap_draft(
        KnowledgeGapsDraftInput(org="o", namespace="n", gap_id=gap_id)
    )
    assert json.loads(completion.await_args.args[1]) == {"queries": [question]}
    assert completion.await_count == 1
    assert state["row"]["candidate_id"] == result.candidate_id
    assert state["row"]["source_resource_id"] == gap_id
    proposal = json.loads(state["row"]["proposed_value"])
    assert proposal["term"] == "Revenue" and proposal["body"] == body
    assert proposal["rests_on"] == {
        "gap_id": gap_id,
        "tables": ["sales"],
        "columns": ["sales.amount"],
    }
    if not repeat_question:
        assert question not in state["row"]["proposed_value"]
