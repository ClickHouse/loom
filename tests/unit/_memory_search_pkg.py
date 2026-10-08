"""The memory.search handler package, for tests that read its source or fake its collaborators.

The handler is a package of modules, so a test that scans the handler's source reads every
module, and a test that fakes ClickHouse or the embedder for a whole search sets the name in
each module that looks it up.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from types import ModuleType
from typing import Any

import pytest

import server.memory.app.search as _pkg


def handler_modules() -> list[ModuleType]:
    """The package and every module in it."""
    return [_pkg] + [
        importlib.import_module(f"{_pkg.__name__}.{m.name}")
        for m in pkgutil.iter_modules(_pkg.__path__)
    ]


def handler_source() -> str:
    """The source of every module of the package, one after another."""
    return "\n".join(inspect.getsource(m) for m in handler_modules())


def patch_handler_name(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """Set `name` in every module of the package that binds it (a module-level import)."""
    bound = [m for m in handler_modules() if name in vars(m)]
    assert bound, f"no module of {_pkg.__name__} binds {name}"
    for m in bound:
        monkeypatch.setattr(m, name, value)
