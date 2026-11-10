"""The key-match arm's statement, run on clickhouse-local against the real
`loom.memory_embeddings`: at most `per_key` rows of a key, in one order however the parts
are read."""

from __future__ import annotations

import pytest

from server.memory.app.search.key_match import key_match_query_stems, key_parts
from server.memory.app.search.sql import (
    KEY_PARTS,
    key_match_component_sql,
    key_match_precheck_sql,
    key_match_sql,
)
from tests.unit.clickhouse_local import CLICKHOUSE, EMBEDDINGS, VECTOR, run

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
    return [*EMBEDDINGS, *rows]


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


# A key with whitespace in a part (`stem()` refuses one) beside one split at `_` alone.
_SPACED = ("communication style", "user_communication_style")


def test_a_key_with_whitespace_is_split_before_it_is_stemmed() -> None:
    setup = _setup(*(_row("s1", key) for key in _SPACED))
    params = {**_params(per_key=3, limit=10), "stems": key_match_query_stems("communication style")}
    assert run(setup, key_match_precheck_sql(), params) == [{"any_key": 1}]
    assert sorted(r["memory_key"] for r in run(setup, _SQL, params)) == sorted(_SPACED)
    component = key_match_component_sql("e.org = {org:String} AND e.tombstoned_at IS NULL")
    rows = run(setup, component, {**params, "pool_keys": list(_SPACED)})
    assert sorted(r["memory_key"] for r in rows) == sorted(_SPACED)


def test_key_parts_in_python_are_the_statements_parts_for_an_ascii_key() -> None:
    keys = [
        "user_task_worktree",
        "a__b",
        "_lead",
        "communication style",
        "x\ty\nz",
        "p/q-r s",
        # each escape in the statement's literal is whitespace, not its letter
        "pref\x0bvim\x0cfoo\rbar_vfr",
    ]
    rows = run(
        [],
        f"SELECT {KEY_PARTS} AS parts FROM (SELECT arrayJoin({{keys:Array(String)}}) AS memory_key)",
        {"keys": keys},
    )
    assert [r["parts"] for r in rows] == [key_parts(k) for k in keys]


_KEYS = (
    "user_task_worktree",
    "worktree_path",
    "user_prs_open_count",
    "pr_review_policy",
    "a__task",
    "_task_lead",
    "Task_Owner",
    "pref/editor_theme",
    "ci-run_flaky_tests",
)


@pytest.mark.parametrize("query", ["task worktree path", "editor theme", "flaky CI tests"])
def test_a_key_without_whitespace_reads_as_it_did_at_the_underscore_split(
    query: str, tmp_path
) -> None:
    """Every statement returns the same rows, in the same order, as it did when keys were
    split at `_` alone: their parts, counts and document frequencies are unchanged."""
    run(_setup(*(_row(f"s{i % 3}", key) for i, key in enumerate(_KEYS))), "SELECT 1", path=tmp_path)
    params = {**_params(per_key=3, limit=10), "stems": key_match_query_stems(query)}
    where = "e.org = {org:String} AND e.tombstoned_at IS NULL"
    statements = [
        (key_match_precheck_sql(), params),
        (_SQL, params),
        (key_match_component_sql(where), {**params, "pool_keys": list(_KEYS)}),
    ]
    for sql, p in statements:
        before = sql.replace(KEY_PARTS, "splitByChar('_', lower(memory_key))")
        assert before != sql
        assert run([], sql, p, path=tmp_path) == run([], before, p, path=tmp_path)
