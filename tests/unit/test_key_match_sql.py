"""The key-match arm's statement, run on clickhouse-local against the real
`loom.memory_embeddings`: at most `per_key` rows of a key, in one order however the parts
are read."""

from __future__ import annotations

import pytest

from server.memory.app.search.sql import key_match_sql
from tests.unit.clickhouse_local import CLICKHOUSE, VECTOR, ddl, run

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _row(session: str, key: str = "user_task_worktree", scope: str = "private") -> str:
    return (
        "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id,"
        " memory_key, memory_type, scope, content_excerpt, embedding, memory_key_embedding,"
        " embedding_model, embedding_dim, version) VALUES ('o', 'n', 'a', 'u',"
        f" '{session}', '{key}', 'semantic', '{scope}', 'the worktree', {VECTOR},"
        f" {VECTOR}, 'm', 3072, 1)"
    )


_SQL = key_match_sql("e.org = {org:String} AND e.tombstoned_at IS NULL")


def _params(**kw: int) -> dict[str, object]:
    return {
        "org": "o",
        "ns": "n",
        "stems": ["user", "task", "worktre"],
        "qv": [0.1] * 3072,
        "min_strength": 0.0,
        **kw,
    }


def _setup(*rows: str) -> list[str]:
    return ["SET enable_quantized_codec = 1", ddl("memory_embeddings"), *rows]


def test_rows_of_one_key_that_tie_keep_one_order() -> None:
    # one value in four sessions: the same strength and the same vector
    setup = _setup(*(_row(s) for s in ("s4", "s2", "s3", "s1")))
    rows = run(setup, _SQL, _params(per_key=3, limit=10))
    assert [r["session_id"] for r in rows] == ["s1", "s2", "s3"]


def test_two_keys_that_tie_keep_one_order_at_the_limit() -> None:
    # a shared row sorts after a private one in the table's key, so FINAL reads
    # `worktree_task_user` first; only the memory_key tiebreak puts `user_task_worktree` first
    setup = _setup(
        _row("s1", "user_task_worktree", scope="shared"), _row("s1", "worktree_task_user")
    )
    rows = run(setup, _SQL, _params(per_key=3, limit=1))
    assert [r["memory_key"] for r in rows] == ["user_task_worktree"]
