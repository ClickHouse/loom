"""A fresh install gets coded vector columns when the server
knows the codec; the migration files stay byte-identical."""

from __future__ import annotations

from server.core.schema import vector_codec_ddl as vcd

BASELINE = """CREATE TABLE IF NOT EXISTS loom.memory_embeddings
(
    `org` LowCardinality(String),
    `embedding` Array(Float32),
    `embedding_dim` UInt16 DEFAULT 0,
    `memory_key_embedding` Array(Float32) DEFAULT [],
    `version` UInt64
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, memory_key)"""


def test_create_of_a_coded_table_gets_bf16_plus_codec_per_vector_column() -> None:
    out = vcd.rewrite_statement(BASELINE, 3072)
    assert (
        "`embedding` Array(BFloat16) DEFAULT CAST(arrayWithConstant(3072, 0), 'Array(BFloat16)') CODEC(Quantized('rabitq', 3072, 0)),"
        in out
    )
    assert (
        "`memory_key_embedding` Array(BFloat16) DEFAULT CAST(arrayWithConstant(3072, 0), 'Array(BFloat16)') CODEC(Quantized('rabitq', 3072, 0))"
        in out
    )
    assert "`embedding_dim` UInt16 DEFAULT 0" in out  # untouched neighbour
    assert out.count("Quantized(") == 2


def test_entity_vectors_chains_lz4_and_other_tables_are_untouched() -> None:
    ev = "CREATE TABLE IF NOT EXISTS loom.entity_vectors (`entity_text` String, `entity_embedding` Array(Float32), `x` UInt8) ENGINE = MergeTree ORDER BY entity_text"
    assert (
        "`entity_embedding` Array(BFloat16) DEFAULT CAST(arrayWithConstant(1024, 0), 'Array(BFloat16)') CODEC(Quantized('rabitq', 1024, 0), LZ4)"
        in vcd.rewrite_statement(ev, 1024)
    )
    other = "CREATE TABLE IF NOT EXISTS loom.code_chunks_prefix (`embedding` Array(BFloat16) CODEC(NONE)) ENGINE = MergeTree ORDER BY tuple()"
    assert (
        vcd.rewrite_statement(other, 3072) == other
    )  # the prefix twin stays plain: rabitq recall500 0.926 there
    # the file cards fill code_summaries, so a fresh install codes it
    cards = "CREATE TABLE IF NOT EXISTS loom.code_summaries (`embedding` Array(BFloat16) DEFAULT [] CODEC(NONE), `embedding_dim` UInt16 DEFAULT 0) ENGINE = MergeTree ORDER BY tuple()"
    assert (
        "`embedding` Array(BFloat16) DEFAULT CAST(arrayWithConstant(3072, 0), 'Array(BFloat16)') "
        "CODEC(Quantized('rabitq', 3072, 0))," in vcd.rewrite_statement(cards, 3072)
    )
    assert (
        vcd.rewrite_statement("INSERT INTO loom.memory_embeddings VALUES", 3072)
        == "INSERT INTO loom.memory_embeddings VALUES"
    )


def test_type_restatements_of_coded_columns_are_skipped_and_others_kept() -> None:
    m16 = "ALTER TABLE loom.memory_embeddings\n    MODIFY COLUMN `embedding` Array(BFloat16) CODEC(NONE)"
    assert vcd.rewrite_statement(m16, 3072) is None
    m17 = "ALTER TABLE loom.entity_vectors\n    MODIFY COLUMN `entity_embedding` Array(BFloat16) CODEC(LZ4)"
    assert vcd.rewrite_statement(m17, 3072) is None
    other_alter = "ALTER TABLE loom.memory_embeddings ADD COLUMN IF NOT EXISTS `status` LowCardinality(String)"
    assert vcd.rewrite_statement(other_alter, 3072) == other_alter
    granularity = "ALTER TABLE loom.memory_chunks MODIFY SETTING index_granularity = 256"
    assert vcd.rewrite_statement(granularity, 3072) == granularity


def test_statement_settings_only_for_coded_creates() -> None:
    # A bare Quantized column needs the codec enabled and nothing else:
    # allow_suspicious_codecs is READONLY on ClickHouse Cloud.
    assert vcd.statement_settings(
        "CREATE TABLE x (e Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0)))"
    ) == {
        "enable_quantized_codec": 1,
    }
    assert vcd.statement_settings("CREATE TABLE y (e Array(BFloat16) CODEC(NONE))") == {}


def test_statement_settings_asks_for_suspicious_only_when_chained() -> None:
    chained = "CREATE TABLE z (e Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0), LZ4))"
    assert vcd.statement_settings(chained) == {
        "enable_quantized_codec": 1,
        "allow_suspicious_codecs": 1,
    }


def test_migrate_runner_threads_the_probe() -> None:
    from server.core.schema import migrate as _migrate

    class _CH:
        def __init__(self, known: int) -> None:
            self.known = known

        def query(self, sql: str):
            class _R:
                result_rows = [(self.known,)]

            _R.result_rows = [(self.known,)]
            return _R()

    assert _migrate._codec_supported(_CH(1)) is True
    assert _migrate._codec_supported(_CH(0)) is False


def test_every_coded_table_except_entity_vectors_has_a_placeholder_rule() -> None:
    # A coded column rejects an empty vector (code 190), so every coded table's
    # writers must pad; entity_vectors is coded LZ4 only and pads anyway.
    from server.core.vectors.placeholders import PLACEHOLDER_COLUMNS

    for table, (cols, _chain) in vcd.CODED_TABLES.items():
        assert PLACEHOLDER_COLUMNS[f"loom.{table}"] == cols, table


def test_a_declared_non_empty_default_is_kept() -> None:
    stmt = (
        "CREATE TABLE IF NOT EXISTS loom.chunks (`embedding` Array(Float32) DEFAULT [1.0], "
        "`x` UInt8) ENGINE = MergeTree ORDER BY x"
    )
    out = vcd.rewrite_statement(stmt, 3072)
    assert "DEFAULT [1.0] CODEC(Quantized('rabitq', 3072, 0))" in out
