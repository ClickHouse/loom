"""The Claude memory tool over Loom documents: every command against a fake
knowledge store, async and sync, the /memories root, stores, and what a real
Agent posts."""

from __future__ import annotations

import asyncio
import importlib
import sys
from typing import Any

import pytest
from anthropic.lib.tools import BetaAbstractMemoryTool, BetaAsyncAbstractMemoryTool, ToolError
from loom import Agent, SyncAgent
from loom.integrations.claude_memory import AsyncLoomMemoryTool, LoomMemoryTool


class _Docs:
    """knowledge.* over a dict: source_uri -> (text, tags), paged two rows at a time."""

    def __init__(self) -> None:
        self.docs: dict[str, tuple[str, list[str]]] = {}
        self.calls: list[str] = []

    async def get(self, source_uri: str) -> dict[str, Any] | None:
        self.calls.append("get")
        doc = self.docs.get(source_uri)
        return None if doc is None else {"found": True, "content": doc[0], "source_uri": source_uri}

    async def upsert(self, content: str, *, source_uri: str, tags: list[str]) -> dict[str, Any]:
        self.calls.append("upsert")
        self.docs[source_uri] = (content, list(tags))
        return {}

    async def list(
        self, *, tags: list[str], limit: int, cursor: str | None = None
    ) -> dict[str, Any]:
        self.calls.append("list")
        rows = sorted(u for u, (_, t) in self.docs.items() if set(t) & set(tags))
        start = int(cursor or 0)
        page = rows[start : start + 2]
        more = start + 2 < len(rows)
        return {
            "rows": [{"source_uri": u} for u in page],
            "next_cursor": str(start + 2) if more else None,
        }

    async def bulk_delete(self, source_uris: list[str]) -> dict[str, Any]:
        self.calls.append("bulk_delete")
        for uri in source_uris:
            self.docs.pop(uri, None)
        return {}


class _SyncDocs:
    def __init__(self, docs: _Docs) -> None:
        self._docs = docs

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._docs, name)
        return lambda *a, **kw: asyncio.run(method(*a, **kw))


class _Loom:
    def __init__(self, docs: _Docs) -> None:
        self.knowledge = docs


def _async_tool(docs: _Docs, store: str = "default") -> AsyncLoomMemoryTool:
    return AsyncLoomMemoryTool(_Loom(docs), store=store)  # type: ignore[arg-type]


def _sync_tool(docs: _Docs, store: str = "default") -> LoomMemoryTool:
    agent = object.__new__(SyncAgent)
    agent.__dict__["knowledge"] = _SyncDocs(docs)
    return LoomMemoryTool(agent, store=store)


Runner = Any


@pytest.fixture(params=["async", "sync"])
def run(request: pytest.FixtureRequest) -> Any:
    """A function running one command (its input as Claude sends it) on a tool over
    `docs`, through the tool's own dispatch."""

    def go(docs: _Docs, command: dict[str, Any], store: str = "default") -> Any:
        if request.param == "async":
            return asyncio.run(_async_tool(docs, store).call(command))
        return _sync_tool(docs, store).call(command)

    return go


def test_the_tools_are_the_sdks_memory_tools() -> None:
    assert issubclass(AsyncLoomMemoryTool, BetaAsyncAbstractMemoryTool)
    assert issubclass(LoomMemoryTool, BetaAbstractMemoryTool)
    assert _async_tool(_Docs()).to_dict() == {"type": "memory_20250818", "name": "memory"}


def test_create_then_view_a_file_with_line_numbers(run: Runner) -> None:
    docs = _Docs()
    assert run(
        docs, {"command": "create", "path": "/memories/notes.md", "file_text": "a\nb\nc"}
    ) == ("File created successfully at: /memories/notes.md")
    assert docs.docs["loom-memory://default/memories/notes.md"] == (
        "a\nb\nc",
        ["claude-memory:default"],
    )
    assert run(docs, {"command": "view", "path": "/memories/notes.md"}) == (
        "Here's the content of /memories/notes.md with line numbers:\n"
        "     1\ta\n     2\tb\n     3\tc"
    )
    ranged = run(docs, {"command": "view", "path": "/memories/notes.md", "view_range": [2, -1]})
    assert ranged.endswith("     2\tb\n     3\tc")
    with pytest.raises(ToolError, match="already exists"):
        run(docs, {"command": "create", "path": "/memories/notes.md", "file_text": "x"})


def test_view_a_directory_two_levels_deep_across_pages(run: Runner) -> None:
    docs = _Docs()
    for path in ("/memories/a.md", "/memories/p/b.md", "/memories/p/q/c.md", "/memories/.hidden"):
        run(docs, {"command": "create", "path": path, "file_text": "x"})
    assert run(docs, {"command": "view", "path": "/memories"}) == (
        "Here're the files and directories up to 2 levels deep in /memories, "
        "excluding hidden items:\n"
        "/memories\n/memories/a.md\n/memories/p/\n/memories/p/b.md\n/memories/p/q/"
    )
    assert run(docs, {"command": "view", "path": "/memories/p"}).endswith(
        "/memories/p\n/memories/p/b.md\n/memories/p/q/\n/memories/p/q/c.md"
    )
    with pytest.raises(ToolError, match="does not exist"):
        run(docs, {"command": "view", "path": "/memories/none"})


def test_str_replace_edits_one_occurrence_and_refuses_none_or_many(run: Runner) -> None:
    docs = _Docs()
    run(docs, {"command": "create", "path": "/memories/n.md", "file_text": "one\ntwo\nthree\ntwo"})
    with pytest.raises(ToolError, match="did not appear verbatim"):
        run(
            docs,
            {"command": "str_replace", "path": "/memories/n.md", "old_str": "four", "new_str": "4"},
        )
    with pytest.raises(ToolError, match="Multiple occurrences of old_str `two` in lines: 2, 4"):
        run(
            docs,
            {"command": "str_replace", "path": "/memories/n.md", "old_str": "two", "new_str": "2"},
        )
    answer = run(
        docs,
        {"command": "str_replace", "path": "/memories/n.md", "old_str": "three", "new_str": "3"},
    )
    assert answer.startswith("The memory file has been edited.")
    assert "     3\t3" in answer
    assert docs.docs["loom-memory://default/memories/n.md"][0] == "one\ntwo\n3\ntwo"
    with pytest.raises(ToolError, match="does not exist"):
        run(
            docs,
            {"command": "str_replace", "path": "/memories/x.md", "old_str": "a", "new_str": "b"},
        )


def test_insert_adds_a_line_and_checks_its_range(run: Runner) -> None:
    docs = _Docs()
    run(docs, {"command": "create", "path": "/memories/n.md", "file_text": "a\nc\n"})
    assert run(
        docs,
        {"command": "insert", "path": "/memories/n.md", "insert_line": 1, "insert_text": "b\n"},
    ) == ("The file /memories/n.md has been edited.")
    assert docs.docs["loom-memory://default/memories/n.md"][0] == "a\nb\nc\n"
    with pytest.raises(ToolError, match=r"within the range \[0, 3\]"):
        run(
            docs,
            {"command": "insert", "path": "/memories/n.md", "insert_line": 9, "insert_text": "z"},
        )


def test_delete_a_file_or_a_directory(run: Runner) -> None:
    docs = _Docs()
    for path in ("/memories/a.md", "/memories/p/b.md", "/memories/p/c.md", "/memories/pq.md"):
        run(docs, {"command": "create", "path": path, "file_text": "x"})
    assert (
        run(docs, {"command": "delete", "path": "/memories/a.md"})
        == "Successfully deleted /memories/a.md"
    )
    run(docs, {"command": "delete", "path": "/memories/p"})
    assert sorted(docs.docs) == [
        "loom-memory://default/memories/pq.md"
    ]  # /p is not a prefix of /pq
    with pytest.raises(ToolError, match="does not exist"):
        run(docs, {"command": "delete", "path": "/memories/p"})
    with pytest.raises(ToolError, match="itself"):
        run(docs, {"command": "delete", "path": "/memories"})


def test_rename_a_file_or_a_directory(run: Runner) -> None:
    docs = _Docs()
    for path in ("/memories/a.md", "/memories/p/b.md", "/memories/p/q/c.md"):
        run(docs, {"command": "create", "path": path, "file_text": path})
    assert run(
        docs, {"command": "rename", "old_path": "/memories/a.md", "new_path": "/memories/z.md"}
    ) == ("Successfully renamed /memories/a.md to /memories/z.md")
    run(docs, {"command": "rename", "old_path": "/memories/p", "new_path": "/memories/r"})
    assert {u: d[0] for u, d in docs.docs.items()} == {
        "loom-memory://default/memories/z.md": "/memories/a.md",
        "loom-memory://default/memories/r/b.md": "/memories/p/b.md",
        "loom-memory://default/memories/r/q/c.md": "/memories/p/q/c.md",
    }
    with pytest.raises(ToolError, match="already exists"):
        run(docs, {"command": "rename", "old_path": "/memories/z.md", "new_path": "/memories/r"})
    with pytest.raises(ToolError, match="does not exist"):
        run(docs, {"command": "rename", "old_path": "/memories/none", "new_path": "/memories/y"})
    with pytest.raises(ToolError, match="itself"):
        run(docs, {"command": "rename", "old_path": "/memories", "new_path": "/memories/y"})


def test_every_path_stays_under_the_memories_root(run: Runner) -> None:
    docs = _Docs()
    for path in ("/etc/passwd", "/memoriesx/a", "memories/a"):
        with pytest.raises(ToolError, match="must start with /memories"):
            run(docs, {"command": "create", "path": path, "file_text": "x"})
    with pytest.raises(ToolError, match="would escape"):
        run(docs, {"command": "create", "path": "/memories/../secret", "file_text": "x"})
    run(docs, {"command": "create", "path": "/memories/a/../b.md", "file_text": "x"})
    assert list(docs.docs) == ["loom-memory://default/memories/b.md"]
    assert docs.calls == ["get", "list", "upsert"]  # the refused paths called nothing
    # Back under the root once resolved, as POSIX normpath reads it.
    run(docs, {"command": "create", "path": "/memories/a/../../memories/c.md", "file_text": "x"})
    assert "loom-memory://default/memories/c.md" in docs.docs


def test_a_store_sees_only_its_own_files(run: Runner) -> None:
    docs = _Docs()
    run(docs, {"command": "create", "path": "/memories/a.md", "file_text": "alice"}, store="alice")
    run(docs, {"command": "create", "path": "/memories/b.md", "file_text": "bob"}, store="bob")
    assert run(docs, {"command": "view", "path": "/memories"}, store="alice").endswith(
        "/memories\n/memories/a.md"
    )
    with pytest.raises(ToolError, match="does not exist"):
        run(docs, {"command": "view", "path": "/memories/b.md"}, store="alice")


def test_it_refuses_a_bad_store_and_the_other_agent_kind() -> None:
    for store in ("", "a/b", " a"):
        with pytest.raises(ValueError, match="store"):
            _async_tool(_Docs(), store=store)
    with pytest.raises(TypeError, match="LoomMemoryTool"):
        AsyncLoomMemoryTool(object.__new__(SyncAgent))
    with pytest.raises(TypeError, match="AsyncLoomMemoryTool"):
        LoomMemoryTool(_Loom(_Docs()))  # type: ignore[arg-type]


def test_without_anthropic_the_import_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "anthropic.lib.tools", None)
    monkeypatch.delitem(sys.modules, "loom.integrations.claude_memory")
    with pytest.raises(ImportError, match=r"clickhouse-loom\[claude-memory\]"):
        importlib.import_module("loom.integrations.claude_memory")


def test_a_real_agent_posts_knowledge_ops() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []

    async def post(path: str, payload: dict[str, Any], *a: Any, **kw: Any) -> dict[str, Any]:
        posts.append((path, dict(payload)))
        return {"found": False, "rows": [], "next_cursor": None}

    agent = Agent(org="acme", namespace="travel", agent="bot", base_url="http://x", api_key="k")
    agent.transport.post = post  # type: ignore[method-assign]
    tool = AsyncLoomMemoryTool(agent, store="alice")
    asyncio.run(tool.call({"command": "create", "path": "/memories/a.md", "file_text": "hi"}))
    assert [p for p, _ in posts] == [
        "/v1/knowledge.get",
        "/v1/knowledge.list",
        "/v1/knowledge.upsert",
    ]
    get, listing, upsert = (body for _, body in posts)
    assert listing["tags"] == ["claude-memory:alice"]
    assert get["source_uri"] == upsert["source_uri"] == "loom-memory://alice/memories/a.md"
    assert (upsert["content"], upsert["tags"]) == ("hi", ["claude-memory:alice"])


def test_an_empty_old_str_is_refused(run: Runner) -> None:
    docs = _Docs()
    run(docs, {"command": "create", "path": "/memories/n.md", "file_text": "abc"})
    with pytest.raises(ToolError, match="old_str is empty"):
        run(
            docs,
            {"command": "str_replace", "path": "/memories/n.md", "old_str": "", "new_str": "x"},
        )


def test_hidden_files_are_left_out_at_every_level(run: Runner) -> None:
    docs = _Docs()
    for path in ("/memories/p/.secret", "/memories/.d/a.md", "/memories/p/b.md"):
        run(docs, {"command": "create", "path": path, "file_text": "x"})
    assert run(docs, {"command": "view", "path": "/memories"}).endswith(
        "/memories\n/memories/p/\n/memories/p/b.md"
    )


def test_create_on_a_directory_is_refused(run: Runner) -> None:
    docs = _Docs()
    run(docs, {"command": "create", "path": "/memories/p/b.md", "file_text": "x"})
    with pytest.raises(ToolError, match="already exists"):
        run(docs, {"command": "create", "path": "/memories/p", "file_text": "x"})


def test_a_large_directory_is_deleted_in_batches_the_server_takes(run: Runner) -> None:
    docs = _Docs()
    for i in range(501):
        docs.docs[f"loom-memory://default/memories/big/{i:04d}.md"] = (
            "x",
            ["claude-memory:default"],
        )
    sizes: list[int] = []
    bulk_delete = docs.bulk_delete

    async def counted(source_uris: list[str]) -> dict[str, Any]:
        sizes.append(len(source_uris))
        return await bulk_delete(source_uris)

    docs.bulk_delete = counted  # type: ignore[method-assign]
    run(docs, {"command": "delete", "path": "/memories/big"})
    assert sizes == [500, 1] and docs.docs == {}
