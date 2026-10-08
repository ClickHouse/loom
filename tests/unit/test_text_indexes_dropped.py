"""No text index is declared on loom.memory_embeddings or
loom.memory_evidence. Stem and lemma indexes keep every granule on real
queries, and the content_excerpt_lc and content_lc ones serve too few queries
to pay for their posting lists. The lexical legs need none: hasAnyTokens evaluates on the columns, which stay."""

from __future__ import annotations

from tests.unit.schema_ddl import ddl

DROPPED = ("content_excerpt_stem_text", "content_excerpt_lemma_text", "content_excerpt_text")


def test_dump_no_longer_declares_them_and_keeps_the_columns() -> None:
    table = ddl("memory_embeddings")
    for name in DROPPED:
        assert f"INDEX {name} " not in table, f"{name} still declared in schema.sql"
    # The columns the predicate is evaluated on stay; only the posting lists go.
    assert "`content_excerpt_stem` String MATERIALIZED" in table
    assert "`content_excerpt_lemma` String DEFAULT ''" in table
    assert "`content_excerpt_lc` String MATERIALIZED" in table
    assert "TYPE text(" not in table
    # Merges have nothing to skip.
    assert "content_excerpt_text" not in table
    # The last INDEX line closes the column list without a trailing comma.
    body = table[: table.index("\n)\nENGINE")]
    last = [ln for ln in body.splitlines() if ln.strip()][-1].rstrip()
    assert last.startswith("    INDEX ") and not last.endswith(","), last


def test_memory_evidence_keeps_content_lc_without_a_text_index() -> None:
    table = ddl("memory_evidence")
    assert "`content_lc` String MATERIALIZED lowerUTF8(content)" in table
    assert "INDEX " not in table
