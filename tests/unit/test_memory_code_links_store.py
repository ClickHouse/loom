"""The link store's statements against a recording client: an insert drops its rows
again for memories an erasure removed meanwhile, writes that follow a read take the
version after it, and each read is scoped as its callers rely on."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from server.memory.store import code_links as store


class _CH:
    def __init__(self, answers: dict[str, list[list[dict[str, Any]]]] | None = None) -> None:
        self.answers = answers or {}
        self.sql: list[tuple[str, dict[str, Any]]] = []
        self.inserted: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append((sql, dict(params or {})))
        for key, queue in self.answers.items():
            if key in sql and queue:
                return queue.pop(0)
        return []

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = await self.query_all(sql, params)
        return rows[0] if rows else None

    async def execute(self, sql: str, params: Any = None) -> None:
        self.sql.append((sql, dict(params or {})))

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.extend(rows)


def _row(mid: str, **kw: Any) -> dict[str, Any]:
    return store.link_row(org="o", memory_id=mid, path="a.py", **kw)


@pytest.mark.asyncio
async def test_an_insert_for_a_memory_erased_meanwhile_is_deleted_again() -> None:
    erasing = {"FROM loom.erasure_requests": [[{"org": "o"}]]}
    ch = _CH({**erasing, "FROM loom.memory FINAL": [[{"mid": "live"}]]})
    await store.insert([_row("live"), _row("erased")], ch=ch)
    assert [r["memory_id"] for r in ch.inserted] == ["live", "erased"]
    (delete, params) = ch.sql[-1]
    assert delete.startswith("DELETE FROM loom.memory_code_links")
    assert (params["org"], params["ids"]) == ("o", ["erased"])
    # only rows older than the liveness read: a link written after it (a restore) stays
    assert "version < {before:UInt64}" in delete and isinstance(params["before"], int)
    ch = _CH({**erasing, "FROM loom.memory FINAL": [[{"mid": "live"}]]})
    await store.insert([_row("live")], ch=ch)
    assert not any(sql.startswith("DELETE") for sql, _ in ch.sql)
    await store.insert([], ch=ch)


@pytest.mark.asyncio
async def test_an_insert_reads_no_memory_where_nothing_is_being_erased() -> None:
    ch = _CH()
    await store.insert([_row("m")], ch=ch)
    assert [sql for sql, _ in ch.sql if "loom.memory FINAL" in sql] == []


def test_a_link_row_types_every_column_the_store_writes() -> None:
    assert store.LinkRow.__required_keys__ == set(store.LINK_COLS)


@pytest.mark.asyncio
async def test_a_link_read_back_carries_its_memory_id_as_text() -> None:
    mid = uuid.uuid4()
    ch = _CH({"SELECT *": [[{"org": "o", "memory_id": mid}]]})
    (row,) = await store.for_memories("o", [str(mid)], ch=ch)
    assert row["memory_id"] == str(mid)


def test_a_write_that_follows_a_read_takes_the_version_after_it() -> None:
    read = _row("m") | {"version": 41}
    assert store.link_row(read, follows=41)["version"] == 42
    assert store.link_row(read)["version"] > 42  # a fresh write: now
    # a tombstone always follows the row it read: a write made since then stays
    (gone,) = store.tombstones([read])
    assert (gone["version"], gone["tombstoned_at"] is not None) == (42, True)


@pytest.mark.asyncio
async def test_reads_are_scoped_as_their_callers_rely_on() -> None:
    ch = _CH(
        {
            "policy_due = 1": [[{"mid": "m"}]],
            "SELECT DISTINCT org, code_namespace": [[{"org": "o", "code_namespace": "c"}]],
            "AS t": [[{"t": 7}], [{"t": 9}]],
            "ORDER BY (toUnixTimestamp64Micro(queued_at)": [
                [{"pos": (1, 2, "o", "n", "a", "-", "-", "k")}]
            ],
        }
    )
    assert await store.for_memories("o", [], ch=ch) == []
    await store.for_memories("o", ["m", "m"], ch=ch)
    assert ch.sql[-1][1]["ids"] == ["m"]
    await store.for_paths(
        "o", ["c"], ["a.py"], include_former=True, namespaces=["n"], states=["changed"], ch=ch
    )
    # a link's path, its former ones and its anchor: a read each, narrowed by its own index
    reads = ch.sql[-3:]
    assert [
        next(c for c in ("anchored_path", "former.path", "path") if c in sql.split("WHERE")[1])
        for sql, _ in reads
    ] == [
        "path",
        "former.path",
        "anchored_path",
    ]
    assert all(" OR " not in sql for sql, _ in reads)
    for _, params in reads:
        assert (params["code_namespace"], params["namespace"], params["state"]) == (
            ["c"],
            ["n"],
            ["changed"],
        )
    await store.pending("o", "c", days=14, ch=ch)
    assert ch.sql[-1][1]["days"] == 14
    assert await store.policy_due("o", "c", "v", limit=50, ch=ch) == ["m"]
    assert ch.sql[-1][1]["lim"] == 50
    assert await store.code_namespaces(ch=ch) == [("o", "c")]
    assert await store.served_heads("o", [], ch=ch) == []
    await store.served_heads("o", [("n", "k"), ("n", "k")], ch=ch)
    assert ch.sql[-1][1]["keys"] == [("n", "k")]
    assert await store.queue_horizon(ch=ch) == 7
    assert await store.queue_tail(ch=ch) == (1, 2, "o", "n", "a", "-", "-", "k")
    assert await store.for_identities([], ch=ch) == []
    await store.for_identities([("o", "n", "a", "-", "-", "k")] * 2, ch=ch)
    assert len(ch.sql[-1][1]["ids"]) == 1
    assert await store.memory_heads("o", [], ch=ch) == []
    assert await store.code_change_policies("o", [], ch=ch) == {}


@pytest.mark.asyncio
async def test_the_queue_is_read_after_the_cursor_up_to_the_cutoff() -> None:
    pos = (5, 3, "o", "n", "a", "-", "-", "k")
    head = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
    }
    ch = _CH(
        {
            "AS t": [[{"t": 100}]],
            "FROM loom.memory_code_link_queue": [[{"pos": pos}]],
            "FROM loom.memory FINAL": [[dict(head)]],
        }
    )
    heads, positions, cutoff = await store.queued_memories(
        (1, 0, "", "", "", "", "", ""), 10, ["code-a"], lag_s=2, ch=ch
    )
    assert (positions, cutoff[0], heads[0]["pos"]) == ([pos], 100, pos)
    sql, params = ch.sql[1]
    assert "> {after:" in sql and params["cut"] == 100
    # only the heads of memories that may name code: a repo: tag, a code namespace, a link
    read, params = ch.sql[2]
    assert "startsWith(t, 'repo:')" in read and params["cns"] == ["code-a"]
    # a link is looked for only under the page's identities, not the org's every link
    assert read.count("memory_key) IN {ident:") == 2


@pytest.mark.asyncio
async def test_a_backfill_page_reads_after_its_last_identity() -> None:
    key = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
    }
    ch = _CH({"UNION DISTINCT": [[key]], "FROM loom.memory FINAL": [[key]]})
    idents, heads = await store.backfill_page(("o", "n", "a", "-", "-", "j"), 50, ["code-a"], ch=ch)
    assert idents == [("o", "n", "a", "-", "-", "k")] and heads == [key]
    sql, params = ch.sql[0]
    assert (params["cns"], params["lim"]) == (["code-a"], 50)
    # the cursor bound column by column, which loom.memory's primary index can use
    assert "org >= {a0:String} AND (org > {a0:String} OR (namespace >= {a1:String}" in sql
    assert "memory_key > {a5:String}" in sql and params["a5"] == "j"
    ch = _CH(
        {"namespace, code_change_policy": [[{"namespace": "n", "code_change_policy": "flag"}]]}
    )
    assert await store.code_change_policies("o", ["n"], ch=ch) == {"n": "flag"}


@pytest.mark.asyncio
async def test_the_sweep_pages_through_every_linked_memory_and_drops_the_gone(
    monkeypatch,
) -> None:
    def row(mid: str) -> dict[str, str]:
        return {
            "org": "o",
            "code_namespace": "c",
            "path": "p",
            "qualified_name": "q",
            "namespace": "n",
            "memory_id": mid,
        }

    monkeypatch.setattr(store, "SWEEP_ROWS_PER_WRITE", 2)
    page = [row("erased"), row("live"), row("erased"), row("live")]
    ch = _CH(
        {"ORDER BY org, code_namespace": [page], "FROM loom.memory FINAL": [[{"mid": "live"}]]}
    )
    nxt, dropped = await store.sweep_page(None, 2, ch=ch)
    assert (nxt, dropped) == (("o", "c", "p", "q", "n", "live"), 1)  # a full page: more may follow
    (read, params) = ch.sql[0]
    assert "{a0:" not in read and params["lim"] == 4
    (delete, params) = next((sql, p) for sql, p in ch.sql if sql.startswith("DELETE"))
    assert delete.startswith("DELETE FROM loom.memory_code_links") and params["ids"] == ["erased"]
    ch = _CH(
        {
            "ORDER BY org, code_namespace": [[row("live")]],
            "FROM loom.memory FINAL": [[{"mid": "live"}]],
        }
    )
    assert await store.sweep_page(("o", "c", "p", "q", "n", "a"), 2, ch=ch) == (None, 0)  # the end
    read, params = ch.sql[0]
    assert "memory_id > {a5:UUID}" in read and params["a0"] == "o"


@pytest.mark.asyncio
async def test_a_long_path_or_commit_list_is_read_in_slices_under_the_field_cap() -> None:
    from server.core.ch_params import FIELD_BYTES

    # a week behind: thousands of changed paths and commits, each list past one field
    paths = [f"src/Storages/MergeTree/Part{i}/Reader{i}.cpp" for i in range(4000)]
    commits = [f"{i:040x}" for i in range(3000)]
    row = {"org": "o", "code_namespace": "c", "path": paths[0], "qualified_name": "f"}
    row |= {"namespace": "n", "memory_id": "m"}
    ch = _CH({"FROM loom.memory_code_links": [[row]] * 50})
    assert await store.for_paths("o", ["c"], paths, ch=ch) == [row]  # once, not per slice
    bound = [p["path"] for _, p in ch.sql]
    assert len(bound) > 1 and [p for part in bound for p in part] == paths
    assert all(len(repr(part)) < 2 * FIELD_BYTES for part in bound)
    ch = _CH()
    await store.for_commits("o", ["c"], commits, ch=ch)
    assert len(ch.sql) > 1 and all("path" not in p for _, p in ch.sql)
    assert [c for _, p in ch.sql for c in p["checked_commit"]] == commits
    ch = _CH()
    await store.for_commits("o", ["c"], [], ch=ch)
    assert ch.sql == []  # an empty list narrows to none: nothing is read


@pytest.mark.asyncio
async def test_a_long_memory_id_list_is_read_in_slices() -> None:
    import uuid

    from server.core.ch_params import FIELD_BYTES

    # every memory a recheck marked due, or every one waiting on the first index
    ids = [str(uuid.UUID(int=i)) for i in range(5000)]
    for read in (store.memory_heads, store.for_memories):
        ch = _CH()
        await read("o", ids, ch=ch)
        assert len(ch.sql) > 1 and [i for _, p in ch.sql for i in p["ids"]] == ids
        assert all(len(repr(p["ids"])) < 2 * FIELD_BYTES for _, p in ch.sql)


@pytest.mark.asyncio
async def test_a_batch_of_identities_with_long_keys_is_read_in_slices() -> None:
    from server.core.ch_params import FIELD_BYTES

    # one queue batch: 500 writes under long keys, in a long agent namespace
    ns = "agent-" + "n" * 57
    idents = [("o", ns, "claude-code", "u", f"s{i}", "k" * 200 + str(i)) for i in range(500)]
    ch = _CH()
    await store.for_identities(idents, ch=ch)
    await store._heads(ch, idents)
    assert len(ch.sql) > 2 and all(len(repr(p)) < 2 * FIELD_BYTES for _, p in ch.sql)
    assert sorted(i for _, p in ch.sql if "ids" in p for i in p["ids"]) == sorted(idents)
    assert sorted(i for _, p in ch.sql if "ident" in p for i in p["ident"]) == sorted(idents)


def test_a_tombstone_keeps_no_suggestion() -> None:
    judged = store.link_row(
        org="o",
        memory_id="m",
        path="a.py",
        suggestion="read_rows frees its buffer now",
        judged_hash="h",
        version=5,
    )
    (gone,) = store.tombstones([judged])
    assert (gone["suggestion"], gone["judged_hash"]) == ("", "")
    assert gone["tombstoned_at"] is not None and gone["version"] > judged["version"]


def test_the_queue_ttl_is_the_tables() -> None:
    from pathlib import Path

    schema = Path(__file__).parents[2].joinpath("schemas", "schema.sql").read_text()
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS loom.memory_code_link_queue") :]
    ttl = table[table.index("TTL ") : table.index("\n", table.index("TTL "))]
    assert ttl == f"TTL toDateTime(queued_at) + toIntervalDay({store.QUEUE_TTL_DAYS})"


@pytest.mark.asyncio
async def test_the_sweep_deletes_every_version_of_a_key_long_tombstoned() -> None:
    key = {"org": "o", "code_namespace": "c", "path": "p", "qualified_name": "q", "namespace": "n"}
    page = [key | {"memory_id": "old"}, key | {"memory_id": "kept"}]
    versions = [key | {"memory_id": "old", "version": v} for v in (5, 9, 12)]
    ch = _CH(
        {
            "ORDER BY org, code_namespace": [page],
            "FROM loom.memory FINAL": [[{"mid": "old"}, {"mid": "kept"}]],
            "FROM loom.memory_code_links FINAL WHERE": [[page[0] | {"version": 9}]],
            "version FROM loom.memory_code_links WHERE": [versions],
        }
    )
    await store.sweep_page(None, 10, ch=ch)
    ((purge, params),) = [(sql, p) for sql, p in ch.sql if sql.startswith("DELETE")]
    # every version under the tombstone (9), and not the row written after the read (12)
    assert params["rows"] == [("o", "c", "p", "q", "n", "old", v) for v in (5, 9)]


@pytest.mark.asyncio
async def test_lesson_excerpts_are_read_only_from_the_lesson_namespaces() -> None:
    from server.memory.store import code_plane_reads

    ch = _CH()
    mid = str(uuid.uuid4())
    await code_plane_reads.served_excerpts("o", ["team", "me"], [mid], ch=ch)
    ((sql, params),) = ch.sql
    assert "namespace IN {nss:Array(String)}" in sql and params["nss"] == ["me", "team"]
    assert await code_plane_reads.served_excerpts("o", [], [mid], ch=ch) == []
    assert len(ch.sql) == 1
