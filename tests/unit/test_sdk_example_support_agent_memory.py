"""sdk/python/examples/support_agent_memory.py runs end to end against a scripted
server that keeps per-user memories and erases a user, deletes what it wrote when a
step in the middle fails, and exits non-zero when a check fails."""

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

EXAMPLE = Path(__file__).resolve().parents[2] / "sdk/python/examples/support_agent_memory.py"


def load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("support_agent_memory_example", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    update_fails: bool = False,
    erasure_leaves_a_fact: bool = False,
) -> tuple[list[tuple[str, str]], list[str]]:
    rows: dict[tuple[str, str], dict[str, Any]] = {}  # (user_id, key) → row
    deleted: list[tuple[str, str]] = []
    ops: list[str] = []

    def server(req: httpx.Request) -> httpx.Response:
        op = req.url.path.removeprefix("/v1/")
        body = json.loads(req.content or b"{}")
        ops.append(op)
        user = body.get("user_id", "-")
        if op == "memory.set":
            row = {"memory_id": f"id-{len(rows)}", "value": body["value"]}
            rows[(user, body["memory_key"])] = row
            return httpx.Response(200, json={"memory_key": body["memory_key"], **row})
        if op == "memory.search":
            hits = [
                {"memory_key": k, "memory_type": "semantic", "content_excerpt": r["value"]}
                for (u, k), r in rows.items()
                if u == user
            ]
            return httpx.Response(200, json={"results": hits})
        if op == "memory.update_by_id":
            if update_fails:
                return httpx.Response(422, json={"detail": {"code": "VALIDATION_ERROR"}})
            for row in rows.values():
                if row["memory_id"] == body["memory_id"]:
                    row["value"] = body["value"]
            return httpx.Response(200, json={"updated": True, "new_version": 2})
        if op == "memory.get":
            row = rows.get((user, body["memory_key"]))
            return httpx.Response(200, json={"found": bool(row), "value": row and row["value"]})
        if op == "erasure.request":
            gone = [key for key in rows if key[0] == body["scope_value"]]
            for key in gone[1:] if erasure_leaves_a_fact else gone:
                del rows[key]
            return httpx.Response(200, json={"erasure_id": "e-1", "status": "in_progress"})
        if op == "erasure.status":
            return httpx.Response(200, json={"status": "completed", "residue_count": 0})
        if op == "memory.delete":
            deleted.append((user, body["memory_key"]))
            rows.pop((user, body["memory_key"]), None)
            return httpx.Response(200, json={"tombstoned": True})
        return httpx.Response(200, json={})

    monkeypatch.setenv("LOOM_API_KEY", "k")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(server)),
    )
    return deleted, ops


def _users(deleted: list[tuple[str, str]]) -> set[str]:
    return {user for user, _ in deleted}


async def test_every_check_passes_and_the_memories_are_deleted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deleted, ops = _install(monkeypatch)
    example = load_example()
    assert await example.main() == 0
    # cleanup asks to delete every key of both customers
    assert sorted(k for _, k in deleted) == sorted(
        k for facts in example.FACTS.values() for k in facts
    )
    assert len(_users(deleted)) == 2
    # the saga is polled, not taken from the request's answer
    assert ops.index("erasure.status") > ops.index("erasure.request")
    out = capsys.readouterr().out
    assert example.REVISED_PLAN in out and "all checks passed" in out


async def test_a_failed_step_still_deletes_the_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted, ops = _install(monkeypatch, update_fails=True)
    example = load_example()
    with pytest.raises(ValidationError):
        await example.main()
    assert len(deleted) == 4 and len(_users(deleted)) == 2
    assert "erasure.request" not in ops


async def test_a_fact_left_after_erasure_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deleted, _ = _install(monkeypatch, erasure_leaves_a_fact=True)
    example = load_example()
    assert await example.main() == 1
    assert len(deleted) == 4
    out = capsys.readouterr().out
    assert "fact(s) still found after erasure" in out and "1 check(s) failed" in out
