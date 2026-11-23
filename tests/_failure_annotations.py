"""Emit bounded failure locations without test names or captured values."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

MAX_MODULES = 50
MAX_PATH = 240
_COMPONENT = re.compile(r"[A-Za-z0-9_ .,%+\-]+", re.ASCII)


def emit_failed_module(
    report: pytest.TestReport, seen: set[str], write: Callable[[str], object], *, enabled: bool
) -> None:
    if not enabled or not report.failed or len(seen) >= MAX_MODULES:
        return
    path, line = report.location[:2]
    if not _valid_path(path) or path in seen:
        return
    seen.add(path)
    escaped = path.replace("%", "%25").replace(",", "%2C")
    position = f",line={line + 1}" if type(line) is int and 0 <= line < 1_000_000 else ""
    annotation = f"::error file={escaped}{position}::A test declared in this module failed.\n"
    try:
        write(annotation)
    except Exception:  # noqa: BLE001 — annotations must preserve the original test result
        pass


def _valid_path(path: str) -> bool:
    if not isinstance(path, str) or len(path) > MAX_PATH:
        return False
    if not path.startswith("tests/") or not path.endswith(".py"):
        return False
    return all(
        part not in {".", ".."} and _COMPONENT.fullmatch(part) is not None
        for part in path.split("/")
    )
