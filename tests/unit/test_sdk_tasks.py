"""The SDK's task inbox client: every method posts the
registry op with the agent's own identity and drops unset fields; `stream`
is the daemon loop over `task.pull`; `get` unwraps `found`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from loom import Agent
from loom.tasks import MAX_WAIT_SECONDS


class _FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float | None]] = []
        self.pull_batches: list[list[dict[str, Any]]] = []

    async def post(self, op_path, payload, idempotency_key=None, *, timeout_s=None):
        self.calls.append((op_path, dict(payload), timeout_s))
        if op_path == "/v1/task.pull":
            return {
                "tasks": self.pull_batches.pop(0) if self.pull_batches else [],
                "waited_ms": 1,
                "backend": "keeper",
            }
        if op_path == "/v1/task.get":
            return {"found": payload["task_id"] == "known", "task": {"task_id": "known"}}
        return {"ok": True, "op": op_path}


@pytest.fixture
def agent() -> Agent:
    a = Agent(org="acme", namespace="private", agent="private-bot", api_key="x")
    a.transport = _FakeTransport()  # type: ignore[assignment]
    return a


@pytest.mark.asyncio
async def test_create_carries_identity_and_drops_unset_fields(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.tasks.create(
        "private-bot",
        kind="security_finding",
        title="UB",
        brief={"inputs": {"finding_id": "f-1"}},
        disclosure="confidential",
        not_before=datetime(2030, 1, 1, tzinfo=UTC),
        idempotency_key="f-1",
    )
    path, body, _ = t.calls[-1]
    assert path == "/v1/task.create"
    assert (
        body["org"] == "acme" and body["namespace"] == "private" and body["agent"] == "private-bot"
    )
    assert body["inbox"] == "private-bot" and body["not_before"].startswith("2030-01-01")
    assert "deadline" not in body and "parent_task_id" not in body  # unset → omitted
    await agent.tasks.create("private-bot", kind="k", title="t", agent_id="other-agent")
    assert t.calls[-1][1]["agent"] == "other-agent"


@pytest.mark.asyncio
async def test_pull_caps_the_wait_and_gives_the_transport_room(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.tasks.pull("private-bot", kinds=["a"], wait_seconds=999, limit=3)
    path, body, timeout = t.calls[-1]
    assert path == "/v1/task.pull" and body["wait_seconds"] == MAX_WAIT_SECONDS
    assert body["kinds"] == ["a"] and body["limit"] == 3 and body["status"] == "todo"
    assert timeout is not None and timeout > MAX_WAIT_SECONDS
    await agent.tasks.pull("private-bot", status="triage")
    assert t.calls[-1][1]["status"] == "triage"


@pytest.mark.asyncio
async def test_stream_yields_claimed_tasks_across_pulls(agent: Agent) -> None:
    t: Any = agent.transport
    t.pull_batches = [[], [{"task_id": "1", "fence": 1}], [{"task_id": "2", "fence": 1}]]
    seen = []
    async for task in agent.tasks.stream("private-bot", kinds=["k"]):
        seen.append(task["task_id"])
        if len(seen) == 2:
            break
    assert seen == ["1", "2"]
    assert all(c[0] == "/v1/task.pull" for c in t.calls)


@pytest.mark.asyncio
async def test_owner_writes_carry_the_fence_and_reasons(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.tasks.heartbeat("t", fence=2)
    await agent.tasks.progress("t", fence=2, note="stage 1")
    await agent.tasks.complete("t", fence=2, outcome={"issue_url": "u"})
    await agent.tasks.fail("t", fence=2, reason="boom")
    await agent.tasks.defer("t", fence=2, reason="no binary", resume_at="2030-01-01T00:00:00Z")
    await agent.tasks.block("t", fence=2, reason="need repro", blocked_on="sender")
    await agent.tasks.wont_do("t", reason="dup")
    await agent.tasks.cancel("t", reason="withdrawn")
    await agent.tasks.reopen("t", reason="it is a bug")
    await agent.tasks.resume("t", note="binary landed")
    ops = [c[0].removeprefix("/v1/") for c in t.calls]
    assert ops == [
        "task.heartbeat",
        "task.progress",
        "task.complete",
        "task.fail",
        "task.defer",
        "task.block",
        "task.wont_do",
        "task.cancel",
        "task.reopen",
        "task.resume",
    ]
    bodies = [c[1] for c in t.calls]
    assert all(b["fence"] == 2 for b in bodies[:6])
    assert bodies[4]["resume_at"] == "2030-01-01T00:00:00Z" and bodies[5]["blocked_on"] == "sender"
    assert "fence" not in bodies[6] and bodies[6]["reason"] == "dup"


@pytest.mark.asyncio
async def test_reads_and_cursor(agent: Agent) -> None:
    t: Any = agent.transport
    assert await agent.tasks.get("known") == {"task_id": "known"}
    assert await agent.tasks.get("nope") is None
    await agent.tasks.list(inbox="private-bot", statuses=["todo"], fields=["title"])
    assert t.calls[-1][1]["fields"] == ["title"] and t.calls[-1][1]["statuses"] == ["todo"]
    await agent.tasks.updates(since_seq=42, wait_seconds=5)
    path, body, timeout = t.calls[-1]
    assert path == "/v1/task.updates" and body["since_seq"] == 42 and body["wait_seconds"] == 5
    assert "inbox" not in body and timeout is not None
    await agent.tasks.counts()
    await agent.tasks.history("t", limit=5)
    assert [c[0] for c in t.calls[-2:]] == ["/v1/task.counts", "/v1/task.history"]


@pytest.mark.asyncio
async def test_inbox_admin_calls(agent: Agent) -> None:
    t: Any = agent.transport
    await agent.tasks.register_inbox("private-bot", posters=["sa:public-bot"])
    body = t.calls[-1][1]
    assert body["owner_agent"] == "private-bot" and body["posters"] == ["sa:public-bot"]
    assert "accept_disclosure" not in body and "if_match_version" not in body
    await agent.tasks.set_policy(
        "private-bot",
        poster="sa:public-bot",
        kind="security_finding",
        mode="review",
        require_evidence=["inputs.finding_id"],
    )
    body = t.calls[-1][1]
    assert body["mode"] == "review" and body["remove"] is False
    assert body["require_evidence"] == ["inputs.finding_id"] and "promote_after" not in body
    await agent.tasks.list_policies("private-bot")
    await agent.tasks.list_inboxes(mine_everywhere=True)
    assert [c[0] for c in t.calls[-2:]] == ["/v1/inbox.policy_list", "/v1/inbox.list"]
    await agent.tasks.principals("private-bot", query="bot")
    assert t.calls[-1][0] == "/v1/inbox.principals" and t.calls[-1][1]["query"] == "bot"


def test_sync_proxy_exposes_tasks() -> None:
    from loom.sync import _CLIENT_ATTRS

    assert "tasks" in _CLIENT_ATTRS
