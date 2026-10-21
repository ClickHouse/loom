"""SDK LearningsClient.review leaves `destination` to the server unless given:
the server sends a bot's rule to knowledge only when the field is unset."""

from __future__ import annotations

from typing import Any

import pytest
from loom.learnings import LearningsClient


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(
        self, op_path: str, payload: dict[str, Any], *a: Any, **k: Any
    ) -> dict[str, Any]:
        self.calls.append((op_path, payload))
        return {"ok": True}


class _Agent:
    def __init__(self) -> None:
        self.org = "acme"
        self.namespace = "support"
        self.transport = _Transport()


@pytest.mark.asyncio
async def test_review_sends_destination_only_when_given() -> None:
    ag = _Agent()
    c = LearningsClient(ag)  # type: ignore[arg-type]
    await c.review("cid-1", "promote")
    await c.review("cid-1", "promote", destination="memory")
    (op, unset), (_, given) = ag.transport.calls
    assert op == "/v1/learnings.review"
    assert "destination" not in unset
    assert given["destination"] == "memory"
