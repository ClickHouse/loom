"""Where FINAL stays and where it does not, as a census.

No read of `memory_evidence` or `memory_belief_sources` uses FINAL: the key
is minted per insert, and the query-time merge would drag the wide `content`
column through for nothing (20.28 vs 2.71 MiB on the same granules). This
census pins that, so a new reader that says FINAL on either table fails here.

The link tables whose rows are all key columns keep FINAL where it was
measured cheaper in wall than the GROUP BY that replaces it — facts.list's
fan-out over `entity_triple_memories` and the export read of
`memory_chunk_sources` (wall before bytes). The lint below asks that the
shapes stay the measured ones and that no aggregate is aliased back to its
source column.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "server"

# A table name followed (possibly across a line break and an alias) by FINAL.
_FINAL_AFTER = {
    t: re.compile(rf"loom\.{t}\b(?:\s+AS\s+\w+)?\s+FINAL", re.IGNORECASE)
    for t in ("memory_evidence", "memory_belief_sources")
}


def _server_sources() -> list[Path]:
    return sorted(p for p in SERVER.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_read_of_the_evidence_tables_says_final() -> None:
    offenders: list[str] = []
    for path in _server_sources():
        text = path.read_text(encoding="utf-8")
        for table, rx in _FINAL_AFTER.items():
            for m in rx.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(ROOT)}:{line} {table} FINAL")
    assert not offenders, "a read of a per-insert-key evidence table says FINAL: " + "; ".join(
        offenders
    )


def test_evidence_readers_decide_the_row_by_version_not_by_merge() -> None:
    """The deep pass's two keyed reads carry the version rule
    in the statement: `LIMIT 1 BY` the table key after `ORDER BY … DESC`,
    tombstone filter outside, no FINAL."""
    import asyncio

    from server.memory.app.deep_evidence import fetch_raw_evidence
    from tests.unit.test_memory_search_service import StubCH

    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "memory_key": "k1",
                "support_type": "extracted_from",
                "char_start": 0,
                "char_end": 1,
                "evidence_id": "00000000-0000-0000-0000-000000000001",
            }
        ]
    )
    ch.query_all_returns.append([])
    asyncio.run(
        fetch_raw_evidence(
            org="o",
            namespace="n",
            memory_keys=["k1"],
            max_chars_per_memory=10,
            max_chars_total=10,
            ch=ch,
        )
    )
    assert len(ch.query_all_calls) == 2
    for sql, _ in ch.query_all_calls:
        assert "FINAL" not in sql and "LIMIT 1 BY" in sql and "tombstoned_at IS NULL" in sql


def test_link_table_reads_keep_final_and_alias_aggregates_to_new_names() -> None:
    import inspect

    from server.memory.app import chunk_links as cl
    from server.memory.app import facts

    facts_src = inspect.getsource(facts)
    assert facts_src.count("FROM loom.entity_triple_memories FINAL") == 1, (
        "facts.list keeps the merge"
    )
    assert "SELECT DISTINCT subject, relation, object, memory_key" not in facts_src, (
        "the DISTINCT subquery under FINAL is redundant and measured slower"
    )
    for sql in (cl.KEYS_SQL,):
        assert " FINAL" in sql and "tombstoned_at IS NULL" in sql and "PREWHERE" not in sql
    for sql in (cl.KEYS_SQL,):
        for agg, alias in re.findall(r"\b(\w+(?:\(\d+\))?\([^()]*\))\s+AS\s+(\w+)", sql):
            inner = agg[agg.index("(") :]
            assert not re.search(rf"\b{alias}\b", inner), (agg, alias)
