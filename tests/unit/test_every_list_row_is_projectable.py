"""Every list row is projectable.

The `_projection` + wrap-serializer pair on the memory rows
is a shared `ProjectedRow` base; `knowledge.list` gains `fields`, resolved
like memory.list's (`namespace` and `source_uri` always ride), and the Library page names
the eleven fields its rows read — never `content_preview`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.core.errors import ValidationError
from server.knowledge.api import models_knowledge as knowledge
from server.memory.api import models_crud as memory_crud
from server.operations import read_projection

_ROOT = Path(__file__).resolve().parents[2]


def _row(**over):
    base = dict(
        namespace="n",
        source_uri="docs/a.md",
        title="A",
        tags=["x"],
        ingested_at="2026-09-19T00:00:00Z",
        chunk_count=3,
        superseded=False,
        content_preview="first 200 chars",
        description="d",
    )
    base.update(over)
    return knowledge._KnowledgeListEntry(**base)


def test_the_rows_share_one_projected_base() -> None:
    assert issubclass(knowledge._KnowledgeListEntry, read_projection.ProjectedRow)
    assert issubclass(memory_crud._MemoryListEntry, read_projection.ProjectedRow)
    assert issubclass(memory_crud.MemoryGetOutput, read_projection.ProjectedRow)


def test_an_unprojected_knowledge_row_is_unchanged() -> None:
    dumped = _row().model_dump()
    assert dumped["content_preview"] == "first 200 chars" and dumped["title"] == "A"


def test_a_projected_knowledge_row_drops_unrequested_fields() -> None:
    from server.knowledge.app.document_reads import _LIST_ALWAYS

    keep = read_projection.resolve(
        knowledge._KnowledgeListEntry, ["title", "tags"], always=_LIST_ALWAYS
    )
    row = _row()
    row._projection = keep
    assert row.model_dump() == {
        "namespace": "n",
        "source_uri": "docs/a.md",
        "title": "A",
        "tags": ["x"],
    }
    with pytest.raises(ValidationError):
        read_projection.resolve(
            knowledge._KnowledgeListEntry, ["nope"], always=frozenset({"source_uri"})
        )


def test_knowledge_list_takes_fields() -> None:
    inp = knowledge.KnowledgeListInput(org="o", namespace="n", fields=["title"])
    assert inp.fields == ["title"]
    assert knowledge.KnowledgeListInput(org="o", namespace="n").fields is None


def test_the_library_page_asks_for_the_fields_its_rows_read() -> None:
    src = (_ROOT / "frontend/src/views/Documents.tsx").read_text()
    start = src.index("LIBRARY_LIST_FIELDS = [")
    body = src[start + len("LIBRARY_LIST_FIELDS = [") : src.index("];", start)]
    fields = set(json.loads("[" + body.strip().rstrip(",") + "]"))
    known = set(knowledge._KnowledgeListEntry.model_fields)
    assert fields <= known, sorted(fields - known)
    assert (
        "content_preview" not in fields and {"source_uri", "title", "description", "tags"} <= fields
    )
    # Every knowledge.list read on the page names them: the first page, Load more and the
    # read behind a link past the loaded rows.
    assert src.count("fields: LIBRARY_LIST_FIELDS") == src.count("listKnowledge(") >= 2
