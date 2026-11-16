"""Loom as the storage of the Claude API memory tool.

The memory tool (``memory_20250818``) lets Claude keep files under ``/memories``
across conversations; the application runs its commands. ``AsyncLoomMemoryTool``
(over a ``loom.Agent``) and ``LoomMemoryTool`` (over a ``loom.SyncAgent``) run
them against Loom: each file is a Loom document, so every agent of the namespace
reads the same files and ``knowledge.search`` finds what they say::

    import anthropic
    from loom import Agent
    from loom.integrations.claude_memory import AsyncLoomMemoryTool

    client = anthropic.AsyncAnthropic()
    async with Agent(org="acme", namespace="support") as loom_agent:
        memory = AsyncLoomMemoryTool(loom_agent, store="alice")
        runner = client.beta.messages.tool_runner(
            model="claude-opus-5-5",
            max_tokens=16000,
            tools=[memory],
            messages=[{"role": "user", "content": "Remember that I prefer aisle seats"}],
        )
        async for message in runner:
            ...

Install with ``pip install 'clickhouse-loom[claude-memory]'``.
"""

from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING, Any

try:
    from anthropic.lib.tools import (
        BetaAbstractMemoryTool,
        BetaAsyncAbstractMemoryTool,
        ToolError,
    )
    from anthropic.types.beta import (
        BetaMemoryTool20250818CreateCommand,
        BetaMemoryTool20250818DeleteCommand,
        BetaMemoryTool20250818InsertCommand,
        BetaMemoryTool20250818RenameCommand,
        BetaMemoryTool20250818StrReplaceCommand,
        BetaMemoryTool20250818ViewCommand,
    )
except ImportError as e:  # the SDK is an optional extra
    raise ImportError(
        "loom.integrations.claude_memory needs anthropic: "
        "pip install 'clickhouse-loom[claude-memory]'"
    ) from e

from loom.sync import SyncAgent

if TYPE_CHECKING:
    from loom.agent import Agent

ROOT = "/memories"
_LINE_WIDTH = 6  # the SDK's local memory tool numbers lines to 999,999
_PAGE = 500  # knowledge.list's cap on rows a page
_DELETE_BATCH = 500  # knowledge.bulk_delete's cap on URIs a call


class _Store:
    """The documents of one store: path <-> source URI, and its tag."""

    def __init__(self, store: str) -> None:
        if not store or "/" in store or store != store.strip():
            raise ValueError(f"store is one non-empty path segment, got {store!r}")
        self.tag = f"claude-memory:{store}"
        self._prefix = f"loom-memory://{store}"

    def uri(self, path: str) -> str:
        return self._prefix + path

    def path(self, uri: str) -> str | None:
        return uri[len(self._prefix) :] if uri.startswith(self._prefix + ROOT) else None


def _checked(path: str) -> str:
    """`path` normalised under /memories; ToolError when it is not under it."""
    if path != ROOT and not path.startswith(ROOT + "/"):
        raise ToolError(f"Path must start with /memories, got: {path}")
    normal = posixpath.normpath(path)
    if normal != ROOT and not normal.startswith(ROOT + "/"):
        raise ToolError(f"Path {path} would escape /memories directory")
    return normal


def _listing(shown: str, directory: str, paths: list[str]) -> str:
    """A directory view of `paths` (the files under `directory`): its files and
    subdirectories two levels deep, as the SDK's local memory tool lists them (no
    sizes: a document's size is not listed)."""
    entries: set[str] = set()
    for path in paths:
        parts = path[len(directory) + 1 :].split("/")
        for depth in range(1, min(len(parts), 2) + 1):
            entries.add("/".join(parts[:depth]) + ("/" if depth < len(parts) else ""))
    header = (
        f"Here're the files and directories up to 2 levels deep in {shown}, excluding hidden items:"
    )
    visible = [e for e in sorted(entries) if not any(part.startswith(".") for part in e.split("/"))]
    lines = [shown] + [f"{shown.rstrip('/')}/{e}" for e in visible]
    return header + "\n" + "\n".join(lines)


def _numbered(path: str, text: str, view_range: list[int] | None) -> str:
    lines = text.split("\n")
    start, shown = 1, lines
    if view_range and len(view_range) == 2:
        start = max(1, view_range[0])
        end = len(lines) if view_range[1] == -1 else view_range[1]
        shown = lines[start - 1 : end]
    body = "\n".join(f"{str(i).rjust(_LINE_WIDTH)}\t{line}" for i, line in enumerate(shown, start))
    return f"Here's the content of {path} with line numbers:\n" + body


def _replaced(path: str, text: str, old: str, new: str) -> tuple[str, str]:
    """(new text, the tool's answer) for a str_replace."""
    if not old:
        raise ToolError(f"No replacement was performed, old_str is empty in {path}.")
    count = text.count(old)
    if count == 0:
        raise ToolError(
            f"No replacement was performed, old_str `{old}` did not appear verbatim in {path}."
        )
    if count > 1:
        lines, start = [], 0
        while (pos := text.find(old, start)) != -1:
            lines.append(text[:pos].count("\n") + 1)
            start = pos + 1
        raise ToolError(
            f"No replacement was performed. Multiple occurrences of old_str `{old}` in lines: "
            f"{', '.join(map(str, lines))}. Please ensure it is unique"
        )
    changed = text[: text.find(old)].count("\n")
    updated = text.replace(old, new)
    rows = updated.split("\n")
    snippet = "\n".join(
        f"{str(n).rjust(_LINE_WIDTH)}\t{rows[n - 1]}"
        for n in range(max(0, changed - 2) + 1, min(len(rows), changed + 3) + 1)
    )
    return updated, (
        "The memory file has been edited. Here is the snippet showing the change "
        "(with line numbers):\n" + snippet
    )


def _inserted(text: str, line: int, insert: str) -> str:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if line < 0 or line > len(lines):
        raise ToolError(
            f"Invalid `insert_line` parameter: {line}. "
            f"It should be within the range [0, {len(lines)}]."
        )
    lines.insert(line, insert.rstrip("\n"))
    return "\n".join(lines) + "\n"


def _missing(path: str) -> ToolError:
    return ToolError(f"The path {path} does not exist. Please provide a valid path.")


def _ancestors(path: str) -> list[str]:
    """The directories above `path`, below /memories."""
    parts = path[len(ROOT) + 1 :].split("/")[:-1]
    return [ROOT + "/" + "/".join(parts[: n + 1]) for n in range(len(parts))]


class AsyncLoomMemoryTool(BetaAsyncAbstractMemoryTool):
    """The memory tool's commands over a ``loom.Agent``'s documents, for the async
    tool runner. ``store`` names the file tree: one per application, or per user."""

    def __init__(self, agent: Agent, *, store: str = "default", **kwargs: Any) -> None:
        if isinstance(agent, SyncAgent):
            raise TypeError(
                "AsyncLoomMemoryTool needs a loom.Agent; LoomMemoryTool takes a SyncAgent"
            )
        super().__init__(**kwargs)
        self._agent = agent
        self._store = _Store(store)

    async def _under(self, directory: str, limit: int | None = None) -> list[str]:
        """The files under `directory`, sorted; at most `limit` when given (any of them)."""
        paths: list[str] = []
        cursor: str | None = None
        while True:
            page = await self._agent.knowledge.list(
                tags=[self._store.tag],
                uri_prefix=[self._store.uri(directory) + "/"],
                fields=["source_uri"],
                limit=limit or _PAGE,
                cursor=cursor,
            )
            paths += [p for r in page["rows"] if (p := self._store.path(r["source_uri"]))]
            cursor = page.get("next_cursor")
            if not cursor or limit:
                return sorted(paths)

    async def _text(self, path: str) -> str | None:
        doc = await self._agent.knowledge.get(self._store.uri(path))
        return None if doc is None else str(doc.get("content") or "")

    async def _write(self, path: str, text: str) -> None:
        await self._agent.knowledge.upsert(
            text, source_uri=self._store.uri(path), tags=[self._store.tag]
        )

    async def _delete(self, paths: list[str]) -> None:
        uris = [self._store.uri(p) for p in paths]
        for start in range(0, len(uris), _DELETE_BATCH):
            await self._agent.knowledge.bulk_delete(uris[start : start + _DELETE_BATCH])

    async def _file(self, shown: str, path: str) -> str:
        """The text of the file `path`; ToolError when it is a directory or nothing."""
        text = await self._text(path)
        if text is not None:
            return text
        if await self._under(path, limit=1):
            raise ToolError(f"The path {shown} is not a file.")
        raise _missing(shown)

    async def _no_file_above(self, path: str) -> None:
        """ToolError when a directory above `path` is a file."""
        for above in _ancestors(path):
            if await self._text(above) is not None:
                raise ToolError(f"The path {above} is a file, not a directory")

    async def view(self, command: BetaMemoryTool20250818ViewCommand) -> str:
        path = _checked(command.path)
        text = None if path == ROOT else await self._text(path)
        if text is not None:
            return _numbered(command.path, text, command.view_range)
        paths = await self._under(path)
        if path != ROOT and not paths:
            raise _missing(command.path)
        return _listing(command.path, path, paths)

    async def create(self, command: BetaMemoryTool20250818CreateCommand) -> str:
        path = _checked(command.path)
        if path == ROOT or await self._text(path) is not None or await self._under(path, limit=1):
            raise ToolError(f"File {command.path} already exists")
        await self._no_file_above(path)
        await self._write(path, command.file_text)
        return f"File created successfully at: {command.path}"

    async def str_replace(self, command: BetaMemoryTool20250818StrReplaceCommand) -> str:
        path = _checked(command.path)
        text = await self._file(command.path, path)
        updated, answer = _replaced(command.path, text, command.old_str, command.new_str)
        await self._write(path, updated)
        return answer

    async def insert(self, command: BetaMemoryTool20250818InsertCommand) -> str:
        path = _checked(command.path)
        text = await self._file(command.path, path)
        await self._write(path, _inserted(text, command.insert_line, command.insert_text))
        return f"The file {command.path} has been edited."

    async def delete(self, command: BetaMemoryTool20250818DeleteCommand) -> str:
        path = _checked(command.path)
        if path == ROOT:
            raise ToolError("Cannot delete the /memories directory itself")
        doomed = [path] if await self._text(path) is not None else await self._under(path)
        if not doomed:
            raise ToolError(f"The path {command.path} does not exist")
        await self._delete(doomed)
        return f"Successfully deleted {command.path}"

    async def rename(self, command: BetaMemoryTool20250818RenameCommand) -> str:
        old, new = _checked(command.old_path), _checked(command.new_path)
        if old == ROOT:
            raise ToolError("Cannot rename the /memories directory itself")
        if new == ROOT or await self._text(new) is not None or await self._under(new, limit=1):
            raise ToolError(f"The destination {command.new_path} already exists")
        await self._no_file_above(new)
        text = await self._text(old)
        if text is not None:
            await self._write(new, text)
            await self._delete([old])
        else:
            sources = await self._under(old)
            if not sources:
                raise ToolError(f"The path {command.old_path} does not exist")
            for source in sources:
                moved = await self._text(source)
                if moved is not None:
                    await self._write(new + source[len(old) :], moved)
            await self._delete(sources)
        return f"Successfully renamed {command.old_path} to {command.new_path}"


class LoomMemoryTool(BetaAbstractMemoryTool):
    """The memory tool's commands over a ``loom.SyncAgent``'s documents, for the sync
    tool runner: ``AsyncLoomMemoryTool`` run on the SyncAgent's loop, the same files."""

    def __init__(self, agent: SyncAgent, *, store: str = "default", **kwargs: Any) -> None:
        if not isinstance(agent, SyncAgent):
            raise TypeError(
                "LoomMemoryTool needs a loom.SyncAgent; AsyncLoomMemoryTool takes an Agent"
            )
        super().__init__(**kwargs)
        self._sync = agent
        self._tool = AsyncLoomMemoryTool(agent._agent, store=store)

    def view(self, command: BetaMemoryTool20250818ViewCommand) -> str:
        return str(self._sync._run(self._tool.view(command)))

    def create(self, command: BetaMemoryTool20250818CreateCommand) -> str:
        return str(self._sync._run(self._tool.create(command)))

    def str_replace(self, command: BetaMemoryTool20250818StrReplaceCommand) -> str:
        return str(self._sync._run(self._tool.str_replace(command)))

    def insert(self, command: BetaMemoryTool20250818InsertCommand) -> str:
        return str(self._sync._run(self._tool.insert(command)))

    def delete(self, command: BetaMemoryTool20250818DeleteCommand) -> str:
        return str(self._sync._run(self._tool.delete(command)))

    def rename(self, command: BetaMemoryTool20250818RenameCommand) -> str:
        return str(self._sync._run(self._tool.rename(command)))
