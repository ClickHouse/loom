"""The rebuild runner's patch-free gate and migration preflight."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.ops import vector_codec_rebuild as R  # noqa: E402


def test_patch_parts_statement_counts_active_patch_parts_of_the_named_table() -> None:
    sql = R.patch_parts_sql("loom", "memory_embeddings_q")
    assert sql == (
        "SELECT countIf(startsWith(name, 'patch')) FROM system.parts "
        "WHERE database = 'loom' AND table = 'memory_embeddings_q' AND active"
    )


def test_rewrite_ddl_recodes_an_already_coded_column_idempotently() -> None:
    ddl = (
        "CREATE TABLE loom.memory_embeddings\n(\n"
        "    `embedding` Array(BFloat16) CODEC(Quantized('rabitq', 3072, 0)),\n"
        "    `memory_key_embedding` Array(BFloat16) DEFAULT [] CODEC(Quantized('rabitq', 3072, 0))\n"
        ")\nENGINE = ReplacingMergeTree(version)\nORDER BY (org, namespace)"
    )
    out = R.rewrite_ddl(
        ddl,
        "loom.memory_embeddings",
        "loom.memory_embeddings_q",
        ["embedding", "memory_key_embedding"],
        None,
        dim=3072,
    )
    assert out.startswith("CREATE TABLE loom.memory_embeddings_q")
    assert out.count("CODEC(Quantized('rabitq', 3072, 0))") == 2
