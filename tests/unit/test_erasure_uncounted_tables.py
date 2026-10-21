"""The saga steps for the tables the census did not count.
Statement shapes through a stub client — keyed on (org, namespace) with the
list form under a namespace_list saga, the user axis where the table has
one, no FINAL on a plain MergeTree. CH-free, except the learnings queue's step, whose
races run on clickhouse-local."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server.erasure.app import saga_steps
from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_ledgers as ledger_steps
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


class _StubCH:
    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.counts: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.counts.append((sql, params or {}))
        # count=0 also stands for "no Keeper": the claims table is absent.
        return {"n": self.count, "engine": "KeeperMap" if self.count else ""}

    async def execute(self, sql, params=None):
        self.commands.append((sql, params or {}))

    async def query_all(self, sql, params=None):
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]


def _tables(ch: _StubCH) -> list[str]:
    return [
        sql.split("ALTER TABLE")[1].split()[0] for sql, _ in ch.commands if "ALTER TABLE" in sql
    ]


def test_conversations_step_tombstones_three_tables() -> None:
    ch = _StubCH()
    n = asyncio.run(ledger_steps.tombstone_conversations_by_namespace("o", ["a", "b"], ch=ch))
    assert n == 3
    assert _tables(ch) == [
        "loom.conversations",
        "loom.conversation_threads",
        "loom.conversation_embeddings",
    ]
    by_table = {sql.split("FROM ")[1].split()[0]: sql for sql, _ in ch.counts}
    assert " FINAL" not in by_table["loom.conversations"]  # plain MergeTree: FINAL is code 181
    assert " FINAL" in by_table["loom.conversation_threads"]
    for sql, params in ch.commands:
        assert "namespace IN {ns:Array(String)}" in sql and params["ns"] == ["a", "b"]
    assert all("UPDATE tombstoned_at" in sql for sql, _ in ch.commands)


def test_conversations_user_scope_uses_the_user_column() -> None:
    ch = _StubCH()
    asyncio.run(ledger_steps.tombstone_conversations_by_user("o", "alice", ch=ch))
    assert len(ch.commands) == 3
    for sql, params in ch.commands:
        assert "user_id = {uid:String}" in sql and params["uid"] == "alice"


@pytest.mark.parametrize(
    ("fn", "tables"),
    [
        (ledger_steps.delete_search_trace_log_by_namespace, ["loom.search_trace_log"]),
        (ledger_steps.delete_memory_exposures_by_namespace, ["loom.memory_exposures"]),
        (
            ledger_steps.delete_code_search_trace_by_namespace,
            ["loom.code_search_trace", "loom.code_search_outcome"],
        ),
        (ledger_steps.delete_events_by_namespace, ["loom.events"]),
        (ledger_steps.delete_memory_drops_by_namespace, ["loom.memory_drops"]),
        (
            ledger_steps.delete_learnings_queue_by_namespace,
            ["loom.learnings_supporters", "loom.learnings_queue"],
        ),
        (ledger_steps.delete_consolidation_runs_by_namespace, ["loom.consolidation_runs"]),
        (ledger_steps.delete_projection_failures_by_namespace, ["loom.memory_projection_failures"]),
    ],
)
def test_delete_steps_are_keyed_alter_deletes_over_the_namespace_list(fn, tables) -> None:
    ch = _StubCH(count=3)
    n = asyncio.run(fn("o", ["a", "b"], ch=ch))
    assert n == 3 * len(tables)
    assert _tables(ch) == tables
    for sql, params in ch.commands:
        assert "DELETE" in sql and "UPDATE" not in sql
        assert "(org = {org:String} AND namespace IN {ns:Array(String)})" in sql
        assert params["ns"] == ["a", "b"]
    for sql, _ in ch.counts:
        assert " FINAL" not in sql  # none of these carries a live predicate


_CANDIDATE, _OTHER = "00000000-0000-0000-0000-0000000000a1", "00000000-0000-0000-0000-0000000000b2"


def _candidate(cid: str, status: str, value: str, version: int) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "candidate_id": cid,
        "memory_key": "rule",
        "memory_type": "fact",
        "proposed_value": value,
        "supporter_count": 1,
        "confidence_avg": 1.0,
        "supporters": ["alice"],
        "status": status,
        "version": version,
        "source_kind": "assistant_turn",
    }


def _supporter(cid: str, user_id: str) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "agent": "bot",
        "candidate_id": cid,
        "user_id": user_id,
        "session_id": "s",
        "confidence": 1.0,
        "asked": f"{user_id} asked",
        "said": "the rule",
    }


class _LocalCH:
    """A client over one clickhouse-local database kept in `path`: the step's statements
    run on real tables, in order. `late` rows ({table: rows}) land after everything the
    step read and wrote and before its first delete of loom.learnings_queue, the window
    a review or a filing can race into."""

    def __init__(self, path, rows: dict[str, list[dict[str, Any]]], late=None) -> None:
        self.path, self.late = path, late or {}
        self.statements: list[str] = []
        run([ddl("learnings_queue"), ddl("learnings_supporters")], "SELECT 1", path=path)
        for table, table_rows in rows.items():
            self._write(table, table_rows)

    def _write(self, table: str, rows: list[dict[str, Any]]) -> None:
        columns = ", ".join(rows[0])
        run(
            [
                f"INSERT INTO loom.{table} ({columns}) SELECT * FROM format(JSONEachRow, {{rows:String}})"
            ],
            "SELECT 1",
            {"rows": "\n".join(json.dumps(r) for r in rows)},
            path=self.path,
        )

    async def query_one(self, sql, params=None):
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def query_all(self, sql, params=None):
        if "system.mutations" in sql:  # execute ran each mutation to the end
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]
        return run([], sql, params, path=self.path)

    async def execute(self, sql, params=None):
        if "ALTER TABLE loom.learnings_queue" in sql:
            for table, rows in self.late.items():
                self._write(table, rows)
            self.late = {}
        self.statements.append(sql)
        # The step waits for its mutation; a synchronous one is the same wait.
        sync = sql.replace("mutations_sync = 0", "mutations_sync = 2")
        run([sync], "SELECT 1", params, path=self.path)

    async def insert(self, table, rows, settings=None):
        self.statements.append(f"INSERT INTO {table}")
        self._write(table.removeprefix("loom."), rows)

    def queue(self) -> list[tuple[str, str, str, int]]:
        rows = run(
            [],
            "SELECT candidate_id, status, proposed_value, version FROM loom.learnings_queue "
            "ORDER BY candidate_id, version",
            path=self.path,
        )
        return [
            (r["candidate_id"], r["status"], r["proposed_value"], int(r["version"])) for r in rows
        ]

    def supporters(self) -> list[tuple[str, str]]:
        rows = run(
            [],
            "SELECT candidate_id, user_id FROM loom.learnings_supporters ORDER BY user_id",
            path=self.path,
        )
        return [(r["candidate_id"], r["user_id"]) for r in rows]


def _erase_alice(ch: _LocalCH) -> None:
    asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=ch))


_needs_clickhouse = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


@_needs_clickhouse
def test_the_learnings_user_step_deletes_the_users_support_rows_only(tmp_path) -> None:
    """A user's words live on their supporter rows: the step deletes those, and a
    candidate someone else still supports keeps its rule."""
    filed = _candidate(_CANDIDATE, "pending", '"rule"', 1)
    ch = _LocalCH(
        tmp_path,
        {
            "learnings_queue": [filed],
            "learnings_supporters": [
                _supporter(_CANDIDATE, "alice"),
                _supporter(_CANDIDATE, "bob"),
            ],
        },
    )
    _erase_alice(ch)
    assert ch.supporters() == [(_CANDIDATE, "bob")]
    assert ch.queue() == [(_CANDIDATE, "pending", '"rule"', 1)]


@_needs_clickhouse
def test_an_orphaned_candidate_goes_when_pending_and_keeps_its_decision_when_decided(
    tmp_path,
) -> None:
    ch = _LocalCH(
        tmp_path,
        {
            "learnings_queue": [
                _candidate(_CANDIDATE, "pending", '"rule"', 1),
                _candidate(_OTHER, "rejected", '"rule"', 7),
            ],
            "learnings_supporters": [_supporter(_CANDIDATE, "alice"), _supporter(_OTHER, "alice")],
        },
    )
    _erase_alice(ch)
    # The pending one goes whole; the rejected one keeps its decision with the rule blanked.
    assert ch.queue() == [(_OTHER, "rejected", "", 8)]
    assert ch.supporters() == []
    # The blank row is written before the rows holding the rule are deleted.
    deletes = [
        i for i, sql in enumerate(ch.statements) if "ALTER TABLE loom.learnings_queue" in sql
    ]
    assert ch.statements.index("INSERT INTO loom.learnings_queue") < deletes[-1]


@_needs_clickhouse
def test_a_review_between_the_read_and_the_delete_survives_and_the_reopened_step_blanks_it(
    tmp_path,
) -> None:
    """The step read a pending candidate; a review rejected it before the delete.
    The delete spares the review row, which still holds the rule, so the census
    counts it and the reopened step blanks it."""
    ch = _LocalCH(
        tmp_path,
        {"learnings_queue": [_candidate(_CANDIDATE, "pending", '"r"', 1)]},
        late={"learnings_queue": [_candidate(_CANDIDATE, "rejected", '"r"', 9)]},
    )
    _erase_alice(ch)
    assert ch.queue() == [(_CANDIDATE, "rejected", '"r"', 9)]
    _erase_alice(ch)
    assert ch.queue() == [(_CANDIDATE, "rejected", "", 10)]


@_needs_clickhouse
def test_a_candidate_a_supporter_backed_after_the_read_survives_the_pending_delete(
    tmp_path,
) -> None:
    """The step read a pending candidate with no supporter; a filing then wrote its
    supporter row before the delete was submitted. The delete re-checks for
    supporter rows, so the candidate stays."""
    filed = _candidate(_CANDIDATE, "pending", '"r"', 1)
    ch = _LocalCH(
        tmp_path,
        {"learnings_queue": [filed]},
        late={"learnings_supporters": [_supporter(_CANDIDATE, "bob")]},
    )
    _erase_alice(ch)
    assert ch.queue() == [(_CANDIDATE, "pending", '"r"', 1)]


@_needs_clickhouse
def test_a_pending_candidate_nobody_supports_goes(tmp_path) -> None:
    ch = _LocalCH(tmp_path, {"learnings_queue": [_candidate(_CANDIDATE, "pending", '"r"', 1)]})
    _erase_alice(ch)
    assert ch.queue() == []


@_needs_clickhouse
def test_a_review_landing_after_the_blank_write_survives_the_decided_delete(tmp_path) -> None:
    """The step read a rejected candidate and wrote its blank row; a review then
    promoted it before the delete. The delete takes only what was read, so the newer
    review row stays (holding the rule) for the reopened step to blank."""
    ch = _LocalCH(
        tmp_path,
        {"learnings_queue": [_candidate(_CANDIDATE, "rejected", '"r"', 7)]},
        late={"learnings_queue": [_candidate(_CANDIDATE, "promoted", '"r"', 20)]},
    )
    _erase_alice(ch)
    assert ch.queue() == [(_CANDIDATE, "rejected", "", 8), (_CANDIDATE, "promoted", '"r"', 20)]


@_needs_clickhouse
def test_a_late_review_on_the_older_of_two_decided_candidates_survives(tmp_path) -> None:
    """The step read candidate a at version 200 and b at version 100; b was then
    re-reviewed at version 150 (a replica's clock behind). b's bound is its own read
    version, not a's, so the review row stays for the reopened step to blank."""
    a, b = _CANDIDATE, _OTHER
    ch = _LocalCH(
        tmp_path,
        {
            "learnings_queue": [
                _candidate(a, "rejected", '"r"', 200),
                _candidate(b, "rejected", '"s"', 100),
            ]
        },
        late={"learnings_queue": [_candidate(b, "promoted", '"s"', 150)]},
    )
    _erase_alice(ch)
    assert ch.queue() == [
        (a, "rejected", "", 201),
        (b, "rejected", "", 101),
        (b, "promoted", '"s"', 150),
    ]


def test_the_agent_runs_step_drops_the_runs_claims_too() -> None:
    ch = _StubCH(count=3)
    assert asyncio.run(ledger_steps.delete_agent_runs_by_namespace("o", ["a", "b"], ch=ch)) == 3
    assert _tables(ch) == ["loom.agent_run_claims", "loom.agent_runs"]
    assert "namespace IN {ns:Array(String)}" in ch.commands[0][0]
    assert ch.commands[0][1] == {"org": "o", "ns": ["a", "b"]}
    ch = _StubCH()
    asyncio.run(ledger_steps.delete_agent_runs_by_namespace("o", "a", ch=ch))
    assert "namespace IN {ns:Array(String)}" in ch.commands[0][0]
    assert ch.commands[0][1] == {"org": "o", "ns": ["a"]}
    ch = _StubCH()
    asyncio.run(ledger_steps.delete_agent_runs_by_user("o", "alice", ch=ch))
    assert _tables(ch) == ["loom.agent_run_claims", "loom.agent_runs"]
    assert all("user_id = {uid:String}" in sql for sql, _ in ch.commands)


def test_curation_step_tombstones_three_tables() -> None:
    ch = _StubCH()
    asyncio.run(ledger_steps.tombstone_curation_by_namespace("o", "ns", ch=ch))
    assert _tables(ch) == [
        "loom.operator_edges",
        "loom.conflict_allowlist",
        "loom.memory_categories",
    ]
    assert all("UPDATE tombstoned_at" in sql for sql, _ in ch.commands)


def test_context_blobs_are_tombstoned_in_both_scopes() -> None:
    ch = _StubCH()
    asyncio.run(ledger_steps.tombstone_context_blobs_by_namespace("o", "ns", ch=ch))
    asyncio.run(ledger_steps.tombstone_context_blobs_by_user("o", "alice", ch=ch))
    assert _tables(ch) == ["loom.context_blobs", "loom.context_blobs"]
    assert (
        "namespace = {ns:String}" in ch.commands[0][0]
        and "user_id = {uid:String}" in ch.commands[1][0]
    )


def test_user_scope_delete_steps_skip_the_tables_without_a_user_axis() -> None:
    ch = _StubCH()
    asyncio.run(ledger_steps.delete_code_search_trace_by_user("o", "alice", ch=ch))
    assert _tables(ch) == ["loom.code_search_trace"]  # code_search_outcome has no user_id
    for fn in (
        ledger_steps.delete_memory_exposures_by_user,
        ledger_steps.delete_events_by_user,
        ledger_steps.delete_consolidation_runs_by_user,
    ):
        ch = _StubCH()
        asyncio.run(fn("o", "alice", ch=ch))
        assert len(ch.commands) == 1 and "user_id = {uid:String}" in ch.commands[0][0]


def test_a_purge_turns_the_conversation_tombstones_into_deletes() -> None:
    ch = _StubCH()
    with erasure_mutations.purge_scope(True):
        asyncio.run(ledger_steps.tombstone_conversations_by_namespace("o", ["a"], ch=ch))
    assert all("DELETE" in sql and "tombstoned_at IS NULL" not in sql for sql, _ in ch.commands)
    # conversation_embeddings is on the lightweight list: DELETE FROM, not ALTER
    lw = [sql for sql, _ in ch.commands if sql.lstrip().startswith("DELETE FROM")]
    assert len(lw) == 1 and "loom.conversation_embeddings" in lw[0]


def test_the_sagas_run_every_uncounted_step_in_the_scope_that_owns_it() -> None:
    ns_steps = {name for name, _ in saga_steps._UNCOUNTED_STEPS_NS}
    user_steps = {name for name, _ in saga_steps._UNCOUNTED_STEPS_USER}
    owners_ns = set(erasure_census.step_owners("namespace"))
    owners_user = set(erasure_census.step_owners("user_id"))
    assert ns_steps <= owners_ns, ns_steps - owners_ns
    assert user_steps <= owners_user, user_steps - owners_user
    assert user_steps <= ns_steps
    # every census row is owned by a saga step in the scope it is counted in
    for sub in erasure_census._CENSUS:
        if sub.ns_step in ns_steps:
            assert sub.table in erasure_census.census_tables("namespace")
            assert (sub.user_step is not None) == (
                sub.table in erasure_census.census_tables("user_id")
            )
