"""memory.export_evidence leaves out the raw chunks and digests behind a key that was
ever 'high', and those whose text reads as a credential: raw text has no sensitivity
column, and a bundle reaches a wider audience than the namespace's grants."""

from __future__ import annotations

from typing import Any

import pytest

import server.memory.store as memory_repo
from server.memory.api.models_evidence_transfer import MemoryExportEvidenceInput
from server.memory.app import chunk_links, evidence_transfer

_KEY = "sk-proj-" + "a" * 40


class _CH:
    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "session_summaries" in sql:
            return [
                {"session_id": "s1", "summary_text": "a plain day", "derived_from": ["k"]},
                {"session_id": "s2", "summary_text": "plain", "derived_from": ["vault"]},
                {"session_id": "s3", "summary_text": f"the key is {_KEY}", "derived_from": []},
            ]
        return [
            {"session_id": "s", "chunk_index": 0, "content": "plain talk"},
            {"session_id": "s", "chunk_index": 1, "content": "about the vault"},
            {"session_id": "s", "chunk_index": 2, "content": f"the key is {_KEY}"},
        ]


@pytest.fixture
def evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _links(_ch: Any, **_k: Any) -> dict[tuple[str, int], list[str]]:
        return {("s", 0): ["k"], ("s", 1): ["vault"], ("s", 2): []}

    async def _ever_high(_org: str, _ns: str, keys: Any, **_k: Any) -> set[str]:
        # `vault` was high once and has since been deleted
        return {"vault"} & set(keys)

    monkeypatch.setattr(evidence_transfer, "get_client", lambda: _CH())
    monkeypatch.setattr(chunk_links, "chunk_memory_keys", _links)
    monkeypatch.setattr(memory_repo, "high_keys", _ever_high)


def _input(kind: str, **over: Any) -> MemoryExportEvidenceInput:
    return MemoryExportEvidenceInput(org="o", namespace="n", kind=kind, **over)


@pytest.mark.asyncio
async def test_chunks_behind_a_high_key_or_holding_a_credential_stay_home(evidence) -> None:
    out = await evidence_transfer.handle_memory_export_evidence(_input("chunks"))
    assert [c.chunk_index for c in out.chunks] == [0]
    assert out.held_high == 2
    every = await evidence_transfer.handle_memory_export_evidence(
        _input("chunks", include_high=True)
    )
    assert [c.chunk_index for c in every.chunks] == [0, 1, 2] and every.held_high == 0


@pytest.mark.asyncio
async def test_digests_behind_a_high_key_or_holding_a_credential_stay_home(evidence) -> None:
    out = await evidence_transfer.handle_memory_export_evidence(_input("digests"))
    assert [d.session_id for d in out.digests] == ["s1"]
    assert out.held_high == 2
