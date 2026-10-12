"""namespace.rollback never writes a stub embedding row for a row that is not
embedded: the stub's excerpt is the value, so a reinstated 'high' row would
reach search. It retires the key's live vectors instead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import server.memory.store as store
from server.memory.app import namespace_rollback as rb
from server.memory.domain.rollback import RollbackAction, RollbackPlan
from server.memory.store import MemoryRow

_NOW = datetime.now(UTC).replace(tzinfo=None)


def _row(key: str, version: int, **overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "22222222-2222-2222-2222-222222222222",
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "value": "a value",
        "confidence": 0.8,
        "version": version,
        "pinned": False,
        "created_at": _NOW - timedelta(days=1),
        "expires_at": None,
        "entities": [],
    }
    base.update(overrides)
    return MemoryRow(**base)


def _action(key: str, action: str, version_to: int) -> RollbackAction:
    return RollbackAction(
        agent="cursor",
        user_id="-",
        session_id="-",
        memory_key=key,
        action=action,
        version_from=1,
        version_to=version_to,
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {"stubs": [], "retired": []}
    historical = [
        _row("plain", 10),
        _row("secret", 11, value="token sk-proj-abc", sensitivity="high"),
        _row("ep", 12, memory_type="episodic"),
    ]
    heads = {"gone_high": _row("gone_high", 20, sensitivity="high"), "gone": _row("gone", 21)}

    async def _fetch(org: str, ns: str, pairs: list[Any], **_: Any) -> list[MemoryRow]:
        return historical

    async def _get(ident: Any, key: str, **_: Any) -> MemoryRow:
        return heads[key]

    async def _noop(rows: list[Any], **_: Any) -> None:
        return None

    async def _stubs(rows: list[dict[str, Any]], **_: Any) -> None:
        seen["stubs"].extend(rows)

    async def _retire(org: str, ns: str, agent: str, uid: str, sid: str, key: str, **_: Any) -> int:
        seen["retired"].append(key)
        return 1

    monkeypatch.setattr(store, "fetch_version_rows", _fetch)
    monkeypatch.setattr(store, "get_by_key", _get)
    monkeypatch.setattr(store, "insert_embedding_rows", _stubs)
    monkeypatch.setattr(store, "tombstone_embeddings_for_key", _retire)
    monkeypatch.setattr(rb, "insert_memory_versions", _noop)
    return seen


async def test_unembedded_rows_get_no_stub_and_their_vectors_are_retired(
    calls: dict[str, list[Any]],
) -> None:
    plan = RollbackPlan(
        reinstate=[
            _action("plain", "reinstate", 10),
            _action("secret", "reinstate", 11),
            _action("ep", "reinstate", 12),
        ],
        remove=[_action("gone_high", "remove", 20), _action("gone", "remove", 21)],
    )
    await rb.apply_rollback("test", "personal", plan, applied_at=_NOW, ch=object())

    assert sorted(r["memory_key"] for r in calls["stubs"]) == ["gone", "plain"]
    assert not any("sk-proj" in r["content_excerpt"] for r in calls["stubs"])
    assert sorted(calls["retired"]) == ["ep", "gone_high", "secret"]
