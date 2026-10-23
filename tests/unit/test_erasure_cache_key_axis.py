"""Cached answers get a key axis; the user saga reaches the
cache, the precedents and the communities; `memory.redact` deletes cached
text by key; `agent_events.scrub` scrubs the memory key and the identifier
entries of `args`. CH-free — statements are read back from recording stubs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import server.erasure.app.saga as h
import server.memory.store as memory_repo
from server.erasure.store import census as erasure_census
from server.erasure.store import steps_graph as graph_steps
from server.erasure.store import steps_memory as memory_steps
from server.llm import cache as lc
from server.llm.cache import CachedLLMClient, cache_key
from server.memory.store.history import HistoryTarget
from tests.unit.test_llm_response_cache import _CH, _Inner, _mem, _settle


class _RecCH:
    """`keys` answers the read of the user's keys; `rows` every other read that is not
    a system.mutations poll."""

    def __init__(
        self, count: int = 3, keys: list[str] | None = None, rows: list[dict] | None = None
    ) -> None:
        self.count = count
        self.keys = keys or []
        self.rows = rows or []
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql, params=None):
        if " AS keys" in sql:
            self.reads.append((" ".join(sql.split()), dict(params or {})))
            return {"keys": self.keys}
        self.queries.append((" ".join(sql.split()), dict(params or {})))
        return {"n": self.count}

    async def execute(self, sql, params=None):
        self.commands.append((" ".join(sql.split()), dict(params or {})))

    async def query_all(self, sql, params=None):
        if "startsWith(t.name, '_provider_rebuild_')" in sql:
            return []
        if "system.mutations" not in sql:
            self.reads.append((" ".join(sql.split()), dict(params or {})))
            return self.rows
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]


_USER_KEYS = (
    "(SELECT groupUniqArray(memory_key) FROM loom.memory "
    "WHERE org = {org:String} AND user_id = {uid:String})"
)


# ---------------------------------------------------------------------------
# the cache writer fills window_keys from the ask's window
# ---------------------------------------------------------------------------


async def test_the_cache_row_carries_the_window_keys_the_call_rendered() -> None:
    ch = _CH()
    lc.set_window_keys(["k1", "k2", "k1", "", None])
    try:
        await CachedLLMClient(_Inner(), org="o", ch=ch, table="loom.t").complete(
            "sys", "u", max_tokens=100
        )
        await _settle()
        assert len(ch.inserts) == 1
        row = ch.inserts[0][1][0]
        assert row["window_keys"] == ["k1", "k2"]  # ordered, de-duplicated, no blanks
        # a narrowed scope is what one call stores; the enclosing window returns
        with lc.window_keys_scope(["k9"]):
            assert lc.current_window_keys() == ("k9",)
            await CachedLLMClient(_Inner(), org="o", ch=ch, table="loom.t").complete(
                "sys", "v", max_tokens=100
            )
        assert lc.current_window_keys() == ("k1", "k2")
        await _settle()
        assert ch.inserts[1][1][0]["window_keys"] == ["k9"]
    finally:
        lc.set_window_keys([])
    # outside an ask there is no window: the row has no key axis
    await CachedLLMClient(_Inner(), org="o", ch=ch, table="loom.t").complete(
        "sys", "w", max_tokens=100
    )
    await _settle()
    assert ch.inserts[2][1][0]["window_keys"] == []


def test_the_cache_key_does_not_change_with_the_window_keys() -> None:
    """The key is the prompt; the window keys are a column beside it, so
    every row cached without window keys still hits and the hit rate is untouched."""
    lc.set_window_keys(["a"])
    k1 = cache_key("s", "u", 100)
    lc.set_window_keys(["b", "c"])
    k2 = cache_key("s", "u", 100)
    lc.set_window_keys([])
    assert k1 == k2 == cache_key("s", "u", 100)


async def test_synthesize_publishes_exactly_the_keys_it_rendered() -> None:
    from server.memory.app.ask import synthesis

    ch = _CH()
    lc.set_window_keys(["pool-only", "k1", "k2"])  # what ask() would publish for the pool
    try:
        window = [_mem("k1", "The dose is 20 mg."), _mem("k2", "Taken at breakfast.")]
        await synthesis._synthesize(
            CachedLLMClient(_Inner(), org="o", ch=ch, table="loom.t"), "dose?", list(window)
        )
        await _settle()
        assert ch.inserts[0][1][0]["window_keys"] == ["k1", "k2"]
        # the pool is back once the synthesis call returns (the judge runs under it)
        assert lc.current_window_keys() == ("pool-only", "k1", "k2")
    finally:
        lc.set_window_keys([])


def test_the_cache_table_insert_is_projected_onto_the_live_columns() -> None:
    """A service whose table lacks the axis column still stores the row, minus the axis
    (the schema-skew projection), instead of losing every cache write."""
    from server.core.ch import PROJECTED_TABLES

    assert "loom.llm_response_cache" in PROJECTED_TABLES


# ---------------------------------------------------------------------------
# the user saga's three new steps and their statements
# ---------------------------------------------------------------------------


async def test_user_saga_orders_the_new_steps_around_the_link_tables(monkeypatch) -> None:
    import server.erasure.app.backends as eb

    calls: list[str] = []

    async def fake_do_step(*, step_name, step_fn, completed, row, **kw):
        calls.append(step_name)
        return SimpleNamespace(step=step_name, rows_affected=0, notes="")

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(h, "_do_step", fake_do_step)
    monkeypatch.setattr(eb, "all_backends", list)
    monkeypatch.setattr("server.core.namespace_write_marks.bump", _noop)
    await h._saga_user_id("o", "alice", completed=set(), row={})
    for step in ("entity_communities.delete", "llm_response_cache.delete", "ask_precedents.delete"):
        assert step in calls, step
    # communities read ownership from the edge links: after the graph, before the links go
    assert calls.index("entity_graph.delete") < calls.index("entity_communities.delete")
    assert calls.index("entity_communities.delete") < calls.index("graph_links.delete")
    # the text stores follow the memory tombstone (their predicate reads the user's keys)
    assert calls.index("memory.tombstone") < calls.index("llm_response_cache.delete")
    assert calls.index("memory.tombstone") < calls.index("ask_precedents.delete")
    assert calls[-1] == "verify.residue"


def test_registry_gives_the_three_tables_a_user_predicate_and_an_owner() -> None:
    owners = erasure_census.step_owners("user_id")
    assert owners["llm_response_cache.delete"] == ("loom.llm_response_cache",)
    assert owners["ask_precedents.delete"] == ("loom.memory_ask_precedents",)
    assert owners["entity_communities.delete"] == ("loom.entity_communities",)
    assert erasure_census.steps_to_reopen({"loom.llm_response_cache": 3}, "user_id") == {
        "llm_response_cache.delete"
    }
    assert erasure_census.steps_to_reopen(
        {"memory_ask_precedents": 1, "entity_communities": 2}, "user_id"
    ) == {
        "ask_precedents.delete",
        "entity_communities.delete",
    }
    by_table = {s.table: s for s in erasure_census._CENSUS}
    assert by_table["loom.llm_response_cache"].user_pred == f"hasAny(window_keys, {_USER_KEYS})"
    assert by_table["loom.memory_ask_precedents"].user_pred == (
        f"hasAny(arrayConcat(window_keys, cited_keys), {_USER_KEYS})"
    )
    # the graph tables stay namespace-only: ownership is not re-derivable after the links go
    assert by_table["loom.entity_edges"].user_pred is None


def test_cache_and_precedent_user_deletes_match_by_window_key_within_the_org() -> None:
    ch = _RecCH(count=3, keys=["k1", "k2"])
    n = asyncio.run(graph_steps.delete_llm_response_cache_by_user("o", "alice", ch=ch))
    assert n == 3
    # the user's keys are read with the census's own subquery, then bound
    ((read_sql, read_params),) = ch.reads
    assert read_sql == f"SELECT {_USER_KEYS} AS keys" and read_params == {
        "org": "o",
        "uid": "alice",
    }
    ((count_sql, count_params),) = ch.queries
    ((mut_sql, mut_params),) = ch.commands
    assert count_sql == (
        "SELECT count() AS n FROM loom.llm_response_cache WHERE org = {org:String} "
        "AND hasAny(window_keys, {keys:Array(String)})"
    )
    assert mut_sql.startswith(
        "ALTER TABLE loom.llm_response_cache DELETE WHERE (org = {org:String} "
        "AND hasAny(window_keys, {keys:Array(String)}))"
    )
    assert count_params == {"org": "o", "keys": ["k1", "k2"]} and mut_params["keys"] == ["k1", "k2"]
    assert "namespace" not in count_sql  # an all_mine window spans namespaces; the key is the axis

    ch = _RecCH(count=1, keys=["k1"])
    asyncio.run(graph_steps.delete_ask_precedents_by_user("o", "alice", ch=ch))
    sql, _ = ch.commands[0]
    assert sql.startswith(
        "ALTER TABLE loom.memory_ask_precedents DELETE WHERE (org = {org:String} "
        "AND hasAny(arrayConcat(window_keys, cited_keys), {keys:Array(String)}))"
    )
    # no keys, no statement
    ch = _RecCH(count=1)
    assert asyncio.run(graph_steps.delete_ask_precedents_by_user("o", "alice", ch=ch)) == 0
    assert ch.commands == [] and ch.queries == []


def test_communities_leave_when_only_the_users_memories_linked_the_entity() -> None:
    ch = _RecCH(count=2, rows=[{"namespace": "ns", "entity": "alice"}])
    asyncio.run(graph_steps.delete_entity_communities_by_user("o", "alice", ch=ch))
    ((read_sql, read_params),) = ch.reads
    assert read_sql.startswith("SELECT namespace, entity FROM (SELECT namespace, entity FROM (")
    assert (
        "SELECT namespace, entity_a AS entity, memory_key FROM loom.entity_edge_memories FINAL WHERE org = {org:String} UNION ALL SELECT namespace, entity_b AS entity, memory_key FROM loom.entity_edge_memories FINAL"
        in read_sql
    )
    assert (
        "HAVING countIf((namespace, memory_key) IN (SELECT namespace, memory_key FROM loom.memory WHERE org = {org:String} AND user_id = {uid:String})) > 0 AND countIf((namespace, memory_key) NOT IN (SELECT namespace, memory_key FROM loom.memory WHERE org = {org:String} AND user_id = {uid:String})) = 0"
        in read_sql
    )
    assert read_params == {"org": "o", "uid": "alice"}
    sql, params = ch.commands[0]
    # a row LABELLED by such an entity goes too: the label text is the residue
    assert sql.startswith(
        "ALTER TABLE loom.entity_communities DELETE WHERE (org = {org:String} AND "
        "((namespace, entity) IN {entities:Array(Tuple(String, String))} "
        "OR (namespace, label) IN {entities:Array(Tuple(String, String))}))"
    )
    assert params["entities"] == [("ns", "alice")]


def test_census_user_scope_counts_the_three_tables_with_the_step_predicates() -> None:
    ch = _RecCH(count=0)
    out = asyncio.run(erasure_census.residue_census("o", "user_id", "alice", ch=ch))
    by_table = {sql.split("FROM ")[1].split()[0]: sql for sql, _ in ch.queries}
    assert out["loom.llm_response_cache"] == 0
    assert "hasAny(window_keys," in by_table["loom.llm_response_cache"]
    assert "hasAny(arrayConcat(window_keys, cited_keys)," in by_table["loom.memory_ask_precedents"]
    assert (
        "(namespace, entity) IN (SELECT namespace, entity FROM ("
        in by_table["loom.entity_communities"]
    )
    # the namespace census of the same tables is unchanged
    ch = _RecCH(count=0)
    asyncio.run(erasure_census.residue_census("o", "namespace", "ns", ch=ch))
    by_table = {sql.split("FROM ")[1].split()[0]: sql for sql, _ in ch.queries}
    assert (
        "namespace = {ns:String}" in by_table["loom.llm_response_cache"]
        and "hasAny" not in by_table["loom.llm_response_cache"]
    )


# ---------------------------------------------------------------------------
# memory.redact deletes the cached text by key
# ---------------------------------------------------------------------------


def test_redact_by_key_statements_are_org_scoped_has_lookups() -> None:
    ch = _RecCH(count=2)
    n = asyncio.run(memory_repo.delete_llm_response_cache_by_key("o", "user_employer", ch=ch))
    assert n == 2
    sql, params = ch.commands[0]
    assert sql.startswith(
        "ALTER TABLE loom.llm_response_cache DELETE WHERE org = {org:String} AND has(window_keys, {key:String})"
    )
    assert params["org"] == "o" and params["key"] == "user_employer"
    ch = _RecCH(count=1)
    asyncio.run(memory_repo.delete_ask_precedents_by_key("o", "user_employer", ch=ch))
    assert "has(arrayConcat(window_keys, cited_keys), {key:String})" in ch.commands[0][0]


async def test_redact_handler_reports_the_cache_and_precedent_rows_it_deleted(monkeypatch) -> None:
    from server.memory.app import curation as cur

    target = {
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "s",
        "memory_key": "user_employer",
        "version": 7,
        "tombstoned_at": None,
    }

    async def _find(org, mid):
        return HistoryTarget.from_row({**target, "org": org, "memory_id": mid})

    async def _head(identity, key, *, fresh=False, strong=False, scope=None):
        return SimpleNamespace(version=9, pinned=False, locked_until=None, lock_mode="")

    async def _scope(org, ns, role, *, hides_answer=True):
        return True

    redacted: list[tuple[str, str, int]] = []

    async def _redact_versions(org, namespace, user_id, mid, version, *, when, reason_blob):
        redacted.append((org, mid, version))

    async def _exists(org, target, mid, version):
        return True

    deleted: list[tuple[str, str, str]] = []

    async def _cache(org, key, *, ch=None):
        deleted.append(("cache", org, key))
        return 2

    async def _prec(org, key, *, ch=None):
        deleted.append(("prec", org, key))
        return 1

    monkeypatch.setattr(memory_repo, "find_history_target", _find)
    monkeypatch.setattr(memory_repo, "get_by_key", _head)
    scrubbed: list[tuple[Any, ...]] = []

    async def _derivatives(*ident: Any, head_live: bool, live_link_inputs: str) -> list[str]:
        scrubbed.append((*ident, head_live, live_link_inputs))
        return ["scrub"]

    async def _live_inputs(org: str, head: Any) -> str:
        return "head-inputs"

    async def _run(scrubs: list[str], *, apply: bool) -> dict[str, int]:
        assert scrubs == ["scrub"] and apply is True
        return {}

    async def _profiles(org, namespace, user_id, memory_id):
        return []

    monkeypatch.setattr(memory_repo, "profile_keys_built_from", _profiles)
    monkeypatch.setattr(memory_repo, "version_exists", _exists)
    monkeypatch.setattr(memory_repo, "redact_versions", _redact_versions)
    monkeypatch.setattr(memory_repo, "key_derivative_scrubs", _derivatives)
    monkeypatch.setattr(cur, "_live_link_inputs", _live_inputs)
    monkeypatch.setattr(memory_repo, "run_scrubs", _run)
    monkeypatch.setattr("server.auth.authz.row_in_scope", _scope)
    monkeypatch.setattr("server.memory.domain.lock.assert_unlocked", lambda *a, **kw: None)
    monkeypatch.setattr(memory_repo, "delete_llm_response_cache_by_key", _cache)
    monkeypatch.setattr(memory_repo, "delete_ask_precedents_by_key", _prec)
    inp = SimpleNamespace(
        org="o",
        memory_id="11111111-1111-1111-1111-111111111111",
        version=7,
        reason="leak",
        redacted_by="ops",
    )
    out = await cur.handle_memory_redact(inp)
    assert out.redacted is True and out.cache_rows_deleted == 2 and out.precedent_rows_deleted == 1
    assert redacted == [("o", inp.memory_id, 7)]
    # The key's derived rows are scrubbed too; the head is live, so its text stays.
    assert scrubbed == [("o", "n", "a", "u", "s", "user_employer", True, "head-inputs")]
    assert deleted == [
        ("cache", "o", "user_employer"),
        ("prec", "o", "user_employer"),
    ]


# ---------------------------------------------------------------------------
# agent_events.scrub reaches memory_key and the identifier entries of args
# ---------------------------------------------------------------------------


def test_events_scrub_rewrites_memory_key_and_the_identifier_args_in_one_mutation() -> None:
    from server.observability.event_args import IDENTITY_KEYS

    ch = _RecCH(count=5)
    n = asyncio.run(memory_steps.scrub_events_by_user("o", "alice", ch=ch))
    assert n == 5
    sql, params = ch.commands[0]
    assert sql.startswith(
        "ALTER TABLE loom.agent_events UPDATE user_id = '<erased>', memory_key = if(memory_key = '', '', '<erased>'), args = mapApply((k, v) -> (k, if(k IN ("
    )
    for key in (
        "memory_key",
        "memory_keys",
        "memory_id",
        "thread_id",
        "block_key",
        "handoff_id",
        "entity",
        "subject",
    ):
        assert f"'{key}'" in sql, key
    # A gateway's end user is named by its subject too.
    assert "WHERE (org = {org:String} AND (user_id = {uid:String} OR subject = {p:String}))" in sql
    assert "subject = if(subject = {p:String}, '<erased>', subject)" in sql
    assert (params["uid"], params["p"]) == ("alice", "user:alice")
    # the identity tuple never reaches args (event_args strips it); the scrub list is the rest
    assert not (set(memory_steps.ARGS_SCRUB_KEYS) & IDENTITY_KEYS)
    assert erasure_census.STEP_ONLY_TABLES["agent_events.scrub"] == ("loom.agent_events",)


@pytest.mark.parametrize("scope", ["namespace", "user_id"])
def test_every_census_table_has_an_owner_in_the_registry(scope: str) -> None:
    owners = set(sum(erasure_census.step_owners(scope).values(), ()))
    assert owners == set(erasure_census.census_tables(scope))
