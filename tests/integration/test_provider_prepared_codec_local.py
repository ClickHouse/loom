"""Opt-in real fixed-width codec exercise, isolated inside clickhouse-local."""

import json
import os
import shutil
import subprocess
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.domain.provider_prepared_ddl import prepared_ddl
from server.insights.store.provider_prepared import _verify_copy
from server.insights.store.provider_staging import SourceSchema, StagingNames


def local_sql(sql):
    executable = shutil.which("clickhouse")
    if executable is None:
        pytest.skip("clickhouse-local is not installed")
    result = subprocess.run(
        [executable, "local", "--multiquery", "--query", sql],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.parametrize("dimensions", [8, 16])
@pytest.mark.asyncio
async def test_coded_candidate_retains_metadata_and_cleared_vector_placeholders(dimensions):
    if os.environ.get("LOOM_PROVIDER_CODEC_LOCAL_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_CODEC_LOCAL_TEST=1 with a codec-capable clickhouse-local")
    source = (
        "SET allow_experimental_codecs=1; SET allow_suspicious_codecs=1; "
        "CREATE DATABASE loom ENGINE=Atomic; "
        "CREATE TABLE loom.memory_embeddings (memory_key String, embedding Array(BFloat16) "
        "CODEC(Quantized('rabitq',8,0),LZ4), embedding_dim UInt16, version UInt64, "
        "tombstoned_at Nullable(DateTime64(6)), metadata String) "
        "ENGINE=ReplacingMergeTree(version) ORDER BY memory_key COMMENT 'original';"
    )
    ddl = json.loads(
        local_sql(
            source
            + "SELECT create_table_query FROM system.tables WHERE database='loom' AND name='memory_embeddings' FORMAT JSONEachRow;"
        )
    )["create_table_query"]
    names = StagingNames(uuid4(), "memory_embeddings")
    plan = prepared_ddl(ddl, "memory_embeddings", names.prepared, "owned", dimensions)
    columns = ("memory_key", "embedding", "embedding_dim", "version", "tombstoned_at", "metadata")
    projection = plan.projection(columns, dimensions)
    setup = (
        source + plan.sql + "; "
        f"CREATE TABLE loom.{names.target} (memory_key String, embedding Array(BFloat16), "
        "embedding_dim UInt16, version UInt64, tombstoned_at Nullable(DateTime64(6)), "
        "metadata String) ENGINE=ReplacingMergeTree(version) ORDER BY memory_key; "
        f"INSERT INTO loom.{names.target} SELECT 'live', arrayResize(CAST([1] AS Array(BFloat16)),{dimensions},0), {dimensions}, 9, NULL, 'keep-live'; "
        f"INSERT INTO loom.{names.target} VALUES ('erased',[],0,10,'2026-10-05 12:34:56.123456','keep-erased'); "
        f"INSERT INTO loom.{names.prepared} SELECT {projection} FROM loom.{names.target}; "
    )
    result = local_sql(
        setup + "SELECT memory_key, length(embedding) AS width, embedding_dim, version, "
        "toString(tombstoned_at) AS tombstoned_at, metadata, arraySum(embedding) AS sum "
        f"FROM loom.{names.prepared} ORDER BY memory_key FORMAT JSONEachRow;"
    )
    rows = [json.loads(line) for line in result.splitlines()]
    assert rows == [
        {
            "memory_key": "erased",
            "width": dimensions,
            "embedding_dim": 0,
            "version": 10,
            "tombstoned_at": "2026-10-05 12:34:56.123456",
            "metadata": "keep-erased",
            "sum": 0,
        },
        {
            "memory_key": "live",
            "width": dimensions,
            "embedding_dim": dimensions,
            "version": 9,
            "tombstoned_at": None,
            "metadata": "keep-live",
            "sum": 1,
        },
    ]
    schema = SourceSchema(uuid4(), "ReplacingMergeTree", columns, "a" * 64)
    client = AsyncMock()

    async def query(sql):
        return json.loads(local_sql(setup + sql + " FORMAT JSONEachRow;"))

    client.query_one.side_effect = query
    await _verify_copy(names, schema, client, projection=projection)
    # Equal row counts cannot hide a changed metadata field in a coded copy.
    setup += f"ALTER TABLE loom.{names.prepared} UPDATE metadata='corrupt' WHERE memory_key='live' SETTINGS mutations_sync=2; "
    with pytest.raises(ValueError, match="content differs"):
        await _verify_copy(names, schema, client, projection=projection)
