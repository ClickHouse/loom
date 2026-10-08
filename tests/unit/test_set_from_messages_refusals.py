"""memory.set_from_messages refuses a malformed request before it writes anything."""

from __future__ import annotations

import asyncio

import pytest


async def test_a_schema_request_without_a_key_writes_no_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 400 comes before the evidence write starts, so a refused request leaves no
    evidence rows behind."""
    from fastapi import HTTPException

    from server.memory.api.models_bulk import MemorySetFromMessagesInput
    from server.memory.app.extract import handler as hm

    started: list[object] = []

    async def _evidence(*a: object, **k: object) -> list[str]:
        started.append(a)
        return []

    monkeypatch.setattr(hm, "_source_evidence_ids", _evidence)
    monkeypatch.setattr(hm, "_deferred_by_allowlist", lambda _inp: False)
    with pytest.raises(HTTPException) as err:
        await hm.handle_memory_set_from_messages(
            MemorySetFromMessagesInput(
                org="o",
                namespace="ns",
                agent="a",
                messages=[{"role": "user", "content": "hi"}],
                record_schema={"name": "string"},
            )
        )
    assert err.value.status_code == 400
    await asyncio.sleep(0)
    assert started == []
