"""A refusal names the next step, a triaged post says why,
an MCP task list is lean and newest first, a locked delete names its unlock
call. Loom still decides nothing: every hint names who moves the task."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.domain import lock as memory_lock
from server.tasks.api import models_task as wire
from server.tasks.app import common, inboxes, reads, transitions
from server.transport import mcp_call
from tests.unit.test_task_inbox import FakeRepo, _caller, _post, _register, patch_handlers


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fr = FakeRepo()
    patch_handlers(monkeypatch, "repo", fr)
    monkeypatch.setattr("server.tasks.domain.cursors.LAG_SECONDS", -1)
    return fr


# ------------------------------------------------------------ task.create


@pytest.mark.asyncio
async def test_a_triaged_post_says_why_and_who_moves_it(fake: FakeRepo) -> None:
    await _register(fake)
    out = await _post(fake)
    assert out.task.status == "triage"
    assert out.next.startswith("status=triage: the inbox trusts only its owner agent 'private-bot'")
    assert "this post came from agent 'public-bot'" in out.next
    assert "task.accept" in out.next and "inbox.policy_set" in out.next
    # the namespace owner's own agent lands in todo, with no next step
    own = await _post(fake, **_caller("private", "private-bot"), from_subject="")
    assert own.task.status == "todo" and own.next == ""


@pytest.mark.asyncio
async def test_a_policy_triage_names_the_policy(fake: FakeRepo) -> None:
    await _register(fake)
    await inboxes.handle_inbox_policy_set(
        wire.InboxPolicySetInput(
            **_caller("private", "admin"),
            inbox="private-bot",
            poster="sa:public-bot",
            kind="*",
            mode="review",
        )
    )
    out = await _post(fake)
    assert out.task.status == "triage"
    assert "the inbox's policy for this poster/kind is 'review'" in out.next


# ------------------------------------------------------------ refusals


@pytest.mark.asyncio
async def test_self_accept_and_claim_of_a_triaged_task_name_the_owner(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(fake)
    t = (await _post(fake)).task
    patch_handlers(monkeypatch, "_caller_subject", lambda fallback="": fallback or "sa:public-bot")
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_accept(
            wire.TaskAcceptInput(**_caller("private", "x"), task_id=t.task_id)
        )
    d = e.value.detail
    assert d["code"] == "TASK_SELF_ACCEPT" and d["state"] == "triage"
    assert "agent 'private-bot'" in d["next"] and "on 'private'" in d["next"]
    assert "task.accept" in d["message"]
    for handler, inp in (
        (
            transitions.handle_task_claim,
            wire.TaskClaimInput(**_caller("private", "b"), task_id=t.task_id),
        ),
        (
            transitions.handle_task_defer,
            wire.TaskDeferInput(
                **_caller("private", "b"), task_id=t.task_id, fence=0, reason="later"
            ),
        ),
    ):
        with pytest.raises(HTTPException) as e:
            await handler(inp)
        d = e.value.detail
        assert d["code"] in ("TASK_NOT_CLAIMABLE", "TASK_NOT_DEFERRABLE")
        assert d["state"] == "triage" and "task.accept" in d["next"]
        assert d["message"].startswith("task is 'triage'; only a")


def test_every_state_names_a_next_op() -> None:
    for status, op in (
        ("todo", "task.pull"),
        ("in_progress", "task.complete"),
        ("blocked", "task.unblock"),
        ("deferred", "task.resume"),
        ("failed", "task.reopen"),
        ("wont_do", "task.reopen"),
        ("cancelled", "task.reopen"),
        ("triage", "task.accept"),
    ):
        assert op in common._state_next({"status": status, "owner": "bot", "fence": 3}), status
    assert "fence 3" in common._state_next({"status": "in_progress", "owner": "bot", "fence": 3})
    assert common._state_next({"status": "done"}) == "it is finished"


@pytest.mark.asyncio
async def test_a_refusal_survives_a_missing_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        async def fetch_inbox_row(self, org: str, inbox: str) -> Any:
            raise RuntimeError("ch down")

    patch_handlers(monkeypatch, "repo", Boom())
    exc = await common._state_conflict(
        "TASK_NOT_CLAIMABLE", {"status": "triage", "org": "o", "inbox": "i"}
    )
    assert exc.status_code == 409 and "the inbox's owner agent" in exc.detail["next"]


# ------------------------------------------------------------ task.pull


@pytest.mark.asyncio
async def test_an_empty_pull_says_how_many_wait_in_triage(fake: FakeRepo) -> None:
    await _register(fake)
    await _post(fake)
    out = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot"), inbox="private-bot")
    )
    assert out.tasks == []
    assert out.note.startswith("no todo task; 1 wait in triage")
    assert "status=triage" in out.note
    # a pull that took something, or found nothing waiting, adds no note
    await _post(fake, **_caller("private", "private-bot"), from_subject="")
    took = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot"), inbox="private-bot")
    )
    assert len(took.tasks) == 1 and took.note == ""


# ------------------------------------------------------------ task.list


@pytest.mark.asyncio
async def test_newest_first_lists_the_task_just_posted_before_a_limit(fake: FakeRepo) -> None:
    await _register(fake)
    base = datetime(2026, 9, 25, 8, 0, tzinfo=UTC).replace(tzinfo=None)
    for i in range(5):
        t = (await _post(fake, title=f"old {i}")).task
        fake.tasks[t.task_id]["created_at"] = base + timedelta(minutes=i)
    newest = (await _post(fake, title="just posted", priority=4)).task
    fake.tasks[newest.task_id]["created_at"] = base + timedelta(hours=1)
    queue = await reads.handle_task_list(
        wire.TaskListInput(**_caller("private", "bot"), inbox="private-bot", limit=3)
    )
    assert "just posted" not in [r["title"] for r in queue.tasks]  # the queue order hides it
    fresh = await reads.handle_task_list(
        wire.TaskListInput(
            **_caller("private", "bot"), inbox="private-bot", limit=3, newest_first=True
        )
    )
    assert fresh.tasks[0]["title"] == "just posted"


def test_mcp_task_list_defaults_to_the_lean_row_newest_first() -> None:
    args: dict[str, Any] = {"inbox": "loomcheck"}
    mcp_call._scope_arguments("task.list", args, None, False)
    assert args["fields"] == list(mcp_call.MCP_TASK_LIST_FIELDS) and args["newest_first"] is True
    assert "brief" not in mcp_call.MCP_TASK_LIST_FIELDS
    explicit: dict[str, Any] = {"inbox": "x", "fields": ["title"], "newest_first": False}
    mcp_call._scope_arguments("task.list", explicit, None, False)
    assert explicit["fields"] == ["title"] and explicit["newest_first"] is False
    other: dict[str, Any] = {"inbox": "x"}
    mcp_call._scope_arguments("task.get", other, None, False)
    assert "fields" not in other
    # every lean field is a real column (a typo would silently drop it)
    from server.tasks.store import repo as tasks_repo

    assert set(mcp_call.MCP_TASK_LIST_FIELDS) <= set(tasks_repo.TASK_FIELDS)


# ------------------------------------------------------------ LOCKED


def test_locked_refusal_says_who_unlocks_and_to_leave_the_key() -> None:
    row = SimpleNamespace(
        memory_key="test/loomcheck/pref",
        memory_id="m-1",
        namespace="jdoe",
        lock_mode="read_only",
        lock_reason="smoke test",
        locked_by="process:sa:x",
        lock_until=None,
    )
    exc = memory_lock.refusal(row, "delete", op="memory.delete", mode="read_only")
    d = exc.detail
    assert exc.status_code == 423 and d["code"] == "LOCKED"
    msg = d["message"]
    assert "locked read_only (smoke test) by process:sa:x" in msg
    assert "Only the user can unlock it:" in msg
    assert "leave the key as it is and tell them it is locked" in msg
    # never a route an agent could take around the owner
    assert "SDK" not in msg and "HTTP" not in msg
    # never an instruction to call an op MCP hides
    assert "calls memory.unlock" not in msg and "unlock_args" not in d
    from server.operations.toolsets import _MCP_HIDDEN_OPS

    assert "memory.unlock" in _MCP_HIDDEN_OPS


# ------------------------------------------------------------ doctor


def test_doctor_warns_when_recording_is_off() -> None:
    from server.cli import doctor_client as dc

    def post(off: bool | None, status: int = 200):
        def _p(base_url: str, op: str, body: dict[str, Any], token: str, timeout: float):
            assert op == "namespace.get" and body == {"org": "acme", "namespace": "coding"}
            out: dict[str, Any] = {"found": off is not None, "namespace": "coding"}
            if off is not None:
                out["conversation_logging_enabled"] = not off
            return status, out

        return _p

    claims = {"org": "acme"}
    name, grade, why = dc.recording_check("u", "t", claims, "coding", post(True), True)
    assert (name, grade) == ("client:recording", "WARN") and "recording is off for 'coding'" in why
    assert dc.recording_check("u", "t", claims, "coding", post(False), True)[1] == "PASS"
    assert dc.recording_check("u", "t", claims, "coding", post(None), True)[1] == "WARN"
    assert dc.recording_check("u", "", claims, "coding", post(False), True)[2].startswith("skipped")
