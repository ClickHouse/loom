"""knowledge.outline: the ToC from chunk metadata and
one section's text; unstructured / legacy documents report structured=False."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest


def _row(idx: int, content: str, path: list[str] | None, depth: int = 0) -> Any:
    meta = (
        {"section_path": path, "section_depth": depth, "section_idx": 0, "char_start": 0}
        if path
        else {}
    )
    return NS(
        chunk_idx=idx,
        content=content,
        chunk_metadata=json.dumps(meta),
        ingested_at=datetime.now(UTC),
    )


def _wire(monkeypatch: pytest.MonkeyPatch, rows: list[Any], *, doc: bool = True) -> None:
    from server.knowledge.store import chunks as knowledge_repo
    from server.knowledge.store import documents as documents_repo

    async def _get_one(org: str, ns: str, uri: str, **kw: Any) -> Any:
        return NS(title="Handbook", source_uri=uri) if doc else None

    async def _list(org: str, ns: str, uri: str, *, embedding_model: str, **kw: Any) -> list[Any]:
        assert embedding_model == "stub-model"
        return rows

    monkeypatch.setattr(documents_repo, "get_one", _get_one)
    monkeypatch.setattr(knowledge_repo, "list_for_doc", _list)
    monkeypatch.setattr(
        "server.knowledge.app.document_reads.get_embedder", lambda: NS(model_name="stub-model")
    )


def test_outline_lists_sections_and_returns_a_section_with_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeOutlineInput
    from server.knowledge.app.document_reads import handle_knowledge_outline

    rows = [
        _row(0, "preamble", None),
        _row(1, "gov intro", ["Governance"], 1),
        _row(2, "institutions a", ["Governance", "Institutions"], 2),
        _row(3, "institutions b", ["Governance", "Institutions"], 2),
        _row(4, "electoral", ["Governance", "Institutions", "Electoral Systems"], 3),
        _row(5, "parties", ["Governance", "Parties"], 2),
    ]
    _wire(monkeypatch, rows)
    out = asyncio.run(
        handle_knowledge_outline(
            KnowledgeOutlineInput(org="o", namespace="ns", source_uri="kb://h")
        )
    )
    assert out.found and out.structured and out.title == "Handbook" and out.chunk_count == 6
    assert [e.title for e in out.sections] == [
        "Governance",
        "Institutions",
        "Electoral Systems",
        "Parties",
    ]
    inst = next(e for e in out.sections if e.title == "Institutions")
    assert inst.path == ["Governance", "Institutions"] and inst.depth == 2
    assert inst.chunk_idxs == [2, 3] and inst.chars == len("institutions a") + len("institutions b")
    assert out.section_content is None

    out = asyncio.run(
        handle_knowledge_outline(
            KnowledgeOutlineInput(
                org="o",
                namespace="ns",
                source_uri="kb://h",
                section_path=["Governance", "Institutions"],
            )
        )
    )
    assert out.section_chunk_idxs == [2, 3, 4]  # children folded in
    assert out.section_content == "institutions a\n\ninstitutions b\n\nelectoral"
    out = asyncio.run(
        handle_knowledge_outline(
            KnowledgeOutlineInput(
                org="o",
                namespace="ns",
                source_uri="kb://h",
                section_path=["Governance", "Institutions"],
                include_children=False,
            )
        )
    )
    assert out.section_chunk_idxs == [2, 3]
    out = asyncio.run(
        handle_knowledge_outline(
            KnowledgeOutlineInput(
                org="o", namespace="ns", source_uri="kb://h", section_path=["Nope"]
            )
        )
    )
    assert out.section_content is None and out.section_chunk_idxs == []


def test_outline_on_legacy_or_headless_document_is_unstructured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeOutlineInput
    from server.knowledge.app.document_reads import handle_knowledge_outline

    rows = [_row(0, "a", None), _row(1, "b", None)]
    rows[1].chunk_metadata = "not json"
    _wire(monkeypatch, rows)
    out = asyncio.run(
        handle_knowledge_outline(
            KnowledgeOutlineInput(org="o", namespace="ns", source_uri="kb://h")
        )
    )
    assert out.found and out.structured is False and out.sections == [] and out.chunk_count == 2


def test_outline_missing_document(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeOutlineInput
    from server.knowledge.app.document_reads import handle_knowledge_outline

    _wire(monkeypatch, [], doc=False)
    out = asyncio.run(
        handle_knowledge_outline(
            KnowledgeOutlineInput(org="o", namespace="ns", source_uri="kb://x")
        )
    )
    assert out.found is False and out.structured is False
