"""A user erasure retires the consolidated rows the user's own keys fed, in the row's
namespace: a summary another namespace built from a key of the same name stays."""

from __future__ import annotations

import pytest

from server.erasure.store.predicates import CONSOLIDATED_IN
from server.erasure.store.scope_reads import consolidated_params
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run

# The user's (namespace, key) pairs, bound as the step binds them.
_U1 = {"org": "o", **consolidated_params([("mine", "prefs/editor")])}
_SUMMARY = "00000000-0000-0000-0000-0000000000c1"


def _row(ns: str, key: str, user: str, tags: list[str], memory_id: str = "") -> str:
    mid = f"'{memory_id}'" if memory_id else "generateUUIDv4()"
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, memory_id, version, tags) VALUES "
        f"('o', '{ns}', 'a', '{user}', '-', '{key}', 'fact', '\"v\"', {mid}, 1, {tags!r})"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_a_summary_is_the_users_only_where_the_user_wrote_its_source() -> None:
    rows = run(
        [
            ddl("memory"),
            _row("mine", "prefs/editor", "u1", []),
            _row("theirs", "prefs/editor", "u2", []),
            _row("mine", "profile", "-", ["consolidated", "src:prefs/editor"]),
            _row("theirs", "profile", "-", ["consolidated", "src:prefs/editor"]),
            _row("mine", "notes", "-", ["src:prefs/editor"]),  # not a summary
        ],
        f"SELECT namespace, memory_key FROM loom.memory WHERE org = {{org:String}} "
        f"AND {CONSOLIDATED_IN} ORDER BY namespace",
        _U1,
    )
    assert rows == [{"namespace": "mine", "memory_key": "profile"}]


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
def test_the_step_retires_the_pair_as_a_mutation() -> None:
    """The step runs the predicate in an ALTER UPDATE, every value bound."""
    rows = run(
        [
            ddl("memory"),
            _row("mine", "prefs/editor", "u1", []),
            _row("mine", "profile", "-", ["consolidated", "src:prefs/editor"]),
            _row("theirs", "profile", "-", ["consolidated", "src:prefs/editor"]),
            "ALTER TABLE loom.memory UPDATE tombstoned_at = now() WHERE org = {org:String} "
            f"AND {CONSOLIDATED_IN} SETTINGS mutations_sync = 2",
        ],
        "SELECT namespace FROM loom.memory WHERE memory_key = 'profile' "
        "AND tombstoned_at IS NOT NULL",
        _U1,
    )
    assert rows == [{"namespace": "mine"}]


class _Recording:
    """Answers the step's reads (the user's keys, the summaries they fed) and counts with
    one row, and keeps the statements it then runs."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, dict[str, object]]] = []

    async def query_one(self, sql: str, params: dict[str, object] | None = None) -> dict:
        return {"n": 1}

    async def query_all(self, sql: str, params: dict[str, object] | None = None) -> list:
        if "namespace, memory_key FROM" in sql:
            return [{"namespace": "mine", "memory_key": "prefs/editor"}]
        if "toString(memory_id) AS id" in sql:
            return [{"namespace": "mine", "id": _SUMMARY}]
        # system.mutations poll: the mutation is done.
        return [{"mutation_id": "m.txt", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    async def execute(self, sql: str, params: dict[str, object] | None = None) -> None:
        self.statements.append((sql, dict(params or {})))


@pytest.mark.skipif(CLICKHOUSE is None, reason="needs the clickhouse binary")
@pytest.mark.asyncio
async def test_the_steps_own_statements_retire_the_users_summary_and_its_links() -> None:
    """The statements `tombstone_consolidated_dependents_by_user` issues, run as they are
    on clickhouse-local: the user's summary and its code link go, the other namespace's
    stay."""
    from server.erasure.store.steps_graph import tombstone_consolidated_dependents_by_user

    ch = _Recording()
    await tombstone_consolidated_dependents_by_user("o", "u1", ch=ch)  # type: ignore[arg-type]
    assert len(ch.statements) == 3
    params: dict[str, object] = {}
    for _, p in ch.statements:
        params.update(p)
    links = (
        "INSERT INTO loom.memory_code_links (org, code_namespace, path, qualified_name, "
        "namespace, memory_id, memory_key, state) "
        "SELECT org, 'code', 'p', 'q', namespace, memory_id, memory_key, 'fresh' "
        "FROM loom.memory WHERE memory_key = 'profile'"
    )
    rows = run(
        [
            "SET enable_quantized_codec = 1",
            ddl("memory"),
            ddl("memory_embeddings"),
            ddl("memory_code_links"),
            _row("mine", "prefs/editor", "u1", []),
            _row("mine", "profile", "-", ["consolidated", "src:prefs/editor"], _SUMMARY),
            _row("theirs", "profile", "-", ["consolidated", "src:prefs/editor"]),
            links,
            *(sql.replace("mutations_sync = 0", "mutations_sync = 2") for sql, _ in ch.statements),
        ],
        "SELECT namespace, (SELECT count() FROM loom.memory_code_links l "
        "WHERE l.namespace = m.namespace) AS links FROM loom.memory m "
        "WHERE memory_key = 'profile' AND tombstoned_at IS NULL",
        params,
    )
    assert rows == [{"namespace": "theirs", "links": 1}]
