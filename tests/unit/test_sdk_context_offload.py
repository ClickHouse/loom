"""SDK: agent.context.offload / fetch post the wire shape and type the answer."""

from __future__ import annotations

import asyncio
from typing import Any

from loom.context import ContextClient, FetchedSlice, OffloadedBlob


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(
        self, op_path: str, payload: dict[str, Any], *a: Any, **k: Any
    ) -> dict[str, Any]:
        self.calls.append((op_path, payload))
        if op_path.endswith("context.offload"):
            return {
                "blob_key": "blob/abc",
                "content_bytes": 12,
                "content_chars": 12,
                "sha256": "ff",
                "summary": "s",
                "expires_at": "2026-09-18T00:00:00",
                "pointer": "[offloaded 12 bytes / 1 lines → context.fetch blob_key=blob/abc] s",
            }
        return {
            "found": True,
            "blob_key": payload["blob_key"],
            "content": "12: FAILED x",
            "total_chars": 100,
            "total_lines": 20,
            "offset": 0,
            "next_offset": -1,
            "matched_lines": 1,
            "content_type": "text/plain",
            "summary": "s",
        }


class _Memory:
    def _identity(self, user_id, session_id, *, run_id=None, agent_id=None):
        return {"org": "o", "namespace": "ns", "agent": agent_id or "a"}


class _Agent:
    def __init__(self) -> None:
        self.transport = _Transport()
        self.memory = _Memory()


def test_offload_posts_content_and_returns_pointer() -> None:
    ag = _Agent()
    out = asyncio.run(ContextClient(ag).offload("hello world!", summary="s", blob_key="k"))
    op, body = ag.transport.calls[0]
    assert op == "/v1/context.offload"
    assert (
        body["content"] == "hello world!"
        and body["blob_key"] == "k"
        and body["ttl_seconds"] == 7 * 86_400
    )
    assert body["org"] == "o" and body["agent"] == "a"
    assert isinstance(out, OffloadedBlob) and out.blob_key == "blob/abc"
    assert str(out).startswith("[offloaded 12 bytes")


def test_fetch_posts_pattern_and_types_the_slice() -> None:
    ag = _Agent()
    out = asyncio.run(
        ContextClient(ag).fetch("blob/abc", pattern="FAILED", context_lines=2, limit=500)
    )
    op, body = ag.transport.calls[0]
    assert op == "/v1/context.fetch"
    assert body["pattern"] == "FAILED" and body["context_lines"] == 2 and body["limit"] == 500
    assert isinstance(out, FetchedSlice) and out.found and out.matched_lines == 1
    assert str(out) == "12: FAILED x" and out.next_offset == -1
