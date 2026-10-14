"""memory.ask staleness note: built from the pool memories' code links — a memory whose
code moved, changed or is gone is named; a fresh one, or none, gives no note."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import code_grounding as ma
from server.memory.app import code_link_reads
from server.memory.store import code_links


def _mem(key: str, memory_type: str = "semantic", user: str = "-") -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key,
        memory_type=memory_type,
        namespace="",
        content_excerpt="",
        _row_identity=("a", user, "-"),
    )


def _head(key: str, user: str = "-") -> dict[str, Any]:
    return {
        "mid": f"id-{key}-{user}",
        "namespace": "agent-a",
        "memory_key": key,
        "agent": "a",
        "user_id": user,
        "session_id": "-",
    }


def _link(key: str, state: str, **kw: Any) -> dict[str, Any]:
    return {
        "memory_id": f"id-{key}-{kw.get('user', '-')}",
        "memory_key": key,
        "code_namespace": kw.get("cns", "code-a"),
        "state": state,
        "qualified_name": kw.get("qn", "DB::Reader::read"),
        "path": kw.get("path", "src/new.cpp"),
        "anchored_path": kw.get("prev", kw.get("path", "src/new.cpp")),
        "anchored_name": kw.get("prev_qn", kw.get("qn", "DB::Reader::read")),
    }


def _links(monkeypatch, heads: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
    cols = ("namespace", "agent", "user_id", "session_id", "memory_key")
    who = {h["mid"]: {c: h[c] for c in cols} for h in heads}

    async def live_links(org, idents):
        found = [who[r["memory_id"]] | r for r in rows if r["memory_id"] in who]
        return [r for r in found if tuple(r[c] for c in cols) in set(idents)]

    async def served_heads(org, keys):
        return [h for h in heads if (h["namespace"], h["memory_key"]) in set(keys)]

    async def readable(org, nss):
        return {n for n in nss if n != "code-secret"}

    monkeypatch.setattr(code_links, "live_links", live_links)
    monkeypatch.setattr(code_links, "served_heads", served_heads)
    monkeypatch.setattr("server.code.app.memory_links.code_namespaces", lambda: ["code-a"])
    monkeypatch.setattr("server.memory.app.code_link_reads.readable", readable)
    monkeypatch.setattr("server.memory.app.code_link_reads.current", lambda org, m, links: links)


_IN = SimpleNamespace(
    org="o", namespace="agent-a", scope="private", agent="*", user_id=None, session_id=None
)


@pytest.mark.asyncio
async def test_names_moved_changed_and_removed_code(monkeypatch) -> None:
    _links(
        monkeypatch,
        [_head(k) for k in ("m1", "m2", "m3")],
        [
            _link("m1", "moved", prev="src/old.cpp"),
            _link("m2", "changed"),
            _link("m3", "missing", qn="", path="docs/gone.md"),
            _link("m3", "missing", cns="code-secret"),  # a repository the caller cannot read
        ],
    )
    note = await ma._annotate_stale_code_memories([_mem("m1"), _mem("m2"), _mem("m3")], _IN)
    assert note is not None and "STALENESS" in note.content_excerpt
    text = note.content_excerpt
    assert "src/old.cpp" in text and "src/new.cpp" in text
    assert "[m2]" in text and "changed" in text and "docs/gone.md" in text
    assert "DB::Reader::read, which the code index" not in text


@pytest.mark.asyncio
async def test_a_rename_within_its_file_names_the_old_and_the_new_symbol(monkeypatch) -> None:
    renamed = _link("m1", "moved", qn="reader::load_rows", path="reader.py", prev="reader.py")
    renamed["anchored_name"] = "reader::fetch_rows"
    _links(monkeypatch, [_head("m1")], [renamed])
    note = await ma._annotate_stale_code_memories([_mem("m1")], _IN)
    assert note is not None
    assert "names reader::fetch_rows; it is now reader::load_rows" in note.content_excerpt
    assert "it now lives in" not in note.content_excerpt


@pytest.mark.asyncio
async def test_two_renames_name_what_the_memory_was_written_against(monkeypatch) -> None:
    renamed = _link("m1", "moved", qn="n::retrieve_rows", path="src/n.py", prev="src/m.py")
    renamed["anchored_name"] = "n::fetch_rows"
    renamed["former"] = [
        {"path": "src/m.py", "name": "n::fetch_rows"},
        {"path": "src/l.py", "name": "n::load_rows"},
    ]
    _links(monkeypatch, [_head("m1")], [renamed])
    note = await ma._annotate_stale_code_memories([_mem("m1")], _IN)
    assert note is not None
    text = note.content_excerpt
    assert "names n::fetch_rows; it is now n::retrieve_rows" in text
    assert "places n::retrieve_rows in src/m.py; it now lives in src/n.py" in text
    assert "load_rows" not in text and "src/l.py" not in text


@pytest.mark.asyncio
async def test_a_file_move_names_the_files_not_a_rename(monkeypatch) -> None:
    moved = _link("m1", "moved", qn="loader::fetch_rows", path="loader.py", prev="reader.py")
    moved["anchored_name"] = "reader::fetch_rows"  # the module part follows the file
    _links(monkeypatch, [_head("m1")], [moved])
    note = await ma._annotate_stale_code_memories([_mem("m1")], _IN)
    assert note is not None and "it now lives in loader.py" in note.content_excerpt
    assert "names reader::fetch_rows" not in note.content_excerpt


@pytest.mark.asyncio
async def test_fresh_links_and_injected_code_facts_give_no_note(monkeypatch) -> None:
    _links(monkeypatch, [_head("m1"), _head("pr")], [_link("m1", "fresh"), _link("pr", "missing")])
    assert await ma._annotate_stale_code_memories([_mem("m1"), _mem("pr", "code")], _IN) is None


@pytest.mark.asyncio
async def test_another_users_memory_under_the_same_key_lends_it_no_state(monkeypatch) -> None:
    _links(monkeypatch, [_head("lunch", "bob")], [_link("lunch", "missing", user="bob")])
    assert await ma._annotate_stale_code_memories([_mem("lunch", user="alice")], _IN) is None


@pytest.mark.asyncio
async def test_an_unreadable_link_table_gives_no_note(monkeypatch) -> None:
    async def broken(org, idents):
        raise RuntimeError("down")

    monkeypatch.setattr(code_links, "live_links", broken)
    assert await ma._annotate_stale_code_memories([_mem("m1")], _IN) is None


@pytest.mark.asyncio
async def test_only_the_hits_the_search_left_unread_are_read(monkeypatch) -> None:
    _links(monkeypatch, [_head("m1"), _head("m2")], [_link("m2", "missing", qn="DB::gone")])
    asked: list[Any] = []
    read = code_link_reads.hit_memories

    async def hit_memories(input, hits):
        asked.extend(h.memory_key for h in hits)
        return await read(input, hits)

    monkeypatch.setattr(code_link_reads, "hit_memories", hit_memories)
    searched = _mem("m1")
    searched._code_links = [_link("m1", "changed", qn="DB::edited")]
    note = await ma._annotate_stale_code_memories([searched, _mem("m2")], _IN)
    assert asked == ["m2"]
    assert "DB::edited" in note.content_excerpt and "DB::gone" in note.content_excerpt
