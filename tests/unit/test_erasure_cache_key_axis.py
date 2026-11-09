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
from server.erasure.store import predicates as erasure_predicates
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


# The statement the census and the steps share: the user's keys and those of the
# summaries built from them, from the current rows and the history, paired with the
# user's (namespace, src:key). What it matches:
# tests/integration/test_erasure_consolidated_quotes_live_ch.py.
_QUOTED_KEYS = erasure_predicates.USER_QUOTED_KEY_LIST


def test_the_quoted_keys_read_the_summaries_rows_and_history_by_the_users_pairs() -> None:
    assert "FROM loom.memory FINAL" in _QUOTED_KEYS
    assert "UNION ALL SELECT" in _QUOTED_KEYS and "FROM loom.memory_versions" in _QUOTED_KEYS
    assert "concat('src:', memory_key)" in _QUOTED_KEYS
    assert "user_id = {uid:String}" in _QUOTED_KEYS


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
    for step in ("graph_links.delete", "llm_response_cache.delete", "ask_precedents.delete"):
        assert step in calls, step
    # the communities go inside graph_links.delete, which reads ownership from the edge
    # links before they go
    assert "entity_communities.delete" not in calls
    # the text stores follow the memory tombstone (their predicate reads the user's keys)
    assert calls.index("memory.tombstone") < calls.index("llm_response_cache.delete")
    assert calls.index("memory.tombstone") < calls.index("ask_precedents.delete")
    assert calls[-1] == "verify.residue"


def test_registry_gives_the_three_tables_a_user_predicate_and_an_owner() -> None:
    owners = erasure_census.step_owners("user_id")
    assert owners["llm_response_cache.delete"] == ("loom.llm_response_cache",)
    assert owners["ask_precedents.delete"] == ("loom.memory_ask_precedents",)
    assert "loom.entity_communities" in owners["graph_links.delete"]
    assert erasure_census.steps_to_reopen({"loom.llm_response_cache": 3}, "user_id") == {
        "llm_response_cache.delete"
    }
    assert erasure_census.steps_to_reopen(
        {"memory_ask_precedents": 1, "entity_communities": 2}, "user_id"
    ) == {
        "ask_precedents.delete",
        "graph_links.delete",
    }
    by_table = {s.table: s for s in erasure_census._CENSUS}
    assert by_table["loom.llm_response_cache"].user_pred == f"hasAny(window_keys, {_QUOTED_KEYS})"
    assert by_table["loom.memory_ask_precedents"].user_pred == (
        f"hasAny(arrayConcat(window_keys, cited_keys), {_QUOTED_KEYS})"
    )
    # the graph tables stay namespace-only: ownership is not re-derivable after the links go
    assert by_table["loom.entity_edges"].user_pred is None


def test_cache_and_precedent_user_deletes_match_by_window_key_within_the_org() -> None:
    ch = _RecCH(count=3, keys=["k1", "k2"])
    n = asyncio.run(graph_steps.delete_llm_response_cache_by_user("o", "alice", ch=ch))
    assert n == 3
    # the keys are read with the census's own subquery, then bound
    ((read_sql, read_params),) = ch.reads
    assert read_sql == f"SELECT {_QUOTED_KEYS} AS keys" and read_params == {
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
    assert ch.reads == [(f"SELECT {_QUOTED_KEYS} AS keys", {"org": "o", "uid": "alice"})]
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
    ch = _RecCH(count=2, rows=[{"h": 7}])
    asyncio.run(graph_steps.delete_entity_communities_by_user("o", "alice", ch=ch))
    ((read_sql, read_params),) = ch.reads
    assert read_sql.startswith(
        "SELECT cityHash64(namespace, entity) AS h FROM (SELECT namespace, entity FROM ("
    )
    assert (
        "SELECT namespace, entity_a AS entity, memory_key FROM loom.entity_edge_memories FINAL WHERE org = {org:String} UNION ALL SELECT namespace, entity_b AS entity, memory_key FROM loom.entity_edge_memories FINAL"
        in read_sql
    )
    having = read_sql.split("HAVING", 1)[1]
    assert (
        "(SELECT namespace, memory_key FROM loom.memory WHERE org = {org:String} AND user_id = {uid:String})"
        in having
    )
    assert "has(tags, 'consolidated')" in having and "FROM loom.memory_versions" in having
    assert read_params == {"org": "o", "uid": "alice"}
    sql, params = ch.commands[0]
    # a row LABELLED by such an entity goes too: the label text is the residue
    assert sql.startswith(
        "ALTER TABLE loom.entity_communities DELETE WHERE (org = {org:String} AND "
        "(cityHash64(namespace, entity) IN {entities:Array(UInt64)} "
        "OR cityHash64(namespace, label) IN {entities:Array(UInt64)}))"
    )
    assert params["entities"] == [7]


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


_MEMORY = "11111111-1111-1111-1111-111111111111"


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
        "content_sha256": "c" * 64,
    }

    async def _find(org, mid, _version):
        return HistoryTarget.from_row({**target, "org": org, "memory_id": mid})

    events: list[str] = []

    def _stamp() -> int:
        events.append("stamp")
        return 1

    async def _head(identity, key, *, fresh=False, strong=False, scope=None):
        events.append("read")
        return SimpleNamespace(
            memory_id=_MEMORY,
            version=9,
            pinned=False,
            locked_until=None,
            lock_mode="",
            redacted_at=None,
            content_sha256="d" * 64,
        )

    async def _scope(org, ns, role, *, hides_answer=True):
        return True

    redacted: list[tuple[Any, ...]] = []

    async def _redact_versions(key, mid, version, *, head, **_):
        redacted.append((key, mid, version, head))
        return [12]  # a later copy of the version's text on the key, scrubbed with it

    deleted: list[tuple[str, str, str]] = []

    async def _cache(org, key, *, ch=None):
        deleted.append(("cache", org, key))
        return 2

    async def _prec(org, key, *, ch=None):
        deleted.append(("prec", org, key))
        return 1

    async def _code(org, key, *, ch=None):
        deleted.append(("code", org, key))
        return 3

    monkeypatch.setattr(memory_repo, "find_history_target", _find)
    monkeypatch.setattr(memory_repo, "get_by_key", _head)

    scrubbed: list[tuple[Any, ...]] = []

    async def _derivatives(
        *ident: Any,
        head_live: bool,
        live_link_inputs: str,
        through: int,
        redacted_sha: str,
        live_sha: str,
        set_chunks_kept: list[str] | None,
    ) -> list[str]:
        scrubbed.append((*ident, head_live, live_link_inputs, redacted_sha, live_sha))
        assert set_chunks_kept is None  # ClickHouse is the authority here
        return ["scrub"]

    async def _live_inputs(org: str, head: Any) -> str:
        return "head-inputs"

    async def _run(scrubs: list[str], *, apply: bool) -> dict[str, int]:
        assert scrubs == ["scrub"] and apply is True
        return {}

    async def _profiles(org, namespace, user_id, memory_id):
        return []

    monkeypatch.setattr(memory_repo, "profile_keys_built_from", _profiles)
    monkeypatch.setattr(memory_repo, "redact_versions", _redact_versions)
    monkeypatch.setattr(memory_repo, "next_version", _stamp)
    announced: list[tuple[str, int]] = []

    async def _announce(changes: list[Any]) -> int:
        announced.extend((event, int(change["version"])) for event, change in changes)
        return len(changes)

    monkeypatch.setattr(cur.webhooks, "announce", _announce)
    monkeypatch.setattr(memory_repo, "key_derivative_scrubs", _derivatives)
    monkeypatch.setattr(cur, "_live_link_inputs", _live_inputs)
    monkeypatch.setattr(memory_repo, "run_scrubs", _run)
    monkeypatch.setattr("server.auth.authz.row_in_scope", _scope)
    monkeypatch.setattr("server.memory.domain.lock.assert_unlocked", lambda *a, **kw: None)
    monkeypatch.setattr(memory_repo, "delete_llm_response_cache_by_key", _cache)
    monkeypatch.setattr(memory_repo, "delete_ask_precedents_by_key", _prec)
    monkeypatch.setattr(memory_repo, "delete_code_answers_by_key", _code)
    inp = SimpleNamespace(
        org="o",
        memory_id=_MEMORY,
        version=7,
        reason="leak",
        redacted_by="ops",
    )
    out = await cur.handle_memory_redact(inp)
    # the model response cache's rows and code.ask's cached answers
    assert out.redacted is True and out.cache_rows_deleted == 5 and out.precedent_rows_deleted == 1
    # The key and the live head the handler checked go with the scrub, for its
    # transaction to recheck.
    key = ("o", "n", "a", "u", "s", "user_employer")
    assert redacted == [(key, inp.memory_id, 7, (_MEMORY, 9))]
    assert announced == [("memory.redacted", 7), ("memory.redacted", 12)]
    # The stamp that bounds the search-row purge is taken before the head is read.
    assert events[:2] == ["stamp", "read"]
    # The key's derived rows are scrubbed too; the head is live, so its text stays.
    # The redacted version's hash goes too: its search rows go whatever their stamp.
    assert scrubbed == [
        ("o", "n", "a", "u", "s", "user_employer", True, "head-inputs", "c" * 64, "d" * 64)
    ]
    assert deleted == [
        ("cache", "o", "user_employer"),
        ("code", "o", "user_employer"),
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
