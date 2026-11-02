"""The anchor pool's and digest descent's statements, run on clickhouse-local against the
real `loom.memory_embeddings`: rows that hold one value tie on distance, and are taken in row
order however the parts are read."""

from __future__ import annotations

import pytest

from server.memory.app.search.sql import anchor_pool_sql, digest_descent_sql
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")

_VEC = "[" + ",".join(["0.1"] * 3072) + "]"


def _row(user: str, session: str, key: str = "user_city", scope: str = "private") -> str:
    return (
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id,"
        " memory_key, memory_type, scope, content_excerpt, embedding, memory_key_embedding,"
        " embedding_model, embedding_dim, version) VALUES ('o', 'n', 'a',"
        f" '{user}', '{session}', '{key}', 'semantic', '{scope}', 'Lives in Berlin', {_VEC},"
        f" {_VEC}, 'm', 3072, 1)"
    )


def _setup(*rows: str) -> list[str]:
    return ["SET enable_quantized_codec = 1", ddl("memory_embeddings"), *rows]


def test_the_anchor_pool_takes_tied_rows_in_row_order() -> None:
    rows = [_row("u", s) for s in ("s4", "s2", "s3", "s1")]
    got = run(
        _setup(*rows, _row("u", "s1", "user_location")),
        anchor_pool_sql("e.org = {org:String}"),
        {"org": "o", "qv": [0.1] * 3072, "limit": 3},
    )
    assert [(r["session_id"], r["memory_key"]) for r in got] == [
        ("s1", "user_city"),
        ("s1", "user_location"),
        ("s2", "user_city"),
    ]


def test_digest_descent_takes_tied_rows_in_row_order() -> None:
    # a shared row sorts after a private one in the table's key, so FINAL reads
    # `user_location` first; only the memory_key tiebreak puts `user_city` first
    rows = [_row("u1", "s1", "user_city", scope="shared"), _row("u1", "s1", "user_location")]
    got = run(
        _setup(*rows),
        digest_descent_sql("org = {org:String}"),
        {"org": "o", "qv": [0.1] * 3072, "k": 2},
    )
    assert [r["memory_key"] for r in got] == ["user_city", "user_location"]
