"""`loom reembed --in-database <named collection>` re-embeds
inside ClickHouse with aiEmbed. The statement is pure and testable; the
embed-text switches gate it; the settings size the AI quotas to the rows."""

from __future__ import annotations

import pytest

from server.memory.app import reembed as rb

COLS = [
    "org",
    "namespace",
    "agent",
    "user_id",
    "session_id",
    "scope",
    "embedding_model",
    "memory_key",
    "content_excerpt",
    "embedding",
    "memory_key_embedding",
    "embedding_dim",
    "memory_key_embedding_dim",
    "version",
    "tombstoned_at",
    "created_at",
]


def test_statement_rewrites_vectors_model_dim_and_version_without_aliases() -> None:
    sql = rb.in_database_sql(
        "memory_embeddings",
        COLS,
        model="text-embedding-3-large",
        dim=3072,
        credentials="openai_embed",
        where_sql="embedding_model != 'text-embedding-3-large' AND org = {org:String}",
    )
    assert sql.startswith("INSERT INTO loom.memory_embeddings (org, namespace, agent")
    assert (
        "CAST(aiEmbed(content_excerpt, 'text-embedding-3-large', map('credentials', 'openai_embed')) AS Array(BFloat16))"
        in sql
    )
    assert (
        "CAST(aiEmbed(memory_key, 'text-embedding-3-large', map('credentials', 'openai_embed')) AS Array(BFloat16))"
        in sql
    )
    assert "'text-embedding-3-large'," in sql and "toUInt16(3072)" in sql
    assert "version + 1" in sql and "now64" not in sql
    assert (
        "FROM loom.memory_embeddings FINAL\nWHERE embedding_model != 'text-embedding-3-large' AND org = {org:String}"
        in sql
    )
    # No `AS embedding_model` alias: it would shadow the column in WHERE and
    # select nothing (seen on 26.8.2.7).
    assert " AS embedding_model" not in sql
    assert " AS embedding" not in sql
    # Column list and select list have the same arity.
    cols_part = sql.split("(", 1)[1].split(")", 1)[0]
    select_part = sql.split("SELECT ", 1)[1].split("\nFROM", 1)[0]
    depth = 0
    n = 1
    for ch in select_part:
        depth += ch == "("
        depth -= ch == ")"
        n += ch == "," and depth == 0
    assert n == len(cols_part.split(","))


def test_created_at_versioned_table_drops_created_at_so_default_now_wins() -> None:
    sql = rb.in_database_sql(
        "entity_vectors",
        [
            "org",
            "namespace",
            "entity_text",
            "entity_embedding",
            "embedding_model",
            "embedding_dim",
            "created_at",
        ],
        model="m",
        dim=3072,
        credentials="c",
        where_sql="1",
    )
    assert "created_at" not in sql.split("SELECT", 1)[0]
    assert "aiEmbed(entity_text, 'm'" in sql


def test_settings_scale_quotas_with_rows() -> None:
    s = rb.in_database_settings(5)
    assert s["short_circuit_function_evaluation"] == "force_enable"
    assert s["allow_experimental_ai_functions"] == 1
    assert s["ai_function_embedding_max_batch_size"] == 100
    assert s["ai_function_max_api_calls_per_query"] == 1000
    big = rb.in_database_settings(500_000)
    assert big["ai_function_max_api_calls_per_query"] == 500_000 // 100 * 2 + 100
    assert big["ai_function_max_input_tokens_per_query"] == 500_000 * 2 * 600
    assert s["wait_for_async_insert"] == 1


def test_shortfall_is_min_of_live_and_limit_minus_returned() -> None:
    from bench.vector_index_health import shortfall

    assert shortfall(live_rows=1000, limit=60, returned=60) == 0
    assert shortfall(live_rows=1000, limit=60, returned=41) == 19
    assert shortfall(live_rows=12, limit=60, returned=12) == 0
    assert shortfall(live_rows=12, limit=60, returned=7) == 5
    assert shortfall(live_rows=0, limit=60, returned=0) == 0


@pytest.mark.asyncio
async def test_in_database_statement_runs_through_the_client_with_its_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The statement goes through CHClient.execute, which takes the AI
    quota settings as the statement's own."""
    import server.core.ch as ch_mod

    sent: list[dict] = []

    class _Driver:
        async def command(self, sql, parameters=None, settings=None):
            sent.append({"sql": sql, "settings": dict(settings or {})})

    c = ch_mod.CHClient()
    monkeypatch.setattr(type(c), "client", property(lambda self: _Driver()))

    async def _one(sql, params=None):
        return {"n": 7}

    async def _all(sql, params=None):
        return [{"name": n} for n in COLS]

    monkeypatch.setattr(c, "query_one", _one)
    monkeypatch.setattr(c, "query_all", _all)
    monkeypatch.setattr(ch_mod, "guard_pending_schema", lambda *a, **k: _noop())
    n = await rb.reembed_table_in_database(
        c, "memory_embeddings", model="m2", dim=4, credentials="coll", log=lambda *_: None
    )
    assert n == 7 and len(sent) == 1
    got = sent[0]
    assert got["sql"].startswith("INSERT INTO loom.memory_embeddings")
    assert got["settings"]["allow_experimental_ai_functions"] == 1
    assert got["settings"]["ai_function_max_api_calls_per_query"] == 1000


async def _noop() -> None:
    return None


def test_an_import_key_gets_no_key_vector_in_the_database_either() -> None:
    """As `key_axis_text` gives an import key no axis text, the in-database statement
    writes it an empty key vector at key dim 0; every other key is embedded."""
    sql = rb.in_database_sql(
        "memory_embeddings", COLS, model="m", dim=8, credentials="c", where_sql="1"
    )
    is_import = "match(memory_key, '^import:[0-9a-f]{24}$')"
    assert f"if({is_import}, CAST([] AS Array(BFloat16)), CAST(aiEmbed(memory_key, 'm'" in sql
    assert f"if({is_import}, toUInt16(0), toUInt16(8))" in sql
