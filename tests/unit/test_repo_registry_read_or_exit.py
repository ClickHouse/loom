"""`read_registry_or_exit`: a process outside the server and the `loom code` CLI reads the
self-service registry once before listing entries, or stops naming itself."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.code.app.index import repo_registry


def test_a_read_registry_lets_the_caller_go_on(monkeypatch) -> None:
    monkeypatch.setattr(repo_registry, "read_registry", AsyncMock(return_value=True))
    asyncio.run(repo_registry.read_registry_or_exit("harness"))


def test_a_failed_read_exits_naming_the_caller(monkeypatch) -> None:
    monkeypatch.setattr(repo_registry, "read_registry", AsyncMock(return_value=False))
    with pytest.raises(
        SystemExit,
        match=r"^harness: the self-service repository registry could not be read; retry$",
    ):
        asyncio.run(repo_registry.read_registry_or_exit("harness"))
