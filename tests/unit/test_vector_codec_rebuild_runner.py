"""The generalised rebuild runner's pure parts."""

from __future__ import annotations

import pytest

from scripts.ops import vector_codec_rebuild as vcr
from server.core.schema.vector_codec_ddl import statement_settings
from tests.unit.codec_states import settings

DDL = """CREATE TABLE loom.memory_chunks
(
    `org` LowCardinality(String),
    `embedding` Array(BFloat16) CODEC(NONE),
    `created_at` DateTime
)
ENGINE = SharedReplacingMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}', created_at)
ORDER BY (org, namespace, session_id, chunk_index, content_sha256)
SETTINGS index_granularity = 256"""


def test_rewrite_ddl_recodes_only_the_vector_column_and_renames() -> None:
    out = vcr.rewrite_ddl(
        DDL, "loom.memory_chunks", "loom.memory_chunks_q", ["embedding"], None, dim=3072
    )
    assert out.startswith("CREATE TABLE loom.memory_chunks_q")
    assert "`embedding` Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0))," in out
    assert "index_granularity = 256" in out and "CODEC(NONE)" not in out


def test_rewrite_ddl_keeps_default_and_chains_a_general_codec() -> None:
    ddl = DDL.replace(
        "`embedding` Array(BFloat16) CODEC(NONE)",
        "`entity_embedding` Array(BFloat16) DEFAULT [] CODEC(LZ4)",
    )
    out = vcr.rewrite_ddl(
        ddl, "loom.memory_chunks", "loom.memory_chunks_q", ["entity_embedding"], "LZ4", dim=3072
    )
    assert (
        "`entity_embedding` Array(BFloat16) DEFAULT [] CODEC(Quantized('rabitq', 3072, 0), LZ4),"
        in out
    )


def test_rewrite_ddl_moves_a_float32_column_to_coded_bfloat16() -> None:
    # Loom.qa_item_embeddings was created Array(Float32) with no codec.
    ddl = DDL.replace(
        "`embedding` Array(BFloat16) CODEC(NONE)", "`embedding` Array(Float32) DEFAULT []"
    )
    out = vcr.rewrite_ddl(
        ddl,
        "loom.memory_chunks",
        "loom.memory_chunks_q",
        ["embedding"],
        None,
        granularity=256,
        dim=3072,
    )
    assert "`embedding` Array(BFloat16) DEFAULT [] CODEC(Quantized('rabitq', 3072, 0))," in out
    assert "Float32" not in out and "index_granularity = 256" in out


def test_recall_gate_ranks_a_float32_source_at_bfloat16() -> None:
    f32 = DDL.replace(
        "`embedding` Array(BFloat16) CODEC(NONE)", "`embedding` Array(Float32) DEFAULT []"
    )
    assert vcr.exact_vector_expr(f32, "embedding") == "CAST(embedding AS Array(BFloat16))"
    assert vcr.exact_vector_expr(DDL, "embedding") == "embedding"


def test_parse_engine_reads_version_and_sorting_key() -> None:
    engine, version, key = vcr.parse_engine(DDL)
    assert engine == "SharedReplacingMergeTree" and version == "created_at"
    assert key == ["org", "namespace", "session_id", "chunk_index", "content_sha256"]
    plain = DDL.replace(
        "SharedReplacingMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}', created_at)",
        "SharedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')",
    )
    assert vcr.parse_engine(plain)[1] is None


def test_replace_select_pads_every_named_column() -> None:
    sel = vcr.replace_select(["embedding", "memory_key_embedding"], dim=3072)
    assert sel.startswith("SELECT * REPLACE (")
    assert sel.count("arrayResize(CAST([] AS Array(BFloat16)), 3072, toBFloat16(0))") == 2
    assert "AS memory_key_embedding" in sel


def test_the_twin_create_asks_for_suspicious_codecs_only_when_chained() -> None:
    # allow_suspicious_codecs is readonly on ClickHouse Cloud, so it is sent only
    # when chaining, and only where this user may change it.
    plain = vcr.rewrite_ddl(
        DDL, "loom.memory_chunks", "loom.memory_chunks_q", ["embedding"], None, dim=3072
    )
    chained = vcr.rewrite_ddl(
        DDL, "loom.memory_chunks", "loom.memory_chunks_q", ["embedding"], "LZ4", dim=3072
    )
    assert statement_settings(plain, settings()) == {"enable_quantized_codec": 1}
    assert statement_settings(chained, settings()) == {
        "enable_quantized_codec": 1,
        "allow_suspicious_codecs": 1,
    }
    assert statement_settings(chained, settings("on", "on")) == {}


def test_sql_literal_renders_datetime_versions_as_todatetime() -> None:
    import datetime as dt

    assert (
        vcr.sql_literal(dt.datetime(2026, 9, 13, 18, 7, 27)) == "toDateTime('2026-09-13 18:07:27')"
    )
    assert vcr.sql_literal("abc'd") == "'abc\\'d'"  # single-quoted with the quote escaped
    assert vcr.sql_literal(42) == "42"


def test_rows_settled_tolerates_half_a_percent_only() -> None:
    # A fresh SharedMergeTree twin can read a few percent fewer rows under FINAL seconds
    # after the copy than the source it matches later; the runner retries the count
    # instead of failing on the first reading.
    assert vcr.rows_settled(1_747_962, 1_747_962)
    assert vcr.rows_settled(1_740_000, 1_747_962)
    assert not vcr.rows_settled(1_656_313, 1_747_962)
    assert vcr.COUNT_SETTLE_ATTEMPTS * vcr.COUNT_SETTLE_SLEEP_S >= 60


def test_rewrite_ddl_method_flag_names_the_quantization(monkeypatch) -> None:
    # `--method turboquant` builds the twin with two-bit codes; the default
    # stays rabitq so every existing invocation renders byte-for-byte as before.
    default = vcr.rewrite_ddl(
        DDL, "loom.memory_chunks", "loom.memory_chunks_q", ["embedding"], None, dim=3072
    )
    explicit = vcr.rewrite_ddl(
        DDL, "loom.memory_chunks", "loom.memory_chunks_q", ["embedding"], None, "rabitq", dim=3072
    )
    assert default == explicit
    out = vcr.rewrite_ddl(
        DDL,
        "loom.memory_chunks",
        "loom.memory_chunks_q",
        ["embedding"],
        None,
        "turboquant",
        dim=3072,
    )
    assert "`embedding` Array(BFloat16) CODEC(Quantized('turboquant', 3072, 0))," in out
    assert "rabitq" not in out
    chained = vcr.rewrite_ddl(
        DDL, "loom.memory_chunks", "loom.memory_chunks_q", ["embedding"], "LZ4", "int8", dim=3072
    )
    assert "CODEC(Quantized('int8', 3072, 0), LZ4)," in chained
    assert vcr.codec_method(" TurboQuant ") == "turboquant"


def test_rewrite_ddl_refuses_an_unmeasured_method() -> None:
    import pytest

    # 26.8.1.187 knows rabitq / turboquant / int8 at this arity; `mrl` and `pq` (the release-note
    # names) are "Unknown quantization method" (code 117) and would fail the CREATE anyway.
    for bad in ("mrl", "pq", "bogus", "prefix", "product"):
        with pytest.raises(SystemExit):
            vcr.rewrite_ddl(
                DDL,
                "loom.memory_chunks",
                "loom.memory_chunks_q",
                ["embedding"],
                None,
                bad,
                dim=3072,
            )
    assert vcr.CODEC_METHODS == ("rabitq", "turboquant", "int8")


ME_DDL = """CREATE TABLE loom.memory_embeddings
(
    `org` LowCardinality(String),
    `embedding` Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0)),
    `memory_key_embedding` Array(BFloat16) DEFAULT [] CODEC(Quantized('rabitq', 3072, 0), LZ4),
    `embedding_dim` UInt16 DEFAULT 0
)
ENGINE = ReplacingMergeTree(version)
ORDER BY (org, namespace)
SETTINGS index_granularity = 8192"""


def test_rewrite_ddl_recodes_an_already_coded_column_exactly() -> None:
    # A nested CODEC(Quantized(...)) clause is replaced whole: the method change
    # and the patch-free rebuild of a coded table both start from one.
    out = vcr.rewrite_ddl(
        ME_DDL,
        "loom.memory_embeddings",
        "loom.memory_embeddings_q",
        ["embedding", "memory_key_embedding"],
        None,
        "turboquant",
        dim=3072,
    )
    assert "`embedding` Array(BFloat16) CODEC(Quantized('turboquant', 3072, 0)),\n" in out
    assert (
        "`memory_key_embedding` Array(BFloat16) DEFAULT [] CODEC(Quantized('turboquant', 3072, 0)),\n"
        in out
    )
    assert "rabitq" not in out and "LZ4" not in out and ")))" not in out


def test_twin_read_is_the_codes_at_the_leg_multiplier() -> None:
    dist, st = vcr.twin_read("embedding", "[1]", 10.0)
    assert dist == "cosineDistance(embedding, [1])"
    assert st == {
        "vector_search_use_quantized_codes": 1,
        "vector_search_index_fetch_multiplier": 10.0,
    }
    assert vcr.vector_filter("embedding", dim=3072) == (
        "length(embedding) = 3072 AND embedding[1] != 0"
    )


def test_the_width_preflight_counts_other_non_empty_widths_under_final() -> None:
    assert vcr.read_final(vcr.parse_engine(DDL)[0]) == " FINAL"
    assert vcr.read_final("SharedMergeTree") == ""
    assert vcr.mixed_width_sql("memory_chunks", "embedding", " FINAL", dim=3072) == (
        "SELECT length(embedding) AS w, count() FROM loom.memory_chunks FINAL "
        "WHERE length(embedding) NOT IN (0, 3072) GROUP BY w ORDER BY w"
    )
    assert "FINAL" not in vcr.mixed_width_sql("chunks", "embedding", "", dim=3072)


def test_the_twin_only_read_keeps_the_twin_keys_the_source_lacks() -> None:
    assert vcr.twin_only_keys_sql("t_rebuild", "t", ["org", "namespace"], " FINAL") == (
        "SELECT org, namespace FROM (SELECT org, namespace FROM loom.t_rebuild FINAL) AS t "
        "LEFT JOIN (SELECT org, namespace, 1 AS __present FROM loom.t FINAL) AS s "
        "USING (org, namespace) WHERE s.__present = 0"
    )
    erased = vcr.twin_only_keys_sql("t", "t_pre_rebuild", ["org"], "", "version < 7")
    assert erased.startswith("SELECT org FROM (SELECT org FROM loom.t WHERE version < 7) AS t ")
    assert vcr.key_literal(b"\x3f\xa9") == "unhex('3fa9')"


def test_the_post_swap_pass_reads_only_parts_written_before_the_swap() -> None:
    assert vcr.in_parts_before([("all", 7), ("202610", 2)]) == (
        "((_partition_id = 'all' AND toUInt64(splitByChar('_', _part)[3]) <= 7) OR "
        "(_partition_id = '202610' AND toUInt64(splitByChar('_', _part)[3]) <= 2))"
    )
    assert vcr.in_parts_before([]) == "(0)"  # an empty twin had no rows to bring back


def test_the_statement_timeout_is_an_argument_above_zero() -> None:
    base = ["--table", "memory_chunks", "--columns", "embedding"]
    assert vcr.parse(base).statement_timeout_s == 1800.0
    assert vcr.parse([*base, "--statement-timeout-s", "7200"]).statement_timeout_s == 7200.0
    with pytest.raises(SystemExit):
        vcr.parse([*base, "--statement-timeout-s", "0"])


def test_parse_engine_reads_the_version_of_a_non_replicated_or_is_deleted_engine() -> None:
    shared = (
        "SharedReplacingMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}', created_at)"
    )
    oss = DDL.replace(shared, "ReplacingMergeTree(created_at)")
    assert vcr.parse_engine(oss)[:2] == ("ReplacingMergeTree", "created_at")
    deleted = DDL.replace("'{replica}', created_at)", "'{replica}', version, is_deleted)")
    assert vcr.parse_engine(deleted)[1] == "version"
    assert vcr.parse_engine(DDL.replace(shared, "ReplacingMergeTree()"))[1] is None


def test_a_coded_live_table_is_on_target_and_a_chained_ask_is_not() -> None:
    cols = ["embedding", "memory_key_embedding"]
    coded = vcr.rewrite_ddl(
        ME_DDL, "loom.memory_embeddings", "loom.memory_embeddings", cols, None, dim=3072
    )
    assert vcr.on_target(coded, cols, None, "rabitq", 3072)
    assert vcr.on_target(ME_DDL, cols, "LZ4", "rabitq", 3072) is False
