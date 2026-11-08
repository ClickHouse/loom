"""Unit tests for the erasure store: the mutation runner, the steps and the census."""

from __future__ import annotations

from typing import Any

import pytest

from server.erasure.store import census as erasure_census
from server.erasure.store import ledger as erasure_ledger
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_graph as graph_steps
from server.erasure.store import steps_memory as memory_steps


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        # (needle, rows): the rows a read whose SQL holds the needle gets
        self.reads: list[tuple[str, list[dict[str, Any]]]] = []
        self.read_calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))

    async def query_all(self, sql, params=None):
        if "system.mutations" not in sql:
            self.read_calls.append((sql, params or {}))
            return next((rows for needle, rows in self.reads if needle in sql), [])
        # system.mutations poll: the mutation is done.
        return [
            {
                "mutation_id": "mutation_1.txt",
                "is_done": 1,
                "parts_to_do": 0,
                "latest_fail_reason": "",
            }
        ]

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


@pytest.mark.asyncio
async def test_count_then_mutate_zero_count_short_circuits() -> None:
    """Idempotent re-run: when there's nothing to tombstone, the
    ALTER UPDATE doesn't fire."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 0})
    n = await erasure_mutations.count_then_mutate(
        "loom.memory",
        count_where="org = {org:String}",
        mutation="UPDATE tombstoned_at = {now:DateTime}",
        mutation_where="org = {org:String}",
        params={"org": "clickhouse"},
        ch=ch,
    )
    assert n == 0
    assert ch.execute_calls == []


@pytest.mark.asyncio
async def test_count_then_mutate_runs_alter_when_rows_present() -> None:
    """Happy path: count returns N>0, the mutation runs once with
    mutations_sync=2."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 5})
    n = await erasure_mutations.count_then_mutate(
        "loom.memory",
        count_where="org = {org:String}",
        mutation="UPDATE tombstoned_at = {now:DateTime}",
        mutation_where="org = {org:String}",
        params={"org": "clickhouse"},
        ch=ch,
    )
    assert n == 5
    sql, params = ch.execute_calls[0]
    assert "ALTER TABLE loom.memory" in sql
    assert "mutations_sync = 0" in sql
    assert "now" in params  # auto-populated


@pytest.mark.asyncio
async def test_tombstone_memory_by_user_filters_by_uid() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"n": 3})
    await memory_steps.tombstone_memory_by_user("clickhouse", "alice", ch=ch)
    sel_sql, sel_params = ch.query_one_calls[0]
    assert "FROM loom.memory FINAL" in sel_sql
    assert "user_id = {uid:String}" in sel_sql
    assert sel_params["uid"] == "alice"


@pytest.mark.asyncio
async def test_scrub_events_by_user_no_final_clause() -> None:
    """agent_events is MergeTree (append-only) — must NOT use FINAL on
    the count, that would be a syntax error."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 7})
    await memory_steps.scrub_events_by_user("clickhouse", "alice", ch=ch)
    sel_sql, _ = ch.query_one_calls[0]
    assert "FROM loom.agent_events" in sel_sql
    assert "FINAL" not in sel_sql
    mut_sql, _ = ch.execute_calls[0]
    assert "UPDATE user_id = '<erased>'" in mut_sql


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["namespace", "user"])
async def test_supersede_chunks_no_final(by: str) -> None:
    """chunks is a plain MergeTree — FINAL would be wrong. The repo
    helper threads `final=False` for these cases."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 2})
    if by == "namespace":
        await memory_steps.supersede_chunks_by_namespace("clickhouse", "ns", ch=ch)
    else:
        await memory_steps.supersede_chunks_by_user("clickhouse", "alice", ch=ch)
    sel_sql, _ = ch.query_one_calls[0]
    assert "FROM loom.chunks" in sel_sql
    assert "FROM loom.chunks FINAL" not in sel_sql
    # Every unstamped chunk counts, a retired one too, so a scope whose documents were all
    # deleted still gets its stamps.
    assert "doc_superseded_at IS NULL" in sel_sql and "chunk_supersessions" not in sel_sql


@pytest.mark.asyncio
async def test_tombstone_entity_canonical_map_by_namespace() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"n": 2})

    await memory_steps.tombstone_entity_canonical_map_by_namespace(
        "clickhouse",
        "ns",
        ch=ch,
    )

    sel_sql, sel_params = ch.query_one_calls[0]
    assert "FROM loom.entity_canonical_map FINAL" in sel_sql
    assert "namespace = {ns:String}" in sel_sql
    assert sel_params["ns"] == "ns"
    mut_sql, _ = ch.execute_calls[0]
    assert "ALTER TABLE loom.entity_canonical_map" in mut_sql
    assert "UPDATE tombstoned_at = {now:DateTime}" in mut_sql


@pytest.mark.asyncio
async def test_delete_derived_facts_by_namespace_hard_delete_no_final() -> None:
    """derived_facts is a plain MergeTree with no
    soft-delete column: erasure must be an ALTER DELETE (not a tombstone) and
    must NOT use FINAL on the count (syntax-wasteful on non-Replacing)."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 4})
    await memory_steps.delete_derived_facts_by_namespace("clickhouse", "ns", ch=ch)
    sel_sql, sel_params = ch.query_one_calls[0]
    assert "FROM loom.derived_facts" in sel_sql
    assert "FROM loom.derived_facts FINAL" not in sel_sql
    assert "namespace = {ns:String}" in sel_sql
    assert sel_params["ns"] == "ns"
    mut_sql, _ = ch.execute_calls[0]
    assert "ALTER TABLE loom.derived_facts" in mut_sql
    # hard delete, NOT a tombstone update
    assert "DELETE" in mut_sql
    assert "tombstoned_at" not in mut_sql


@pytest.mark.asyncio
async def test_delete_derived_facts_by_user_filters_by_uid() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"n": 2})
    await memory_steps.delete_derived_facts_by_user("clickhouse", "alice", ch=ch)
    sel_sql, sel_params = ch.query_one_calls[0]
    assert "FROM loom.derived_facts" in sel_sql
    assert "FINAL" not in sel_sql
    assert "user_id = {uid:String}" in sel_sql
    assert sel_params["uid"] == "alice"
    mut_sql, _ = ch.execute_calls[0]
    assert "ALTER TABLE loom.derived_facts" in mut_sql
    assert "DELETE" in mut_sql


@pytest.mark.asyncio
async def test_delete_derived_facts_zero_count_short_circuits() -> None:
    """Idempotent re-run: nothing to delete → no ALTER fires."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 0})
    n = await memory_steps.delete_derived_facts_by_namespace("clickhouse", "ns", ch=ch)
    assert n == 0
    assert ch.execute_calls == []


@pytest.mark.asyncio
async def test_tombstone_working_memory_by_user() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"n": 1})

    await memory_steps.tombstone_working_memory_by_user("clickhouse", "alice", ch=ch)

    sel_sql, sel_params = ch.query_one_calls[0]
    assert "FROM loom.working_memory FINAL" in sel_sql
    assert "user_id = {uid:String}" in sel_sql
    assert sel_params["uid"] == "alice"
    mut_sql, _ = ch.execute_calls[0]
    assert "ALTER TABLE loom.working_memory" in mut_sql
    assert "UPDATE tombstoned_at = {now:DateTime}" in mut_sql


@pytest.mark.asyncio
async def test_tombstone_working_memory_by_namespace() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"n": 1})

    await memory_steps.tombstone_working_memory_by_namespace(
        "clickhouse",
        "ns",
        ch=ch,
    )

    sel_sql, sel_params = ch.query_one_calls[0]
    assert "FROM loom.working_memory FINAL" in sel_sql
    assert "namespace = {ns:String}" in sel_sql
    assert sel_params["ns"] == "ns"
    mut_sql, _ = ch.execute_calls[0]
    assert "ALTER TABLE loom.working_memory" in mut_sql
    assert "UPDATE tombstoned_at = {now:DateTime}" in mut_sql


@pytest.mark.asyncio
async def test_insert_request_checkpoint_writes_to_erasure_requests() -> None:
    ch = StubCH()
    await erasure_ledger.insert_request_checkpoint(
        {"id": "x", "status": "completed"},
        ch=ch,
    )
    table, rows, _ = ch.insert_calls[0]
    assert table == "loom.erasure_requests"
    assert rows[0]["id"] == "x"


# ----------------------------------------------------------------------
# GDPR cascade — shared rows promoted from an erased user
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_shared_by_user_uses_arrayexists_on_promoted_from() -> None:
    """Shared rows store source provenance in promoted_from as
    `org/namespace/agent/user_id/session_id/memory_key` strings. The
    user-erasure cascade must find any shared row whose array contains
    a tuple naming the erased user — checked via arrayExists +
    splitByChar('/', x)[4]."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 2})
    n = await memory_steps.tombstone_shared_by_user("clickhouse", "alice", ch=ch)
    assert n == 2
    sel_sql, sel_params = ch.query_one_calls[0]
    assert "FROM loom.shared FINAL" in sel_sql
    assert "arrayExists" in sel_sql
    assert "splitByChar('/', x)[4]" in sel_sql
    assert sel_params["uid"] == "alice"
    assert sel_params["org"] == "clickhouse"
    # ALTER UPDATE fires
    mut_sql, _ = ch.execute_calls[0]
    assert "ALTER TABLE loom.shared" in mut_sql
    assert "UPDATE tombstoned_at" in mut_sql
    assert "mutations_sync = 0" in mut_sql


@pytest.mark.asyncio
async def test_tombstone_shared_by_user_zero_count_short_circuits() -> None:
    """No matching shared rows → no ALTER fires."""
    ch = StubCH()
    ch.query_one_returns.append({"n": 0})
    n = await memory_steps.tombstone_shared_by_user("clickhouse", "bob", ch=ch)
    assert n == 0
    assert ch.execute_calls == []


@pytest.mark.asyncio
async def test_tombstone_shared_embeddings_by_user_names_each_namespace_with_its_key() -> None:
    """Shared embeddings don't carry user_id directly (sentinel
    agent='_shared'); the cascade reads the (namespace, key) pairs of the shared rows
    promoted from the user, then tombstones the shared embeddings of those pairs
    (the rows on clickhouse-local: test_erasure_shared_embeddings_sql.py)."""
    ch = StubCH()
    ch.reads.append(("FROM loom.shared FINAL", [{"namespace": "team-a", "memory_key": "k1"}]))
    ch.query_one_returns.append({"n": 2})
    await memory_steps.tombstone_shared_embeddings_by_user(
        "clickhouse",
        "alice",
        ch=ch,
    )
    # the summaries of the user's memories first, then the shared rows promoted from either
    assert "has(tags, 'consolidated')" in ch.read_calls[0][0]
    ((read_sql, read_params),) = [c for c in ch.read_calls if "FROM loom.shared FINAL" in c[0]]
    assert "SELECT DISTINCT namespace, memory_key FROM loom.shared FINAL" in read_sql
    assert "splitByChar('/', x)[4]" in read_sql and read_params["uid"] == "alice"
    sel_sql, _ = ch.query_one_calls[0]
    assert "FROM loom.memory_embeddings FINAL" in sel_sql
    assert (
        "scope = 'shared' AND (namespace, memory_key) IN {keys:Array(Tuple(String, String))}"
        in sel_sql
    )
    ((mut_sql, mut_params),) = ch.execute_calls
    assert "SELECT" not in mut_sql and mut_params["keys"] == [("team-a", "k1")]


def _summary_row(memory_id: str) -> dict[str, str]:
    """A summary as `scope_reads.user_summary_rows` reads it."""
    return {
        "ns": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "t",
        "id": memory_id,
    }


@pytest.mark.asyncio
async def test_consolidated_dependents_of_an_erased_user_are_tombstoned_in_both_tables() -> None:
    """A topic memory synthesised from an erased user's rows lives under
    another identity and carries the content verbatim (tag `src:<key>`); the
    per-user tombstone never reaches it. The step retires any summary whose
    sources overlap the erased keys in its own namespace, on loom.memory AND
    loom.memory_embeddings, and deletes its code links (a suggestion rewrites its
    text), every value bound as the replicated tables require."""
    topic = "00000000-0000-0000-0000-0000000000c1"
    ch = StubCH()
    ch.reads.append(("namespace, memory_key FROM", [{"namespace": "ns", "memory_key": "k1"}]))
    ch.reads.append(("toString(memory_id) AS id FROM (SELECT", [_summary_row(topic)]))
    ch.query_one_returns.extend([{"n": 2}, {"n": 2}, {"n": 1}])
    n = await graph_steps.tombstone_consolidated_dependents_by_user("o", "alice", ch=ch)
    assert n == 5
    keys_sql, keys_params = ch.read_calls[0]
    assert "namespace, memory_key FROM" in keys_sql
    assert keys_params == {"org": "o", "uid": "alice"}
    tables = [sql.split("ALTER TABLE ")[1].split()[0] for sql, _ in ch.execute_calls]
    assert tables == ["loom.memory_embeddings", "loom.memory", "loom.memory_code_links"]
    *tombstones, (links, link_params) = ch.execute_calls
    for sql, params in tombstones:
        assert "has(tags, 'consolidated') AND hasAny(tags, {src:Array(String)})" in sql
        # Paired with the user's keys in the row's own namespace.
        assert "{pairs:Array(Tuple(String, String))}" in sql
        assert params["src"] == ["src:k1"] and params["pairs"] == [("ns", "src:k1")]
        assert "UPDATE tombstoned_at = {now:DateTime}" in sql
    # the topic rows (and their history) are read after the tombstones, then bound with
    # their namespace
    ids_sql, ids_params = ch.read_calls[1]
    assert "has(tags, 'consolidated')" in ids_sql and "FROM loom.memory_versions" in ids_sql
    assert ids_params == {"org": "o", "uid": "alice"}
    assert (
        "DELETE" in links and "(namespace, memory_id) IN {ids:Array(Tuple(String, UUID))}" in links
    )
    assert link_params["ids"] == [("ns", topic)]
    assert not [sql for sql, _ in ch.execute_calls if "SELECT" in sql]


@pytest.mark.asyncio
async def test_a_user_purge_deletes_the_memory_rows_after_the_steps_that_read_their_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In a purge, `memory.tombstone` and the summaries' tombstone still stamp
    loom.memory, so the cache steps between can read the keys; `memory.retire`
    deletes the summaries and then the user's rows."""
    # and what a summary written since fed (test_erasure_step_order pins them)
    monkeypatch.setattr(graph_steps, "_SUMMARY_DERIVED_STEPS", ())
    pairs = ("namespace, memory_key FROM", [{"namespace": "ns", "memory_key": "k1"}])
    with erasure_mutations.purge_scope(True):
        ch = StubCH()
        ch.query_one_returns.append({"n": 1})
        assert await memory_steps.tombstone_memory_by_user("o", "alice", ch=ch) == 0
        ((sql, _),) = ch.execute_calls
        assert " ".join(sql.split()).startswith("ALTER TABLE loom.memory UPDATE tombstoned_at")

        ch = StubCH()
        ch.reads.append(pairs)
        ch.query_one_returns.extend([{"n": 1}, {"n": 1}])
        await graph_steps.tombstone_consolidated_dependents_by_user("o", "alice", ch=ch)
        embeddings, memory = (" ".join(sql.split()) for sql, _ in ch.execute_calls)
        assert embeddings.startswith("DELETE FROM loom.memory_embeddings")
        assert memory.startswith("ALTER TABLE loom.memory UPDATE tombstoned_at")

        # the user's late rows are stamped; the summaries' rows are tombstoned and their
        # links go; the answers quoting them
        # go again, while their keys still find them; the rows are deleted last, so a
        # step stopped part way still finds the summaries through them
        topic = "00000000-0000-0000-0000-0000000000c1"
        ch = StubCH()
        ch.reads.append(pairs)
        ch.reads.append(("toString(memory_id) AS id FROM (SELECT", [_summary_row(topic)]))
        keys, one = {"keys": ["t"]}, {"n": 1}
        ch.query_one_returns.extend(
            [one, one, one, one, keys, one, keys, one, keys, one, one, {"n": 2}]
        )
        # each row counts once: the summary's at its tombstone, the user's at their
        # delete here (memory.tombstone counts none in a purge, the late stamp neither)
        assert await graph_steps.retire_memory_by_user("o", "alice", ch=ch) == 8
        calls = [(" ".join(sql.split()), params) for sql, params in ch.execute_calls]
        stamp, embeddings, summaries, (links, link_params), cache, precedents, code, *rest = calls
        assert stamp[0].startswith("ALTER TABLE loom.memory UPDATE tombstoned_at")
        (memory, memory_params), (own, own_params) = rest
        assert embeddings[0].startswith("DELETE FROM loom.memory_embeddings")
        assert summaries[0].startswith("ALTER TABLE loom.memory UPDATE tombstoned_at")
        assert "loom.memory_code_links" in links and link_params["ids"] == [("ns", topic)]
        assert "loom.llm_response_cache" in cache[0] and cache[1]["keys"] == ["t"]
        assert "loom.memory_ask_precedents" in precedents[0]
        assert "DELETE FROM loom.code_answers" in code[0] and code[1]["keys"] == ["t"]
        assert memory.startswith("ALTER TABLE loom.memory DELETE WHERE")
        assert "{pairs:Array(Tuple(String, String))}" in memory
        assert memory_params["pairs"] == [("ns", "src:k1")]
        assert own.startswith("ALTER TABLE loom.memory DELETE WHERE")
        assert "user_id = {uid:String}" in own and own_params["uid"] == "alice"

    # a census residue on loom.memory reopens it, in either mode
    assert "memory.retire" in erasure_census.steps_to_reopen({"loom.memory": 1}, "user_id")


@pytest.mark.asyncio
async def test_the_saga_names_the_step_each_mutation_belongs_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run through `_do_step`, two steps that submit one statement get two tags, and
    the step's name does not outlast it."""
    from server.erasure.app import saga

    args = ("loom.memory", "UPDATE tombstoned_at = {now:DateTime}", "user_id = {uid:String}")
    tags: list[str] = []

    async def step() -> int:
        tags.append(erasure_mutations.mutation_tag(*args, {"uid": "alice"}))
        return 0

    async def no_checkpoint(**_: Any) -> None:
        return None

    monkeypatch.setattr(saga, "checkpoint", no_checkpoint)
    with erasure_mutations.saga_scope("s1"):
        for name in ("memory.tombstone", "memory.retire"):
            await saga._do_step(step_name=name, step_fn=step, completed=set(), row={})
        outside = erasure_mutations.mutation_tag(*args, {"uid": "alice"})
    assert len(set(tags)) == 2 and outside not in tags


@pytest.mark.asyncio
async def test_purge_of_a_wide_table_is_a_lightweight_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In purge mode `memory_embeddings` gets `DELETE FROM`
    (a `_row_exists` mask, no part rewrite); a narrow table
    keeps the `ALTER TABLE … DELETE`."""
    with erasure_mutations.purge_scope(True):
        ch = StubCH()
        ch.query_one_returns.append({"n": 3})
        await erasure_mutations.count_then_mutate(
            "loom.memory_embeddings",
            count_where="org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL",
            mutation="UPDATE tombstoned_at = {now:DateTime}",
            mutation_where="org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL",
            params={"org": "o", "ns": "bench-1"},
            ch=ch,
        )
        sql, params = ch.execute_calls[0]
        assert sql.lstrip().startswith("DELETE FROM loom.memory_embeddings")
        assert "lightweight_deletes_sync = 0" in sql
        assert "tombstoned_at IS NULL" not in sql  # purge removes tombstoned rows too
        assert params["erasure_mutation_tag"].startswith("erasure:")

        ch = StubCH()
        ch.query_one_returns.append({"n": 3})
        await erasure_mutations.count_then_mutate(
            "loom.memory",
            count_where="org = {org:String} AND tombstoned_at IS NULL",
            mutation="UPDATE tombstoned_at = {now:DateTime}",
            mutation_where="org = {org:String} AND tombstoned_at IS NULL",
            params={"org": "o"},
            ch=ch,
        )
        assert "ALTER TABLE loom.memory" in ch.execute_calls[0][0]
        assert "DELETE" in ch.execute_calls[0][0]


@pytest.mark.asyncio
async def test_tombstone_outside_purge_is_untouched_by_the_wide_table_delete() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"n": 2})
    await erasure_mutations.count_then_mutate(
        "loom.memory_embeddings",
        count_where="org = {org:String} AND tombstoned_at IS NULL",
        mutation="UPDATE tombstoned_at = {now:DateTime}",
        mutation_where="org = {org:String} AND tombstoned_at IS NULL",
        params={"org": "o"},
        ch=ch,
    )
    assert "ALTER TABLE loom.memory_embeddings" in ch.execute_calls[0][0]
    assert "UPDATE tombstoned_at" in ch.execute_calls[0][0]
