"""A sensitive row carries its retention window at write."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.domain.sensitive_retention import sensitive_retention

NOW = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)


def _s(days: int = 30, on: str = "anonymize") -> Any:
    return SimpleNamespace(memory_sensitive_retention_days=days, memory_sensitive_on_expire=on)


def test_off_or_not_sensitive_leaves_the_row_alone() -> None:
    assert sensitive_retention(
        sensitivity="medium", expires_at=None, on_expire="", now=NOW, settings=_s(days=0)
    ) == (None, "", False)
    assert sensitive_retention(
        sensitivity="", expires_at=None, on_expire="", now=NOW, settings=_s()
    ) == (None, "", False)


def test_a_medium_row_with_no_expiry_gets_the_window_and_the_default_post_action() -> None:
    exp, on, changed = sensitive_retention(
        sensitivity="medium", expires_at=None, on_expire="", now=NOW, settings=_s(30)
    )
    assert exp == NOW + timedelta(days=30) and on == "anonymize" and changed


def test_a_shorter_caller_expiry_stands_and_a_longer_one_is_pulled_in() -> None:
    short = NOW + timedelta(days=3)
    exp, on, _ = sensitive_retention(
        sensitivity="high", expires_at=short, on_expire="tombstone", now=NOW, settings=_s(30)
    )
    assert exp is short and on == "tombstone"  # the caller's choices stand
    long = NOW + timedelta(days=400)
    exp2, _, changed = sensitive_retention(
        sensitivity="medium", expires_at=long, on_expire="", now=NOW, settings=_s(30)
    )
    assert exp2 == NOW + timedelta(days=30) and changed


def test_naive_and_aware_expiries_compare_on_the_wall_clock() -> None:
    naive_long = (NOW + timedelta(days=400)).replace(tzinfo=None)
    exp, _, _ = sensitive_retention(
        sensitivity="medium", expires_at=naive_long, on_expire="", now=NOW, settings=_s(30)
    )
    assert exp == NOW + timedelta(days=30)
    naive_short = (NOW + timedelta(days=1)).replace(tzinfo=None)
    exp2, _, _ = sensitive_retention(
        sensitivity="medium", expires_at=naive_short, on_expire="", now=NOW, settings=_s(30)
    )
    assert exp2 is naive_short


# ---- through memory.set ------------------------------------------------------


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_memory_set_stamps_the_window_on_a_contact_detail(monkeypatch) -> None:
    import server.memory.app.handlers as mem_handlers
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import restore as _restore_mod
    from server.memory.app import stack as _stack_mod
    from server.memory.app import write as write_mod
    from server.settings import settings

    ch = _CH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch)

    async def _none(*_: Any, **__: Any):
        return None

    monkeypatch.setattr("server.memory.store.get_by_key", _none)
    monkeypatch.setattr("server.memory.store.get_by_id", _none)
    # Every other read on the write path (blacklist cache, conflict gate,
    # type config) goes through the module-level client: the fake answers
    # them all with "nothing there".
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    import server.memory.app.blacklist_guard as _bl
    import server.memory.app.conflicts as _cf

    monkeypatch.setattr(_bl, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(_cf, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(settings, "memory_sensitive_retention_days", 30)
    monkeypatch.setattr(settings, "memory_sensitive_on_expire", "anonymize")
    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", True)

    await write_mod.handle_memory_set(
        MemorySetInput(
            org="test",
            namespace="personal",
            agent="cursor",
            memory_key="contact_jane",
            value="Jane's number is +1 415 555 0134 and her email is jane@example.com",
            classify=False,
        )
    )
    memory_rows = [r for t, rows in ch.inserts if t == "loom.memory" for r in rows]
    assert memory_rows, "the set path wrote a loom.memory row"
    row = memory_rows[0]
    assert row["sensitivity"] == "medium"
    assert row["expires_at"] is not None and row["on_expire"] == "anonymize"
    delta = row["expires_at"] - datetime.now(UTC).replace(tzinfo=row["expires_at"].tzinfo)
    assert timedelta(days=29, hours=23) < delta <= timedelta(days=30)
