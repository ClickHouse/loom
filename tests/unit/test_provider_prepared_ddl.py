"""CREATE-only vector widths change without dropping live schema features."""

import pytest

from server.insights.domain.provider_prepared_ddl import prepared_ddl


def source(codec="Quantized('rabitq', 3072, 0)", extra=""):
    return (
        "CREATE TABLE loom.memory_embeddings UUID '11111111-1111-1111-1111-111111111111' "
        "(`memory_key` String DEFAULT 'COMMENT Quantized( and comma,', "
        f"`embedding` Array(BFloat16) CODEC({codec}), "
        "`memory_key_embedding` Array(BFloat16) DEFAULT [] CODEC(NONE), "
        "`version` UInt64, `tombstoned_at` Nullable(DateTime64(6))"
        f"{extra}) ENGINE = ReplacingMergeTree(version) ORDER BY memory_key "
        "SETTINGS index_granularity = 256 COMMENT 'old owner'"
    )


@pytest.mark.parametrize("method", ["rabitq", "turboquant", "int8"])
@pytest.mark.parametrize("chain", ["", ", LZ4", ", ZSTD(3)"])
def test_resizes_only_fixed_arity_codec_and_retains_every_other_clause(method, chain):
    ddl = source(f"Quantized('{method}', 3072, 0){chain}")
    plan = prepared_ddl(ddl, "memory_embeddings", "candidate", "owned", 8)
    expected = (
        ddl.replace(
            "loom.memory_embeddings UUID '11111111-1111-1111-1111-111111111111'", "loom.candidate"
        )
        .replace(f"Quantized('{method}', 3072, 0)", f"Quantized('{method}', 8, 0)")
        .replace("COMMENT 'old owner'", "COMMENT 'owned'")
    )
    assert plan.sql == expected
    assert plan.padded_columns == ("embedding",)
    assert plan.projection(("memory_key", "embedding", "version"), 8) == (
        "`memory_key`, if(empty(`embedding`), arrayResize(`embedding`, 8, 0), `embedding`), `version`"
    )


def test_plain_vectors_and_defaults_are_not_padded_or_rewritten():
    plan = prepared_ddl(source("NONE"), "memory_embeddings", "candidate", "owned", 8)
    assert not plan.padded_columns
    assert "Array(BFloat16) DEFAULT [] CODEC(NONE)" in plan.sql
    assert (
        plan.projection(("embedding", "memory_key_embedding"), 8)
        == "`embedding`, `memory_key_embedding`"
    )


@pytest.mark.parametrize("codec", ["Quantized('prefix', 3072, 0)", "Quantized('rabitq', 3072, 1)"])
def test_unknown_codec_recipe_never_silently_becomes_plain(codec):
    with pytest.raises(ValueError, match="Unsupported CREATE-only"):
        prepared_ddl(source(codec), "memory_embeddings", "candidate", "owned", 8)


@pytest.mark.parametrize(
    "extra",
    [
        ", CONSTRAINT vector_width CHECK length(embedding) = 3072",
        ", INDEX vector_idx embedding TYPE vector_similarity('hnsw', 'cosineDistance', 3072) GRANULARITY 1",
    ],
)
def test_dimension_bound_operator_layout_needs_an_explicit_plan(extra):
    with pytest.raises(ValueError, match="separate dimension plan"):
        prepared_ddl(source(extra=extra), "memory_embeddings", "candidate", "owned", 8)


@pytest.mark.parametrize("dimensions", [0, -1, True, 65536])
def test_invalid_dimensions_are_rejected(dimensions):
    with pytest.raises(ValueError, match="Invalid prepared vector"):
        prepared_ddl(source(), "memory_embeddings", "candidate", "owned", dimensions)


def test_commentless_canonical_ddl_gets_atomic_ownership_marker():
    plan = prepared_ddl(
        source().removesuffix(" COMMENT 'old owner'"), "memory_embeddings", "candidate", "owned", 8
    )
    assert plan.sql.endswith("SETTINGS index_granularity = 256 COMMENT 'owned'")


def test_nonvector_indexes_are_preserved():
    plan = prepared_ddl(
        source(extra=", INDEX key_idx memory_key TYPE bloom_filter() GRANULARITY 4"),
        "memory_embeddings",
        "candidate",
        "owned",
        8,
    )
    assert "INDEX key_idx memory_key TYPE bloom_filter() GRANULARITY 4" in plan.sql


@pytest.mark.parametrize("method", ["rabitq", "turboquant"])
def test_bit_codecs_reject_unsupported_width_before_creating_a_copy(method):
    with pytest.raises(ValueError, match="multiple of eight"):
        prepared_ddl(
            source(f"Quantized('{method}', 3072, 0)"), "memory_embeddings", "candidate", "owned", 12
        )
