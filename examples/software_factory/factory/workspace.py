"""Source-only capabilities; no shell, git history, arbitrary import or execution."""

from __future__ import annotations

import difflib
from pathlib import Path

from examples.software_factory.factory.model import Task, digest


class Checkout:
    def __init__(self, root: Path, task: Task):
        self.root = root.resolve()
        self.original = dict(task.files)
        self.root.mkdir(parents=True, exist_ok=False)
        for name, content in task.files.items():
            path = self.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def path(self, name: str) -> Path:
        if name not in self.original or any(p.startswith(".") for p in Path(name).parts):
            raise PermissionError("Source file is outside the task allowlist")
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

    def state(self) -> str:
        return digest(self.files())

    def edit(self, path: str, old: str, new: str) -> dict:
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
