"""`memory.pin` and `memory.bulk_set` hand back the server's response, so a
merged pin or a bulk row skipped for a pin or a lock says why it wrote nothing."""

from __future__ import annotations

from typing import Any

from loom import Agent


class _FakeTransport:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response

    async def post(self, op_path: str, payload: dict[str, Any], **_: Any) -> dict[str, Any]:
        return self.response

    async def close(self) -> None:
        pass


def _agent(response: dict[str, Any]) -> Agent:
    a = Agent(org="o", namespace="n", agent="ag", api_key="x")
    a.transport = _FakeTransport(response)  # type: ignore[assignment]
    return a


async def test_a_merged_pin_says_so() -> None:
    res = {"version": 0, "conflict_verdict": "merge", "noop": True, "memory_key": "k"}
    assert await _agent(res).memory.pin("k", "v", "semantic") == res


async def test_bulk_set_keeps_the_skipped_keys() -> None:
    res = {
        "written": [{"memory_key": "a", "version": 5}, {"memory_key": "b", "version": 0}],
        "skipped_pinned": [],
        "skipped_locked": ["b"],
    }
    entries = [{"memory_key": k, "value": k, "memory_type": "semantic"} for k in "ab"]
    assert await _agent(res).memory.bulk_set(entries) == res
