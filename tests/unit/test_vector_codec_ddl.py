"""A fresh install gets coded vector columns when this user can have the
codec on; the migration files stay byte-identical."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.core.schema import vector_codec_ddl as vcd
from tests.unit.codec_states import (
    PAIRS,
    SENT,
    SHAPES,
    STATES,
    USABLE,
    codec_rows,
    dict_rows,
    rows,
    settings,
)

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


BARE = "CREATE TABLE x (e Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0)))"
CHAINED = "CREATE TABLE z (e Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0), LZ4))"


def test_statement_settings_only_for_coded_creates() -> None:
    # A bare Quantized column needs the codec enabled and nothing else.
    assert vcd.statement_settings(BARE, settings()) == {"enable_quantized_codec": 1}
    assert (
        vcd.statement_settings("CREATE TABLE y (e Array(BFloat16) CODEC(NONE))", settings()) == {}
    )


def test_statement_settings_asks_for_suspicious_only_when_chained() -> None:
    assert vcd.statement_settings(CHAINED, settings()) == {
        "enable_quantized_codec": 1,
        "allow_suspicious_codecs": 1,
    }


@pytest.mark.parametrize("state", STATES + SHAPES)
def test_a_setting_is_settable_held_or_absent(state: str) -> None:
    """Held at 1 is on already; held at 0 or absent cannot be turned on. A `<max>0</max>`
    or `<disallowed>1</disallowed>` constraint holds a setting as `<readonly/>` does, though
    system.settings shows readonly = 0 for it."""
    codec = vcd.CodecSettings.from_rows(rows(enable_quantized_codec=state))
    assert codec.listed("enable_quantized_codec") is (state != "absent")
    assert codec.usable("enable_quantized_codec") is USABLE[state] is codec.codec
    assert codec.settable("enable_quantized_codec") is SENT[state]
    assert vcd.CodecSettings.from_rows(dict_rows(enable_quantized_codec=state)) == codec


@pytest.mark.parametrize(("codec", "chain"), PAIRS)
def test_a_setting_is_sent_only_where_this_user_may_change_it(codec: str, chain: str) -> None:
    """A readonly setting is never sent: at 1 it needs no change, at 0 it is refused (452)."""
    want = {"enable_quantized_codec": 1} if SENT[codec] else {}
    want |= {"allow_suspicious_codecs": 1} if SENT[chain] else {}
    assert vcd.statement_settings(CHAINED, settings(codec, chain)) == want


@pytest.mark.parametrize("chain", STATES + SHAPES)
def test_the_chain_is_dropped_where_suspicious_codecs_cannot_be_on(chain: str) -> None:
    stmt, sent = settings("writable", chain).statement(CHAINED)
    if USABLE[chain]:
        assert stmt == CHAINED
        assert sent == {"enable_quantized_codec": 1} | (
            {"allow_suspicious_codecs": 1} if SENT[chain] else {}
        )
    else:
        assert stmt == BARE.replace("TABLE x", "TABLE z")
        assert sent == {"enable_quantized_codec": 1}


def test_drop_chains_keeps_the_codes_and_every_other_codec() -> None:
    ddl = (
        "CREATE TABLE t (`a` Array(BFloat16) CODEC(Quantized('rabitq', 8, 0), LZ4),\n"
        "`b` Array(BFloat16) CODEC(Quantized(\\'int8\\', 16, 0), ZSTD(3)),\n"
        "`c` Array(BFloat16) CODEC(Quantized('rabitq', 8, 0)), `d` String CODEC(ZSTD(1)))"
    )
    assert vcd.drop_chains(ddl) == (
        "CREATE TABLE t (`a` Array(BFloat16) CODEC(Quantized('rabitq', 8, 0)),\n"
        "`b` Array(BFloat16) CODEC(Quantized(\\'int8\\', 16, 0)),\n"
        "`c` Array(BFloat16) CODEC(Quantized('rabitq', 8, 0)), `d` String CODEC(ZSTD(1)))"
    )


class _SettingsCH:
    """Answers the codec settings read; records every other statement."""

    def __init__(self, settings_rows: list[tuple[str, str, int]] | None) -> None:
        self.settings_rows = settings_rows
        self.queries: list[str] = []
        self.commands: list[tuple[str, dict[str, int] | None]] = []

    def query(self, sql: str, parameters: dict | None = None, settings: dict | None = None):
        self.queries.append(sql)
        assert sql == vcd.CODEC_SETTINGS_SQL, sql
        if self.settings_rows is None:
            raise RuntimeError("Code: 60. Unknown table system.settings")
        return SimpleNamespace(result_rows=self.settings_rows)

    def command(self, sql: str, settings: dict[str, int] | None = None) -> None:
        self.commands.append((" ".join(sql.split()), settings))


def test_the_runner_reads_the_settings_and_their_constraints_before_sending_anything() -> None:
    from server.core.schema import migrate as mig

    ch = _SettingsCH(codec_rows("on", "off"))
    codec = mig._codec_settings(ch)  # type: ignore[arg-type]
    assert "value, readonly, min, max, disallowed_values FROM system.settings" in ch.queries[0]
    assert ch.commands == [] and codec == settings("on", "off")
    assert mig._codec_settings(_SettingsCH(None)) == vcd.NO_CODEC  # type: ignore[arg-type]
    with pytest.raises(ValueError):  # a row of another shape is a bug, not an older server
        mig._codec_settings(_SettingsCH([(0,)]))  # type: ignore[arg-type,list-item]


@pytest.mark.parametrize(
    ("codec", "chain", "width", "line"),
    [
        ("absent", "writable", None, "no Quantized codec here (it needs 26.8 or later)"),
        ("off", "writable", None, "enable_quantized_codec is held at 0 here"),
        ("max0", "writable", None, "enable_quantized_codec is held at 0 here"),
        ("max0_on", "disallowed1", 1024, "no general codec after it"),
        ("writable", "writable", 1024, "fresh vector tables are created coded"),
        ("on", "on", 1024, "fresh vector tables are created coded"),
        ("on", "off", 1024, "no general codec after it (allow_suspicious_codecs is held at 0"),
        ("writable", "absent", 1024, "no general codec after it"),
    ],
)
def test_the_layout_line_says_why(
    monkeypatch: pytest.MonkeyPatch, codec: str, chain: str, width: int | None, line: str
) -> None:
    from server.core.schema import migrate as mig

    monkeypatch.setattr(mig, "_install_vector_width", lambda ch: 1024)
    got, why = mig._new_table_layout(object(), settings(codec, chain))  # type: ignore[arg-type]
    assert got == width and line in why
    assert ("general codec" in why) is (width is not None and not USABLE[chain])


_CODED_FILE = """CREATE TABLE IF NOT EXISTS loom.entity_vectors
(
    `entity_text` String,
    `entity_embedding` Array(BFloat16) CODEC(LZ4)
)
ENGINE = MergeTree ORDER BY entity_text;

CREATE TABLE IF NOT EXISTS loom.memory_embeddings
(
    `org` LowCardinality(String),
    `embedding` Array(Float32)
)
ENGINE = MergeTree ORDER BY org;
"""


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path, codec: str, chain: str) -> list:
    """The statements a fresh install sends, and their settings, on a server whose
    codec settings are in these states."""
    from server.core.schema import migrate as mig
    from server.core.schema.journal import sorted_migrations

    journal = tmp_path / f"{codec}-{chain}"
    journal.mkdir()
    (journal / "1999_vectors.sql").write_text(_CODED_FILE)
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_install_vector_width", lambda ch: 1024)
    monkeypatch.setattr(mig, "_dimensions_setting_known", lambda ch: False)
    monkeypatch.setattr(mig, "_sync_settings_accepted", lambda ch: frozenset())
    monkeypatch.setattr(mig, "_record", lambda ch, name, cs: None)
    ch = _SettingsCH(codec_rows(codec, chain))
    files = sorted_migrations(journal.glob("*.sql"))
    assert mig._apply_pending(ch, files, dry=False, lease=None) == 0  # type: ignore[arg-type]
    return ch.commands


@pytest.mark.parametrize(("codec", "chain"), PAIRS)
def test_a_fresh_install_follows_the_codec_settings_it_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path, codec: str, chain: str
) -> None:
    """Every state pair: coded where the codec can be on, the LZ4 chain kept where
    allow_suspicious_codecs can be, and a setting sent only where it is settable."""
    _assert_layout(_install(monkeypatch, tmp_path, codec, chain), codec, chain)


@pytest.mark.parametrize("shape", SHAPES)
def test_a_constraint_holds_a_codec_setting_as_readonly_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path, shape: str
) -> None:
    """system.settings shows readonly = 0 under `<max>0</max>` or `<disallowed>1</disallowed>`,
    and the server still refuses 1 (code 452)."""
    _assert_layout(_install(monkeypatch, tmp_path, shape, "writable"), shape, "writable")
    _assert_layout(_install(monkeypatch, tmp_path, "on", shape), "on", shape)


def _assert_layout(commands: list, codec: str, chain: str) -> None:
    (entity, entity_settings), (memory, memory_settings) = commands
    if not USABLE[codec]:
        assert "Quantized" not in entity + memory
        assert entity_settings is None and memory_settings is None
        return
    codes = "Quantized('rabitq', 1024, 0)"
    kept = USABLE[chain]
    assert f"`entity_embedding` Array(BFloat16) CODEC({codes}{', LZ4' if kept else ''})" in entity
    assert f"`embedding` Array(BFloat16) CODEC({codes})" in memory
    on = {"enable_quantized_codec": 1} if SENT[codec] else {}
    chained = on | ({"allow_suspicious_codecs": 1} if kept and SENT[chain] else {})
    assert entity_settings == (chained or None)
    assert memory_settings == (on or None)


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
    monkeypatch.setattr(mig, "_codec_settings", lambda ch: settings())
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
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_codec_settings", lambda ch: settings())
    monkeypatch.setattr(mig, "_install_vector_width", lambda ch: None)
    monkeypatch.setattr(mig, "_dimensions_setting_known", lambda ch: False)
    monkeypatch.setattr(mig, "_apply_file", lambda ch, f, **kw: None)
    files = [tmp_path / "1999_a.sql"]
    assert mig._apply_pending(object(), files, dry=True, lease=None) == 0  # type: ignore[arg-type]
    assert ("created plain" in capsys.readouterr().out) is noted


def test_a_server_without_the_codec_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys, tmp_path
) -> None:
    from server.core.schema import migrate as mig

    (tmp_path / "1999_a.sql").write_text(
        "CREATE TABLE IF NOT EXISTS loom.chunks (embedding Array(Float32)) ENGINE = Memory;\n"
    )
    monkeypatch.setattr(mig, "_nlp_available", lambda ch: True)
    monkeypatch.setattr(mig, "_codec_settings", lambda ch: vcd.NO_CODEC)
    monkeypatch.setattr(mig, "_dimensions_setting_known", lambda ch: False)
    monkeypatch.setattr(mig, "_apply_file", lambda ch, f, **kw: None)
    files = [tmp_path / "1999_a.sql"]
    assert mig._apply_pending(object(), files, dry=True, lease=None) == 0  # type: ignore[arg-type]
    assert "no Quantized codec here" in capsys.readouterr().out


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
    monkeypatch.setattr(mig, "_codec_settings", lambda ch: settings())
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
