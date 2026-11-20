"""The two-principal handoff verifies provenance and cleans up after failures."""

from __future__ import annotations

import functools
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from loom import ValidationError

EXAMPLE = Path(__file__).resolve().parents[2] / "sdk/python/examples/multi_agent_handoff.py"


def load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("multi_agent_handoff_example", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HandoffServer:
    def __init__(self, fail_op: str = "", bad_field: str = "") -> None:
        self.fail_op = fail_op
        self.bad_field = bad_field
        self.actor = "process:handoff-worker"
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.task: dict[str, Any] | None = None
        self.memory: dict[str, Any] | None = None
        self.inbox = ""
        self.key = ""
        self.deleted = False
        self.canonical_value: Any = None
        self.force_noop = False

    def __call__(self, req: httpx.Request) -> httpx.Response:
        op = req.url.path.removeprefix("/v1/")
        token = req.headers["authorization"]
        body = json.loads(req.content)
        self.calls.append((op, token, body))
        assert body["org"] == "example-org" and body["namespace"] == "existing-space"
        assert body["agent"] == (
            "handoff-planner" if token == "Bearer planner-token" else "handoff-worker"
        )
        if op == self.fail_op:
            return httpx.Response(422, json={"detail": {"code": "VALIDATION_ERROR"}})
        if op == "inbox.register":
            assert token == "Bearer planner-token" and body["owner_agent"] == "handoff-worker"
            self.inbox = body["inbox"]
            return httpx.Response(200, json={"inbox": {"inbox": self.inbox}})
        if op == "task.create":
            assert token == "Bearer planner-token" and body["inbox"] == self.inbox
            assert body["idempotency_key"] == self.inbox
            self.task = {
                "task_id": "task-1",
                "status": "todo",
                "owner": "",
                "fence": 0,
                "brief": body["brief"],
                "outcome": {},
                "from_subject": "handoff-planner",
            }
            if self.bad_field == "from_subject":
                self.task["from_subject"] = "handoff-worker"
            return httpx.Response(200, json={"task": self.task, "created": True})
        if op in {"task.claim", "task.heartbeat", "task.complete", "task.cancel", "task.get"}:
            assert self.task is not None and body["task_id"] == "task-1"
            if op == "task.claim":
                assert token == "Bearer worker-token"
                self.task.update(status="in_progress", owner="handoff-worker", fence=17)
            elif op in {"task.heartbeat", "task.complete"}:
                assert token == "Bearer worker-token" and body["fence"] == 17
                if op == "task.complete":
                    assert body["outcome"] == {"memory_key": self.key}
                    self.task.update(status="done", outcome=body["outcome"])
            elif op == "task.cancel":
                if self.task["owner"]:
                    assert token == "Bearer worker-token" and body["fence"] == 17
                else:
                    assert token == "Bearer planner-token"
                self.task["status"] = "cancelled"
            else:
                assert token == "Bearer planner-token"
            return httpx.Response(200, json={"found": True, "task": self.task})
        if op == "memory.set":
            assert token == "Bearer worker-token"
            self.key = body["memory_key"]
            if self.force_noop or body["value"] == self.canonical_value:
                return httpx.Response(
                    200,
                    json={
                        "noop": True,
                        "conflict_verdict": "merge",
                        "merged_into_key": "existing-result",
                    },
                )
            self.memory = {
                "found": True,
                "value": body["value"],
                "agent": "handoff-worker",
                "actor": self.actor,
                "source_class": "asserted",
            }
            if self.bad_field:
                self.memory[self.bad_field] = "wrong"
            return httpx.Response(200, json={"memory_key": self.key})
        if op == "memory.get":
            return httpx.Response(200, json=self.memory or {"found": False})
        if op == "memory.delete":
            assert token == "Bearer worker-token"
            if self.key:
                assert body["memory_key"] == self.key
            self.memory = None
            self.deleted = True
            return httpx.Response(200, json={"tombstoned": True})
        raise AssertionError(f"Unexpected operation: {op}")


def configure(monkeypatch: pytest.MonkeyPatch, server: HandoffServer) -> None:
    monkeypatch.setenv("LOOM_ORG", "example-org")
    monkeypatch.setenv("LOOM_NAMESPACE", "existing-space")
    monkeypatch.setenv("LOOM_BASE_URL", "http://example.invalid")
    monkeypatch.setenv("LOOM_PLANNER_API_KEY", "planner-token")
    monkeypatch.setenv("LOOM_WORKER_API_KEY", "worker-token")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(server)),
    )


async def test_handoff_uses_two_tokens_and_cleans_its_memory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = HandoffServer()
    configure(monkeypatch, server)
    await load_example().main()
    assert server.task is not None and server.task["status"] == "done"
    assert server.deleted and server.memory is None
    assert not any(op == "task.cancel" for op, _, _ in server.calls)
    output = capsys.readouterr().out
    assert "result: 7 squared is 49" in output
    assert "writer actor: process:handoff-worker" in output and "source class: asserted" in output
    assert "planner-token" not in output and "worker-token" not in output


async def test_handoff_keeps_an_existing_identical_calculation_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = HandoffServer()
    server.canonical_value = "7 squared is 49"
    configure(monkeypatch, server)
    await load_example().main()
    assert server.task is not None and server.task["status"] == "done"
    written = next(body for op, _, body in server.calls if op == "memory.set")
    assert written["value"] == {"task_id": "task-1", "result": "7 squared is 49"}
    assert server.canonical_value == "7 squared is 49"
    assert server.deleted and server.memory is None
    assert all(
        body["memory_key"] != "existing-result"
        for op, _, body in server.calls
        if op == "memory.delete"
    )


async def test_handoff_refuses_to_complete_after_a_noop_result_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = HandoffServer()
    server.force_noop = True
    configure(monkeypatch, server)
    with pytest.raises(RuntimeError, match="result memory was not written"):
        await load_example().main()
    assert server.task is not None and server.task["status"] == "cancelled"
    assert not any(op == "task.complete" for op, _, _ in server.calls)
    assert server.deleted and server.memory is None


@pytest.mark.parametrize("fail_op", ["task.claim", "task.heartbeat", "task.complete"])
async def test_handoff_cancels_with_the_current_owner_after_a_step_fails(
    monkeypatch: pytest.MonkeyPatch,
    fail_op: str,
) -> None:
    server = HandoffServer(fail_op=fail_op)
    configure(monkeypatch, server)
    with pytest.raises(ValidationError):
        await load_example().main()
    assert server.task is not None and server.task["status"] == "cancelled"
    assert server.deleted and server.memory is None
    cancel = next(call for call in server.calls if call[0] == "task.cancel")
    assert cancel[1] == (
        "Bearer planner-token" if fail_op == "task.claim" else "Bearer worker-token"
    )


@pytest.mark.parametrize("bad_field", ["value", "agent", "actor", "source_class", "from_subject"])
async def test_handoff_refuses_an_incorrect_result_or_provenance(
    monkeypatch: pytest.MonkeyPatch,
    bad_field: str,
) -> None:
    server = HandoffServer(bad_field=bad_field)
    configure(monkeypatch, server)
    with pytest.raises(RuntimeError):
        await load_example().main()
    assert server.deleted and server.memory is None


async def test_handoff_still_deletes_memory_when_cancellation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = HandoffServer(bad_field="actor")
    configure(monkeypatch, server)
    server.fail_op = "task.complete"
    original = server.__call__

    def fail_cancel(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/task.cancel":
            return httpx.Response(422, json={"detail": {"code": "VALIDATION_ERROR"}})
        return original(req)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(fail_cancel)),
    )
    with pytest.raises(ValidationError):
        await load_example().main()
    assert server.deleted and server.memory is None


@pytest.mark.parametrize(
    "actor",
    ["process:other:handoff-worker", "human:other:handoff-worker", "invalid:handoff-worker"],
)
async def test_handoff_refuses_a_different_principal_with_the_same_suffix(
    monkeypatch: pytest.MonkeyPatch, actor: str
) -> None:
    server = HandoffServer()
    server.actor = actor
    configure(monkeypatch, server)
    with pytest.raises(RuntimeError, match="another writing principal"):
        await load_example().main()
    assert server.deleted and server.memory is None
