"""memory.history drops a version that reads the same as its newer neighbour (the
entity tagger's re-emit) and keeps every change a reader can see."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from server.memory.app import curation as cur
from server.memory.store.history import HistoryVersion

_T0 = datetime(2026, 10, 10, 17, 45, 9)


def _v(version: int, value: str, *, memory_id: str = "m1", **over: Any) -> HistoryVersion:
    row = {
        "memory_id": memory_id,
        "version": version,
        "value": value,
        "confidence": 0.95,
        "source_trace": "",
        "pinned": 0,
        "created_at": _T0,
        "updated_at": _T0,
        "expires_at": None,
        "tombstoned_at": None,
        "redacted_at": None,
        "redaction_reason": "",
        "actor": "process:sa:desktop",
        "source_class": "asserted",
        "agent": "a",
    }
    return HistoryVersion.from_row({**row, **over})


def _history(rows: list[HistoryVersion], monkeypatch: pytest.MonkeyPatch) -> list[int]:
    import server.memory.store as memory_repo
    from server.memory.api.models_temporal import MemoryHistoryInput

    async def _list(*_a: Any, **_k: Any) -> list[HistoryVersion]:
        return rows

    monkeypatch.setattr(memory_repo, "list_history", _list)
    out = asyncio.run(
        cur.handle_memory_history(
            MemoryHistoryInput(org="o", namespace="n", agent="a", memory_key="k")
        )
    )
    return [r.version for r in out.rows]


def test_an_entity_re_emit_is_not_a_version(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_v(3, '"x"'), _v(2, '"x"'), _v(1, '"old"')]
    assert _history(rows, monkeypatch) == [3, 1]


def test_visible_changes_and_reverts_stay(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _v(5, '"x"', tombstoned_at=_T0),
        _v(4, '"x"'),
        _v(3, '"x"', pinned=1),
        _v(2, '"y"'),
        _v(1, '"x"'),
    ]
    assert _history(rows, monkeypatch) == [5, 4, 3, 2, 1]


def test_writers_are_compared_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_v(3, '"x"', memory_id="m2"), _v(2, '"x"'), _v(1, '"x"', memory_id="m2")]
    assert _history(rows, monkeypatch) == [3, 2]
