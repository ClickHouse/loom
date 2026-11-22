"""Repository reads and implementation edits without shell or Git capabilities."""

from __future__ import annotations

import difflib
from pathlib import Path

from examples.software_factory.factory.model import Task, digest


class Checkout:
    def __init__(self, root: Path, task: Task):
        self.root = root.resolve()
        self.original = dict(task.files)
        self.read_only = dict(task.read_only_files)
        if self.original.keys() & self.read_only.keys():
            raise ValueError("Read-only evidence overlaps editable source")
        self.root.mkdir(parents=True, exist_ok=False)
        for name, content in {**self.original, **self.read_only}.items():
            path = self.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def path(self, name: str) -> Path:
        if name not in self.original and name not in self.read_only:
            raise PermissionError("Repository file is outside the task allowlist")
        if any(p.startswith(".") for p in Path(name).parts):
            raise PermissionError("Repository metadata is outside the task allowlist")
        candidate = self.root / name
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.root):
            raise PermissionError("Source path escapes checkout")
        return candidate

    def full_read(self, name: str) -> str:
        path = self.path(name)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Source budget exceeded")
        return path.read_text()

    def read(self, name: str, start_line: int = 1, start_column: int = 0) -> str:
        if type(start_line) is not int or start_line < 1:
            raise ValueError("start_line must be a positive integer")
        if type(start_column) is not int or start_column < 0:
            raise ValueError("start_column must be a nonnegative integer")
        lines = self.full_read(name).splitlines(True)[start_line - 1 :]
        if start_column > (len(lines[0].rstrip("\r\n")) if lines else 0):
            raise ValueError("start_column exceeds the source line")
        return "".join(lines)[start_column:][:16000]

    def files(self) -> dict[str, str]:
        return {name: self.full_read(name) for name in self.original}

    def readable_files(self) -> dict[str, str]:
        return {name: self.full_read(name) for name in sorted({**self.original, **self.read_only})}

    def list_files(self, prefix: str = "", offset: int = 0) -> dict:
        if not isinstance(prefix, str) or type(offset) is not int or offset < 0:
            raise ValueError("Use a text prefix and nonnegative integer offset")
        names = sorted(
            name for name in {**self.original, **self.read_only} if name.startswith(prefix)
        )
        page = names[offset : offset + 100]
        more = offset + len(page) < len(names)
        return {
            "paths": page,
            "has_more": more,
            "next_offset": offset + len(page) if more else None,
        }

    def state(self) -> str:
        return digest(self.files())

    def edit(self, path: str, old: str, new: str) -> dict:
        if path not in self.original:
            raise ValueError("Only implementation source is editable")
        before = self.full_read(path)
        if not old or before.count(old) != 1:
            return {"error": "Replacement must match exactly once"}
        after = before.replace(old, new, 1)
        if len(after.encode()) > 1024 * 1024 or len(new) > 16000:
            raise ValueError("Source budget exceeded")
        self.path(path).write_text(after)
        return {"edited": path, "state": self.state()}

    def diff(self) -> str:
        return "".join(
            "".join(
                difflib.unified_diff(
                    before.splitlines(True),
                    self.full_read(name).splitlines(True),
                    fromfile="a/" + name,
                    tofile="b/" + name,
                )
            )
            for name, before in self.original.items()
        )
