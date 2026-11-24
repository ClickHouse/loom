"""The reads behind a code read's lessons narrow by the sort key, and still judge each row
by its newest version: a link moved away, a link moved in, a deleted link and a memory id
a restore replaced read as they do under FINAL alone. Real SQL on clickhouse-local, with
merges stopped so every version stays in its own part."""

from __future__ import annotations

import pytest

from server.memory.store import code_links, code_plane_reads
from tests.unit import clickhouse_local

pytestmark = [
    pytest.mark.skipif(
        clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed"
    ),
    pytest.mark.asyncio,
]

P = "src/a.py"
ID = {name: f"00000000-0000-0000-0000-00000000000{n}" for n, name in enumerate("abcdefg", 1)}


def _link(
    name: str,
    version: int,
    *,
    path: str = "src/z.py",
    anchored: str = "",
    former: str = "",
    gone: bool = False,
) -> str:
    former_rows = f"[('{former}', 'f', '', '', '', '')]" if former else "[]"
    return (
        "INSERT INTO loom.memory_code_links (org, code_namespace, path, qualified_name, "
        "namespace, memory_id, memory_key, agent, state, anchored_path, former, "
        "tombstoned_at, version) VALUES ('o', 'c', "
        f"'{path}', 'f', 'n', '{ID[name]}', 'k-{name}', 'a', 'fresh', '{anchored}', "
        f"{former_rows}, {'now()' if gone else 'NULL'}, {version})"
    )


def _links_ch(*rows: str) -> clickhouse_local.LocalCH:
    return clickhouse_local.LocalCH(
        clickhouse_local.ddl("memory_code_links"),
        "SYSTEM STOP MERGES loom.memory_code_links",
        *rows,
    )


async def test_a_link_is_found_by_its_anchor_or_former_path_as_its_newest_version_has_it() -> None:
    ch = _links_ch(
        _link("a", 1, anchored=P),
        _link("a", 2, anchored="src/b.py"),  # moved away: not here
        _link("b", 1, anchored="src/b.py"),
        _link("b", 2, anchored=P),  # moved in
        _link("c", 1, anchored=P),
        _link("c", 2, anchored=P, gone=True),  # deleted
        _link("d", 1, anchored=P),  # here all along
        _link("e", 1, former=P),
        _link("e", 2),  # its former path dropped
        _link("f", 1, former=P),  # moved from here
        _link("g", 1, path=P),  # on the path itself
    )
    got = await code_links.for_paths("o", ["c"], [P], include_former=True, namespaces=["n"], ch=ch)
    assert sorted(r["memory_key"] for r in got) == ["k-b", "k-d", "k-f", "k-g"]
    assert {r["memory_key"]: r["version"] for r in got}["k-b"] == 2


async def test_the_reads_off_the_sort_key_take_their_keys_from_the_index_first() -> None:
    reads: list[str] = []

    class _CH:
        async def query_all(self, sql: str, params: object = None) -> list:
            reads.append(sql)
            return []

    await code_links.for_paths("o", ["c"], [P], include_former=True, ch=_CH())
    by_key = f"({code_links._KEY}) IN (SELECT {code_links._KEY} FROM loom.memory_code_links WHERE"
    column = {
        sql: next(c for c in ("anchored_path", "former.path", "path") if c in sql.split(by_key)[0])
        for sql in reads
    }
    # path is in the sort key: FINAL narrows by it alone
    assert {column[sql]: by_key in sql for sql in reads} == {
        "anchored_path": True,
        "former.path": True,
        "path": False,
    }


def _memory(key: str, mid: str, version: int, *, gone: bool = False, status: str = "active") -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_id, value, status, tombstoned_at, version) VALUES "
        f"('o', 'n', 'a', '-', '-', '{key}', '{mid}', '\"text of {key}\"', '{status}', "
        f"{'now()' if gone else 'NULL'}, {version})"
    )


def _memory_ch(*rows: str) -> clickhouse_local.LocalCH:
    return clickhouse_local.LocalCH(
        clickhouse_local.ddl("memory"), "SYSTEM STOP MERGES loom.memory", *rows
    )


def _named(key: str, mid: str) -> code_links.LinkedMemory:
    return ("n", "a", "-", "-", key, mid)


async def test_a_linked_memory_is_read_by_its_identity_while_it_holds_the_linked_id() -> None:
    ch = _memory_ch(
        _memory("k1", ID["a"], 1),  # served
        _memory("k2", ID["b"], 1),
        _memory("k2", ID["c"], 2),  # a restore put another id under the key
        _memory("k3", ID["d"], 1),
        _memory("k3", ID["d"], 2, gone=True),  # deleted
    )
    linked = [_named("k1", ID["a"]), _named("k2", ID["b"]), _named("k3", ID["d"])]
    heads = await code_links.heads_by_identity("o", linked, ch=ch)
    # the deleted head is read, and the links of a deleted memory are dropped after
    assert sorted((h["memory_key"], h["tombstoned_at"] is not None) for h in heads) == [
        ("k1", False),
        ("k3", True),
    ]
    served = await code_plane_reads.served_excerpts("o", ["n"], linked, ch=ch)
    assert [(r["memory_key"], r["excerpt"]) for r in served] == [("k1", "text of k1")]


async def test_no_memory_named_reads_nothing() -> None:
    class _CH:
        async def query_all(self, sql: str, params: object = None) -> list:
            raise AssertionError("read")

    assert await code_links.heads_by_identity("o", [], ch=_CH()) == []
    assert await code_plane_reads.served_excerpts("o", ["n"], [], ch=_CH()) == []
