"""Private snapshots/backups cannot be omitted from verified erasure."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.erasure.store.provider_copies import copy_residue


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["source", "target", "publish", "backup"])
async def test_copies_count_all_physical_rows_in_scope(role):
    name = f"_provider_rebuild_{uuid4().hex}_memory_embeddings_{role}"
    ch = SimpleNamespace(
        query_all=AsyncMock(
            return_value=[{"table": name, "columns": ["org", "namespace", "user_id"]}]
        ),
        query_one=AsyncMock(return_value={"n": 2}),
    )
    assert await copy_residue("o", "namespace", ["n1", "n2"], ch=ch) == {f"loom.{name}": 2}
    sql, params = ch.query_one.call_args.args
    assert " FINAL" not in sql and "tombstoned_at" not in sql
    assert params == {"org": "o", "namespaces": ["n1", "n2"]}


@pytest.mark.asyncio
async def test_unscoped_copy_blocks_any_subject_conservatively():
    name = f"_provider_rebuild_{uuid4().hex}_embedding_cache_source"
    ch = SimpleNamespace(
        query_all=AsyncMock(return_value=[{"table": name, "columns": ["embedding"]}]),
        query_one=AsyncMock(return_value={"n": 1}),
    )
    assert await copy_residue("o", "user_id", "u", ch=ch) == {f"loom.{name}": 1}
    assert ch.query_one.call_args.args == (f"SELECT count() AS n FROM loom.`{name}` WHERE 1", {})


@pytest.mark.asyncio
async def test_unknown_copy_or_missing_count_is_not_zero():
    ch = SimpleNamespace(
        query_all=AsyncMock(return_value=[{"table": "_provider_rebuild_unknown", "columns": []}]),
        query_one=AsyncMock(return_value=None),
    )
    with pytest.raises(ValueError, match="Unknown rebuild"):
        await copy_residue("o", "namespace", "n", ch=ch)
    name = f"_provider_rebuild_{uuid4().hex}_memory_embeddings_source"
    ch.query_all.return_value = [{"table": name, "columns": ["org", "user_id"]}]
    with pytest.raises(ValueError, match="Cannot establish"):
        await copy_residue("o", "user_id", "u", ch=ch)


@pytest.mark.asyncio
async def test_a_code_answer_copy_counts_the_answers_that_quoted_the_namespace():
    name = f"_provider_rebuild_{uuid4().hex}_code_answers_source"
    ch = SimpleNamespace(
        query_all=AsyncMock(
            return_value=[{"table": name, "columns": ["org", "namespace", "memory_namespace"]}]
        ),
        query_one=AsyncMock(return_value={"n": 1}),
    )
    assert await copy_residue("o", "namespace", "decisions", ch=ch) == {f"loom.{name}": 1}
    sql, params = ch.query_one.call_args.args
    assert sql == (
        f"SELECT count() AS n FROM loom.`{name}` WHERE org = {{org:String}} AND "
        "(namespace IN {namespaces:Array(String)} "
        "OR memory_namespace IN {namespaces:Array(String)})"
    )
    assert params == {"org": "o", "namespaces": ["decisions"]}
