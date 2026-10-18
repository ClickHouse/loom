"""Loom.chunks takes no lightweight UPDATE, and every reader
decides liveness through one predicate."""

from __future__ import annotations

import re
from pathlib import Path

from server.erasure.store import census as erasure_census
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL, live_chunk_sql

ROOT = Path(__file__).resolve().parents[2]


def _server_py() -> list[Path]:
    return [p for p in (ROOT / "server").rglob("*.py") if "__pycache__" not in p.parts]


def test_no_server_code_updates_loom_chunks_in_place() -> None:
    """A lightweight UPDATE leaves a patch part that no merge folds in; under
    one ClickHouse skips the codes-first rescore (upstream #119885)."""
    pat = re.compile(r"UPDATE\s+loom\.chunks\b", re.I)
    hits = [f"{p.relative_to(ROOT)}" for p in _server_py() if pat.search(p.read_text())]
    assert not hits, hits


def test_liveness_is_decided_in_one_place() -> None:
    """Erasure's `doc_superseded_at` column alone is not liveness; only the
    predicate (and erasure's own ALTER mutation, which writes that column)
    names it."""
    allowed = {"server/knowledge/domain/live_chunks.py", "server/erasure/store/steps_memory.py"}
    hits = []
    for p in _server_py():
        rel = str(p.relative_to(ROOT))
        if rel in allowed:
            continue
        if "doc_superseded_at IS NULL" in p.read_text():
            hits.append(rel)
    assert not hits, hits
    src = (ROOT / "server/erasure/store/steps_memory.py").read_text()
    # erasure's mutation_where may use the column; its count must use the predicate
    assert src.count("doc_superseded_at IS NULL") == 1


def test_predicate_shape() -> None:
    assert LIVE_CHUNK_SQL.startswith(
        "doc_superseded_at IS NULL AND (org, namespace, embedding_model, source_uri, doc_version) NOT IN ("
    )
    assert "FROM loom.chunk_supersessions" in LIVE_CHUNK_SQL
    assert live_chunk_sql("c").startswith("c.doc_superseded_at IS NULL AND (c.org, c.namespace,")


def test_every_chunk_writer_stamps_its_ingestion() -> None:
    """A chunk row without doc_version would read as a legacy (0) row and be
    retired by the first supersede of its uri, new ingestion or not."""
    # Every ingest path builds its rows with the one builder.
    for rel, n in (("server/knowledge/store/chunks.py", 1),):
        src = (ROOT / rel).read_text()
        assert src.count('"doc_superseded_at": None,') == n
        assert src.count('"doc_version": ') == n, rel


def test_side_table_is_classified_for_erasure() -> None:
    assert "loom.chunk_supersessions" in erasure_census.NO_MEMORY_CONTENT
