"""sdk/python/examples/agent_memory.py runs end to end against a scripted server
and deletes what it wrote, even when a step in the middle fails."""

from __future__ import annotations

import functools
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from loom import ValidationError

EXAMPLE = Path(__file__).resolve().parents[2] / "sdk/python/examples/agent_memory.py"


def load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("agent_memory_example", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("ask_fails", [False, True])
async def test_the_example_deletes_its_memories(
    monkeypatch: pytest.MonkeyPatch, ask_fails: bool
) -> None:
    deleted: list[str] = []

    def server(req: httpx.Request) -> httpx.Response:
        op = req.url.path.removeprefix("/v1/")
        body = json.loads(req.content or b"{}")
        if op == "ops.capabilities":
            return httpx.Response(200, json={"ops": [], "build_commit": "test"})
        if op == "memory.search":
            return httpx.Response(200, json={"results": []})
        if op == "memory.ask":
            if ask_fails:
                return httpx.Response(422, json={"detail": {"code": "VALIDATION_ERROR"}})
            return httpx.Response(200, json={"abstain": True, "memory_keys": []})
        if op == "memory.get":
            return httpx.Response(200, json={"found": False})
        if op == "memory.delete":
            deleted.append(body["memory_key"])
            return httpx.Response(200, json={"tombstoned": True})
        if op == "memory.no_such_op":
            return httpx.Response(404, json={"detail": {"code": "RESOURCE_NOT_FOUND"}})
        return httpx.Response(200, json={"memory_key": body.get("memory_key", "")})

    monkeypatch.setenv("LOOM_API_KEY", "k")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(server)),
    )
    example = load_example()
    if ask_fails:
        with pytest.raises(ValidationError):
            await example.main()
    else:
        await example.main()
    assert deleted == list(example.KEYS)
