"""The saga steps for the tables the census did not count.
Statement shapes through a stub client — keyed on (org, namespace) with the
list form under a namespace_list saga, the user axis where the table has
one, no FINAL on a plain MergeTree. CH-free."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.erasure.app import saga_steps
from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_ledgers as ledger_steps


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


class _LearningsCH(_StubCH):
    """Answers the orphan read with `ids` and the latest rows with `latest`."""

    def __init__(self, ids: list[str], latest: list[dict[str, Any]], count: int = 1) -> None:
        super().__init__(count)
        self.ids, self.latest = ids, latest
        self.inserted: list[dict[str, Any]] = []

    async def query_all(self, sql, params=None):
        if "SELECT DISTINCT candidate_id" in sql:
            self.counts.append((sql, params or {}))
            return [{"candidate_id": i} for i in self.ids]
        if "FROM loom.learnings_queue FINAL" in sql:
            return [dict(r) for r in self.latest]
        return await super().query_all(sql, params)

    async def insert(self, table, rows, settings=None):
        self.commands.append((f"INSERT INTO {table}", {}))
        self.inserted.extend(rows)


def test_the_learnings_user_step_deletes_the_users_support_rows_only() -> None:
    """A user's words live on their supporter rows: the step deletes those, then the
    candidates left with none, and nothing of anyone else's."""
    ch = _LearningsCH([], [])
    assert asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=ch)) == 1
    assert _tables(ch) == ["loom.learnings_supporters"]
    ((sql, params),) = ch.commands
    assert "user_id = {uid:String}" in sql and params["uid"] == "alice"
    orphans, _ = ch.counts[-1]
    assert "source_kind = 'assistant_turn' AND proposed_value != ''" in orphans
    assert "NOT IN (SELECT candidate_id FROM loom.learnings_supporters" in orphans


def test_an_orphaned_candidate_goes_when_pending_and_keeps_its_decision_when_decided() -> None:
    latest = [
        {"candidate_id": "p", "status": "pending", "proposed_value": '"rule"', "version": 1},
        {"candidate_id": "r", "status": "rejected", "proposed_value": '"rule"', "version": 7},
    ]
    ch = _LearningsCH(["p", "r"], latest)
    asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=ch))
    # The rejected one keeps its decision on a newer row with the rule blanked...
    assert ch.inserted == [
        {"candidate_id": "r", "status": "rejected", "proposed_value": "", "version": 8}
    ]
    deletes = [(sql, params) for sql, params in ch.commands if "ALTER TABLE" in sql]
    supporters, pending, decided = deletes
    assert "loom.learnings_supporters" in supporters[0]
    # ...the pending one goes whole, and only the rejected one's rows holding the rule go.
    assert pending[1]["cids"] == ["p"] and "proposed_value" not in pending[0]
    # Only its pending rows: a review landing in between survives.
    assert "status = 'pending'" in pending[0]
    assert "proposed_value != ''" in decided[0]
    # ...no newer than the version read for it.
    assert "{read:Array(Tuple(UUID, UInt64))}" in decided[0] and decided[1]["read"] == [("r", 7)]
    # The blank row is written before the rows holding the rule are deleted.
    sqls = [sql for sql, _ in ch.commands]
    assert sqls.index("INSERT INTO loom.learnings_queue") < sqls.index(decided[0])


def _deleted(
    row: dict[str, Any], sql: str, params: dict[str, Any], supported: frozenset[str]
) -> bool:
    """Whether one recorded delete of loom.learnings_queue takes `row`, given the
    candidates with a supporter row when it runs: a pending delete only `pending`
    rows of a candidate nobody supports, a decided one only rows holding the rule
    at a version no newer than the one it read for that candidate."""
    if "status = 'pending'" in sql:
        assert "candidate_id NOT IN (SELECT candidate_id FROM loom.learnings_supporters" in sql
        return (
            row["candidate_id"] in params["cids"]
            and row["status"] == "pending"
            and row["candidate_id"] not in supported
        )
    read = dict(params["read"])
    return (
        bool(row["proposed_value"])
        and row["candidate_id"] in read
        and row["version"] <= read[row["candidate_id"]]
    )


def _survivors(
    rows: list[dict[str, Any]], ch: _LearningsCH, supported: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    """`rows` and the step's inserts, after the deletes it sent."""
    left = [*rows, *ch.inserted]
    for sql, params in ch.commands:
        if "ALTER TABLE loom.learnings_queue" in sql:
            left = [r for r in left if not _deleted(r, sql, params, supported)]
    return left


def test_a_review_between_the_read_and_the_delete_survives_and_the_reopened_step_blanks_it() -> (
    None
):
    """The step read a pending candidate; a review rejected it before the delete.
    The delete spares the review row, which still holds the rule, so the census
    counts it and the reopened step blanks it."""
    filed = {"candidate_id": "c", "status": "pending", "proposed_value": '"r"', "version": 1}
    review = {"candidate_id": "c", "status": "rejected", "proposed_value": '"r"', "version": 9}
    first = _LearningsCH(["c"], [filed])
    asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=first))
    assert _survivors([filed, review], first) == [review]
    reopened = _LearningsCH(["c"], [review])
    asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=reopened))
    blank = {"candidate_id": "c", "status": "rejected", "proposed_value": "", "version": 10}
    assert reopened.inserted == [blank]
    assert _survivors([review], reopened) == [blank]


def test_a_candidate_a_supporter_backed_after_the_read_survives_the_pending_delete() -> None:
    """The step read a pending candidate with no supporter; a filing then wrote its
    supporter row and its version-1 candidate row before the delete was submitted.
    The delete re-checks for supporter rows, so the candidate stays."""
    filed = {"candidate_id": "c", "status": "pending", "proposed_value": '"r"', "version": 1}
    ch = _LearningsCH(["c"], [filed])
    asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=ch))
    # The filer's rows landed before the delete ran: the candidate is supported.
    assert _survivors([filed, dict(filed)], ch, supported=frozenset({"c"})) == [filed, filed]
    # With nobody's support it goes.
    assert _survivors([filed], ch) == []


def test_a_review_landing_after_the_blank_write_survives_the_decided_delete() -> None:
    """The step read a rejected candidate and wrote its blank row; a review then
    promoted it before the delete. The delete takes only what was read, so the newer
    review row stays (holding the rule) for the reopened step to blank."""
    rejected = {"candidate_id": "c", "status": "rejected", "proposed_value": '"r"', "version": 7}
    promoted = {"candidate_id": "c", "status": "promoted", "proposed_value": '"r"', "version": 20}
    ch = _LearningsCH(["c"], [rejected])
    asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=ch))
    blank = {"candidate_id": "c", "status": "rejected", "proposed_value": "", "version": 8}
    assert _survivors([rejected, promoted], ch) == [promoted, blank]


def test_a_late_review_on_the_older_of_two_decided_candidates_survives() -> None:
    """The step read candidate a at version 200 and b at version 100; b was then
    re-reviewed at version 150 (a replica's clock behind). b's bound is its own read
    version, not a's, so the review row stays for the reopened step to blank."""
    a = {"candidate_id": "a", "status": "rejected", "proposed_value": '"r"', "version": 200}
    b = {"candidate_id": "b", "status": "rejected", "proposed_value": '"s"', "version": 100}
    review = {"candidate_id": "b", "status": "promoted", "proposed_value": '"s"', "version": 150}
    ch = _LearningsCH(["a", "b"], [a, b])
    asyncio.run(ledger_steps.delete_learnings_queue_by_user("o", "alice", ch=ch))
    blank_a = {**a, "proposed_value": "", "version": 201}
    blank_b = {**b, "proposed_value": "", "version": 101}
    assert _survivors([a, b, review], ch) == [review, blank_a, blank_b]


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
