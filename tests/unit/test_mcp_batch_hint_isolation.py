"""A batched MCP call's ledger row carries only its own mentor hint and
traffic class.

A JSON-RPC batch runs its entries sequentially in one task, and both live
in ContextVars the ledger row reads (`out_hint_code`, `traffic_class`). Left
set by a call that succeeded, they would put the previous entry's values on
the next entry's row — a refusal that showed no hint and never reached the
classifier.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.observability import ledger, traffic_class
from server.transport import mcp_call, mcp_outcome


def test_a_refused_entry_does_not_record_the_previous_entrys_hint_or_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    def _emit(op_name: str, *a: Any, **k: Any) -> None:
        seen.append(
            (
                k.get("error_code", ""),
                ledger.current_hint_code(),
                traffic_class.current_traffic_class(),
            )
        )

    async def _no_defaults(auth: Any, client_name: str | None = None) -> None:
        return None

    monkeypatch.setattr(mcp_call, "_emit_mcp_event", _emit)

    monkeypatch.setattr(mcp_outcome, "_emit_mcp_event", _emit)
    monkeypatch.setattr(mcp_call, "_caller_defaults", _no_defaults)

    async def batch() -> None:
        # What a previous entry's success left behind.
        ledger.set_hint_context("unused_plane")
        traffic_class.set_current_traffic_class("bench", "claim")
        params = {"name": "loom_memory_get", "arguments": {"memory_key": 5}}
        await mcp_call._call_tool(2, params)

    asyncio.run(batch())
    assert seen == [("VALIDATION_FAILED", "", "")]
