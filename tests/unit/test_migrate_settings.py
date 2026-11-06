"""Unit tests for the migration runner's per-statement experimental flags.

Migrations use experimental ClickHouse features (column statistics,
NLP stem/lemmatize materialized columns, vector-similarity HNSW indexes). The
runner sends the matching `allow_experimental_*` flag ONLY for the statements
that use the feature — attaching a flag to an unrelated statement makes some
clusters reject that statement too. Pure function; no CH needed.
"""

from __future__ import annotations

import pytest

from server.core.schema.migrate import _stmt_experimental_settings


@pytest.mark.parametrize(
    ("statement", "flags"),
    [
        pytest.param(
            "ALTER TABLE loom.memory MODIFY COLUMN memory_type "
            "LowCardinality(String) STATISTICS(countmin)",
            {"allow_experimental_statistics": 1},
            id="statistics",
        ),
        pytest.param(
            "ALTER TABLE loom.memory_embeddings ADD COLUMN content_excerpt_stem "
            "String MATERIALIZED arrayStringConcat(arrayMap(t -> stem('en', t), "
            "splitByNonAlpha(content_excerpt)), ' ')",
            {"allow_experimental_nlp_functions": 1},
            id="nlp stem",
        ),
        pytest.param(
            "ALTER TABLE loom.memory_chunks ADD INDEX emb_idx embedding "
            "TYPE vector_similarity('hnsw', 'cosineDistance', 1536, 'bf16', 256, 64)",
            {"allow_experimental_vector_similarity_index": 1},
            id="vector similarity index",
        ),
        pytest.param(
            "CREATE TABLE loom.foo (x UInt32) ENGINE = MergeTree ORDER BY x", None, id="plain"
        ),
        pytest.param(
            "ALTER TABLE t MODIFY COLUMN c String MATERIALIZED stem('en', x) STATISTICS(countmin)",
            {"allow_experimental_statistics": 1, "allow_experimental_nlp_functions": 1},
            id="several features get every flag",
        ),
        # This ALTER never names stem/lemmatize, but CH re-validates the table's
        # content_excerpt_{stem,lemma} MATERIALIZED columns on every ALTER, so the
        # flag is still required (regression: code 344 in CI otherwise).
        pytest.param(
            "ALTER TABLE loom.memory_embeddings ADD COLUMN IF NOT EXISTS files "
            "Array(String) DEFAULT [] AFTER tags, ADD COLUMN IF NOT EXISTS "
            "concepts Array(String) DEFAULT [] AFTER files",
            {"allow_experimental_nlp_functions": 1},
            id="memory_embeddings ALTER without the function",
        ),
        # loom.memory carries no NLP materialized column, so its ALTER must NOT get
        # the flag: the heuristic stays scoped to the table that needs it.
        pytest.param(
            "ALTER TABLE loom.memory ADD COLUMN IF NOT EXISTS files "
            "Array(String) DEFAULT [] AFTER tags",
            None,
            id="other table ALTER gets no nlp flag",
        ),
    ],
)
def test_a_statement_gets_the_flags_its_features_or_table_need(
    statement: str, flags: dict[str, int] | None
) -> None:
    assert _stmt_experimental_settings(statement) == flags


# --------------------------------------------------------------------------
# ClickHouse Cloud compatibility: the NLP experimental flag is never sent
# (Cloud forbids enabling it) where the memory_embeddings ALTERs only carry
# it defensively.
# --------------------------------------------------------------------------


class _CaptureCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    def command(self, sql, settings=None):
        self.calls.append((sql, settings))


def test_apply_file_drops_nlp_flag_when_unavailable(tmp_path) -> None:
    """On a server that forbids NLP (Cloud), the defensive nlp flag on a plain
    memory_embeddings ALTER is dropped so the statement runs."""
    from server.core.schema.migrate import _apply_file

    f = tmp_path / "001_plain.sql"
    f.write_text(
        "ALTER TABLE loom.memory_embeddings "
        "ADD COLUMN IF NOT EXISTS files Array(String) DEFAULT [];"
    )
    ch = _CaptureCH()
    _apply_file(ch, f, dry=False, nlp_ok=False)
    _, settings = ch.calls[0]
    assert settings is None or "allow_experimental_nlp_functions" not in settings


def test_apply_file_keeps_nlp_flag_when_available(tmp_path) -> None:
    from server.core.schema.migrate import _apply_file

    f = tmp_path / "001_nlp.sql"
    f.write_text(
        "ALTER TABLE loom.memory_embeddings ADD COLUMN content_excerpt_stem "
        "String MATERIALIZED stem('en', content_excerpt);"
    )
    ch = _CaptureCH()
    _apply_file(ch, f, dry=False, nlp_ok=True)
    _, settings = ch.calls[0]
    assert settings == {"allow_experimental_nlp_functions": 1}
