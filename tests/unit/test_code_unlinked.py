"""Why a served memory that names code holds no link (`code_unlinked.unlinked`), the read
behind namespace.validate's `code_unlinked_*` findings: the causes from stubbed reads, and
its statements on clickhouse-local against the production tables."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app import code_unlinked
from server.memory.app.code_link_reads import memory_inputs
from server.memory.store import code_links as store
from tests.unit import clickhouse_local

pytestmark = pytest.mark.asyncio
local_sql = pytest.mark.skipif(
    clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed"
)

# the link pass read every write up to this queue time (µs), and its backfill is done
READ_TO = 5_000
PASS = {"queue": f'[{READ_TO}, 7, "o", "ns", "a", "-", "-", "k"]', "backfill": '{"done": true}'}


def _memory(key: str, text: str, *, tags: list[str], files: list[str] | None = None) -> dict:
    return {
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "mid": f"id-{key}",
        "value": text,
        "tags": tags,
        "files": files or [],
        "sha": f"sha-{key}",
        "version": 1_000_000,  # ns: read by the pass (before READ_TO µs)
    }


def _link(memory: dict, name: str, state: str, cns: str = "code-a") -> dict:
    return {
        "mid": memory["mid"],
        "code_namespace": cns,
        "qualified_name": name,
        "state": state,
        "inputs_hash": memory_inputs(memory, cns),
    }


class _World:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.memories: list[dict] = []
        self.links: list[dict] = []
        self.cursors = dict(PASS)
        self.asked: dict[str, Any] = {}
        self.repos = [type("Cfg", (), {"org": "o", "namespace": "code-a"})()]
        tags = {"repo:a": "code-a", "repo:secret": "code-secret"}

        async def served(org: str, namespace: str, keys: Any = None, *, limit: int) -> list:
            self.asked["served"] = (org, namespace, keys, limit)
            return [m for m in self.memories if keys is None or m["memory_key"] in keys]

        async def link_states(org: str, namespace: str, ids: list[str]) -> list:
            self.asked["links"] = sorted(ids)
            return [a for a in self.links if a["mid"] in ids]

        async def readable(org: str, namespaces: set[str]) -> set[str]:
            return namespaces - {"code-secret"}

        async def cursor(name: str) -> str:
            self.asked["cursors"] = True
            return self.cursors.get(name, "")

        monkeypatch.setattr(code_unlinked, "configured_repos", lambda: self.repos)
        monkeypatch.setattr(
            code_unlinked,
            "code_namespace_for",
            lambda org, ns, t: next((tags[x] for x in t if x in tags), ""),
        )
        monkeypatch.setattr(code_unlinked, "readable_namespaces", readable)
        monkeypatch.setattr(code_unlinked.store, "linkable_of_namespace", served)
        monkeypatch.setattr(code_unlinked.store, "link_states", link_states)
        monkeypatch.setattr(code_unlinked.link_pass.code, "link_cursor", cursor)

    async def causes(self, keys: Any = None) -> dict[str, list[str]]:
        out = await code_unlinked.unlinked("o", "ns", keys)
        assert list(out) == list(code_unlinked.CAUSES)
        return {cause: [m["memory_key"] for m in rows] for cause, rows in out.items() if rows}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    return _World(monkeypatch)


async def test_each_unlinked_memory_that_names_code_is_counted_under_its_cause(
    world: _World,
) -> None:
    linked = _memory("linked", "`Reader.read_rows` retries twice.", tags=["repo:a"])
    unmerged = _memory("unmerged", "`Planner.plan_all` batches.", tags=["repo:a"])
    no_index = _memory("no-index", "`Reader.close` flushes.", tags=["repo:a"])
    rewritten = _memory("rewritten", "`parse` is called first.", tags=["repo:a"])
    world.memories = [
        _memory("plain", "We deploy on Tuesdays.", tags=["repo:a"]),
        # a word of the prose links only inside the memory's files: with none, not a name
        _memory("prose-word", "The read_rows path is slow.", tags=["repo:a"]),
        _memory("untagged", "`Reader.read_rows` retries twice.", tags=[]),
        linked,
        _memory("branch-file", "The new parser.", tags=["repo:a"], files=["src/new.py"]),
        _memory("prose-in-file", "The read_rows path.", tags=["repo:a"], files=["src/r.py"]),
        rewritten,
        unmerged,
        no_index,
    ]
    world.links = [
        _link(linked, "Reader.read_rows", "changed"),  # linked, whatever its state
        _link(unmerged, "Planner.plan_all", "pending"),
        _link(no_index, "*", "pending"),
        # made from an older value: not a link of the memory as it is now
        {**_link(rewritten, "parse", "fresh"), "inputs_hash": "an-older-value"},
    ]
    assert await world.causes() == {
        "no_repository": ["untagged"],
        "file_not_indexed": ["branch-file", "prose-in-file"],
        "name_unresolved": ["rewritten"],
        "pending": ["unmerged", "no-index"],
    }
    out = await code_unlinked.unlinked("o", "ns")
    assert out["pending"] == [
        {"memory_key": "unmerged", "memory_id": "id-unmerged", "waits_for": "trunk"},
        {"memory_key": "no-index", "memory_id": "id-no-index", "waits_for": "index"},
    ]
    assert out["no_repository"] == [{"memory_key": "untagged", "memory_id": "id-untagged"}]
    assert world.asked["served"] == ("o", "ns", None, code_unlinked.SCANNED)


async def test_a_memory_the_link_pass_has_not_read_waits_for_it(world: _World) -> None:
    """Written after the pass's queue position, never passed by a first pass, or not
    reached by a backfill in progress: nothing says yet that its names resolve to nothing."""
    late = _memory("late", "`Reader.open` locks.", tags=["repo:a"]) | {
        "version": (READ_TO + 1) * 1000
    }
    early = _memory("early", "`Reader.seek` rewinds.", tags=["repo:a"])
    world.memories = [late, early]
    assert await world.causes() == {"pending": ["late"], "name_unresolved": ["early"]}
    assert (await code_unlinked.unlinked("o", "ns"))["pending"][0]["waits_for"] == "link_pass"
    world.cursors = {}  # no pass has run
    assert await world.causes() == {"pending": ["late", "early"]}
    # a backfill that has read to agent `a`, key `k`: `early` sorts before it, `late` after
    world.cursors = dict(PASS) | {"backfill": '{"after": ["o", "ns", "a", "-", "-", "k"]}'}
    late["version"] = early["version"]
    assert await world.causes() == {"pending": ["late"], "name_unresolved": ["early"]}
    world.cursors["backfill"] = "{}"  # restarted: nothing read again yet
    assert await world.causes() == {"pending": ["late", "early"]}


async def test_a_repository_the_caller_may_not_read_counts_as_none(world: _World) -> None:
    """Whether a name resolves in a repository the caller may not read must not show: its
    memories read as naming no repository, and their links are never asked for."""
    secret = _memory("secret", "`Vault.open` needs a key.", tags=["repo:secret"])
    world.memories = [secret]
    world.links = [_link(secret, "Vault.open", "fresh", "code-secret")]
    assert await world.causes() == {"no_repository": ["secret"]}
    assert world.asked["links"] == [] and "cursors" not in world.asked


async def test_an_org_that_indexes_no_repository_is_not_read(world: _World) -> None:
    """With the memory plane alone no memory could be linked: nothing is read or counted."""
    world.memories = [_memory("untagged", "`Reader.read_rows` retries.", tags=[])]
    world.repos = [type("Cfg", (), {"org": "another", "namespace": "code-a"})()]
    assert await world.causes() == {} and world.asked == {}


async def test_only_the_keys_asked_for_are_read(world: _World) -> None:
    world.memories = [
        _memory("k1", "`Reader.open` locks.", tags=[]),
        _memory("k2", "`Reader.close` flushes.", tags=[]),
    ]
    assert await world.causes(["k2"]) == {"no_repository": ["k2"]}
    assert world.asked["served"][2] == ["k2"]


# ---- the statements, on clickhouse-local ------------------------------------------------


def _memory_row(key: str, status: str, version: int, *, gone: bool = False) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_id, value, tags, files, status, tombstoned_at, version) VALUES ('o', 'ns', "
        f"'a', '-', '-', '{key}', generateUUIDv4(), 'text of {key}', ['repo:a'], "
        f"['src/{key}.py'], '{status}', {'now()' if gone else 'NULL'}, {version})"
    )


@local_sql
async def test_the_served_memories_of_a_namespace_are_read_last_written_first() -> None:
    ch = clickhouse_local.LocalCH(
        clickhouse_local.ddl("memory"),
        _memory_row("old", "active", 10),
        _memory_row("new", "active", 30),
        _memory_row("new", "active", 20),  # an older version of `new`
        _memory_row("conflicted", "conflicted", 25),
        _memory_row("retired", "superseded", 40),
        _memory_row("deleted", "active", 50, gone=True),
        _memory_row("obs:tool-1", "active", 70),  # a raw tool observation: never linked
        _memory_row("elsewhere", "active", 60).replace("'ns'", "'other'"),
    )
    got = await store.linkable_of_namespace("o", "ns", limit=10, ch=ch)
    assert [(m["memory_key"], m["version"]) for m in got] == [
        ("new", 30),
        ("conflicted", 25),
        ("old", 10),
    ]
    assert got[0]["files"] == ["src/new.py"] and got[0]["tags"] == ["repo:a"]
    assert {"namespace", "agent", "user_id", "session_id", "mid", "value", "sha"} <= set(got[0])
    assert [
        m["memory_key"] for m in await store.linkable_of_namespace("o", "ns", limit=1, ch=ch)
    ] == ["new"]
    scoped = await store.linkable_of_namespace("o", "ns", ["old", "retired"], limit=10, ch=ch)
    assert [m["memory_key"] for m in scoped] == ["old"]


def _link_row(cns: str, n: int, state: str, *, gone: bool = False, version: int = 1) -> str:
    return (
        "INSERT INTO loom.memory_code_links (org, code_namespace, path, qualified_name, "
        "namespace, memory_id, memory_key, agent, state, inputs_hash, tombstoned_at, version) "
        f"VALUES ('o', '{cns}', 'src/a.py', 'f{n}', 'ns', '{_id(n)}', 'k{n}', 'a', '{state}', "
        f"'h{n}', {'now()' if gone else 'NULL'}, {version})"
    )


def _id(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _links_ch() -> clickhouse_local.LocalCH:
    return clickhouse_local.LocalCH(
        clickhouse_local.ddl("memory_code_links"),
        "SYSTEM STOP MERGES loom.memory_code_links",
        _link_row("code-a", 1, "fresh"),
        _link_row("code-a", 2, "changed"),
        _link_row("code-a", 2, "missing", version=2),  # its newest version
        _link_row("code-b", 3, "pending"),
        _link_row("code-b", 3, "pending", gone=True, version=2),  # deleted
        _link_row("code-c", 4, "fresh").replace("'ns'", "'other'"),
    )


@local_sql
async def test_link_states_are_the_live_links_of_the_memories_asked_for() -> None:
    got = await store.link_states("o", "ns", [_id(1), _id(2), _id(3), _id(4)], ch=_links_ch())
    assert sorted((a["mid"], a["code_namespace"], a["state"], a["inputs_hash"]) for a in got) == [
        (_id(1), "code-a", "fresh", "h1"),
        (_id(2), "code-a", "missing", "h2"),
    ]
    assert set(got[0]) == {"mid", "code_namespace", "qualified_name", "state", "inputs_hash"}
    assert await store.link_states("o", "ns", [], ch=_links_ch()) == []


@local_sql
async def test_the_linked_repositories_can_be_read_for_some_of_them() -> None:
    ch = _links_ch()
    # without FINAL, as the link pass reads them: code-b's deleted link still counts
    assert await store.code_namespaces(ch=ch) == [("o", "code-a"), ("o", "code-b"), ("o", "code-c")]
    among = [("o", "code-a"), ("o", "code-none"), ("another", "code-c")]
    assert await store.code_namespaces(among, ch=ch) == [("o", "code-a")]
    assert await store.code_namespaces([], ch=ch) == []
