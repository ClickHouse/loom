"""A fresh install gets coded vector columns when the server
knows the codec; the migration files stay byte-identical."""

from __future__ import annotations

import pytest

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
    assert "`embedding` Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0))," in out
    assert (
        "`memory_key_embedding` Array(BFloat16) DEFAULT [] CODEC(Quantized('rabitq', 3072, 0))"
        in out
    )
    assert "`embedding_dim` UInt16 DEFAULT 0" in out  # untouched neighbour
    assert out.count("Quantized(") == 2


def test_entity_vectors_chains_lz4_and_other_tables_are_untouched() -> None:
    ev = "CREATE TABLE IF NOT EXISTS loom.entity_vectors (`entity_text` String, `entity_embedding` Array(Float32), `x` UInt8) ENGINE = MergeTree ORDER BY entity_text"
    assert (
        "`entity_embedding` Array(BFloat16) CODEC(Quantized('rabitq', 1024, 0), LZ4)"
        in vcd.rewrite_statement(ev, 1024)
    )
    other = "CREATE TABLE IF NOT EXISTS loom.code_chunks_prefix (`embedding` Array(BFloat16) CODEC(NONE)) ENGINE = MergeTree ORDER BY tuple()"
    assert (
        vcd.rewrite_statement(other, 3072) == other
    )  # the prefix twin stays plain: rabitq recall500 0.926 there
    # the file cards fill code_summaries, so a fresh install codes it
    cards = "CREATE TABLE IF NOT EXISTS loom.code_summaries (`embedding` Array(BFloat16) DEFAULT [] CODEC(NONE), `embedding_dim` UInt16 DEFAULT 0) ENGINE = MergeTree ORDER BY tuple()"
    assert (
        "`embedding` Array(BFloat16) DEFAULT [] CODEC(Quantized('rabitq', 3072, 0)),"
        in vcd.rewrite_statement(cards, 3072)
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


def test_codable_widths_are_whole_bytes_of_rabitq_bits() -> None:
    assert vcd.codable(8) and vcd.codable(384) and vcd.codable(3072)
    assert not vcd.codable(12) and not vcd.codable(0)


@pytest.mark.parametrize(("dim", "coded"), [(3072, True), (1020, False)])
def test_a_fresh_install_codes_its_vectors_only_at_a_codable_width(
    monkeypatch: pytest.MonkeyPatch, tmp_path, dim: int, coded: bool
) -> None:
    from server.core.schema import migrate as mig
    from server.core.schema.journal import sorted_migrations

    (tmp_path / "1999_a.sql").write_text(
        "CREATE TABLE IF NOT EXISTS loom.x (a UInt8) ENGINE = Memory;\n"
    )
    seen: list[bool] = []
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_codec_supported", lambda ch: True)
    monkeypatch.setattr(mig, "_install_vector_width", lambda ch: dim)
    monkeypatch.setattr(mig, "_dimensions_setting_known", lambda ch: False)
    monkeypatch.setattr(mig, "_sync_settings_accepted", lambda ch: frozenset())
    monkeypatch.setattr(mig, "_run_file", lambda ch, f, **kw: seen.append(kw["coded"]))
    monkeypatch.setattr(mig, "_record", lambda ch, name, cs: None)
    files = sorted_migrations(tmp_path.glob("*.sql"))
    assert mig._apply_pending(object(), files, dry=False, lease=None) == 0  # type: ignore[arg-type]
    assert seen == [coded]


@pytest.mark.parametrize(
    ("sql", "noted"),
    [
        (
            "CREATE TABLE IF NOT EXISTS loom.chunks (embedding Array(Float32)) ENGINE = Memory;",
            True,
        ),
        ("CREATE TABLE IF NOT EXISTS loom.x (a UInt8) ENGINE = Memory;", False),
    ],
)
def test_the_plain_layout_is_noted_only_when_a_vector_table_is_created(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path, sql: str, noted: bool
) -> None:
    from server.core.schema import migrate as mig

    (tmp_path / "1999_a.sql").write_text(sql + "\n")
    monkeypatch.setattr(mig, "_codec_supported", lambda ch: True)
    monkeypatch.setattr(mig, "_install_vector_width", lambda ch: None)
    assert mig._new_table_codec_width(object(), [tmp_path / "1999_a.sql"]) is None  # type: ignore[arg-type]
    assert ("created plain" in capsys.readouterr().out) is noted


@pytest.mark.parametrize(
    ("columns", "model", "width"),
    [
        (
            [
                ("memory_embeddings", "embedding", "CODEC(Quantized('rabitq', 16, 0))"),
                ("entity_vectors", "entity_embedding", "CODEC(Quantized('rabitq', 16, 0), LZ4)"),
                ("memory_embeddings", "memory_key_embedding", "CODEC(NONE)"),  # mid-rebuild
            ],
            "fake-ngram-3072",
            16,
        ),
        ([("memory_embeddings", "embedding", "CODEC(NONE)")], "fake-ngram-3072", None),
        ([("memory_embeddings", "org", "")], "fake-ngram-3072", 3072),  # no vector column yet
        ([], "fake-ngram-3072", 3072),
    ],
    ids=["stored-width", "plain-install", "not-a-vector", "fresh-install"],
)
def test_new_coded_tables_take_the_width_the_install_stores(
    settings_override, columns: list[tuple[str, str, str]], model: str, width: int | None
) -> None:
    """A model chosen in the dashboard leaves LOOM_EMBEDDING_DIM at its old value."""
    from types import SimpleNamespace

    from server.core.schema import migrate as mig

    settings_override(embedding_dim=3072, embedding_model=model)

    class _CH:
        def query(self, sql: str, parameters: dict | None = None) -> SimpleNamespace:
            assert "memory_embeddings" in parameters["tables"]
            return SimpleNamespace(result_rows=columns)

    assert mig._install_vector_width(_CH()) == width  # type: ignore[arg-type]


@pytest.mark.parametrize(("width", "coded"), [(None, False), (384, True)])
def test_an_install_with_plain_vector_tables_creates_new_ones_plain(
    monkeypatch: pytest.MonkeyPatch, tmp_path, width: int | None, coded: bool
) -> None:
    from server.core.schema import migrate as mig
    from server.core.schema.journal import sorted_migrations

    (tmp_path / "1999_a.sql").write_text(
        "CREATE TABLE IF NOT EXISTS loom.x (a UInt8) ENGINE = Memory;\n"
    )
    seen: list[tuple[bool, int]] = []
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_codec_supported", lambda ch: True)
    monkeypatch.setattr(mig, "_install_vector_width", lambda ch: width)
    monkeypatch.setattr(mig, "_dimensions_setting_known", lambda ch: False)
    monkeypatch.setattr(mig, "_sync_settings_accepted", lambda ch: frozenset())
    monkeypatch.setattr(
        mig, "_run_file", lambda ch, f, **kw: seen.append((kw["coded"], kw["codec_dim"]))
    )
    monkeypatch.setattr(mig, "_record", lambda ch, name, cs: None)
    files = sorted_migrations(tmp_path.glob("*.sql"))
    assert mig._apply_pending(object(), files, dry=False, lease=None) == 0  # type: ignore[arg-type]
    assert seen == [(coded, width or 0)]


def test_codec_width_reads_the_quantized_argument() -> None:
    assert vcd.codec_width("CODEC(Quantized(\\'rabitq\\', 1024, 0), LZ4)") == 1024
    assert vcd.codec_width("CODEC(ZSTD(1))") is None


def test_coded_tables_of_several_widths_are_named(settings_override, capsys) -> None:
    from types import SimpleNamespace

    from server.core.schema import migrate as mig

    settings_override(embedding_dim=3072, embedding_model="fake-ngram-3072")
    rows = [
        ("memory_embeddings", "embedding", "CODEC(Quantized('rabitq', 16, 0))"),
        ("chunks", "embedding", "CODEC(Quantized('rabitq', 16, 0))"),
        ("categories", "embedding", "CODEC(Quantized('rabitq', 8, 0))"),
    ]
    ch = SimpleNamespace(query=lambda sql, parameters=None: SimpleNamespace(result_rows=rows))
    assert mig._install_vector_width(ch) == 16  # type: ignore[arg-type]
    assert "several widths [8, 16]" in capsys.readouterr().out
