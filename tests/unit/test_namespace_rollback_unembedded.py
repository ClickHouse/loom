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
    seen: dict[str, list[Any]] = {
        "stubs": [],
        "retired": [],
        "memory": [],
        "retire_calls": [],
        "fenced": [],
    }
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

    async def _memory(rows: list[dict[str, Any]], *, alongside: Any = (), **_: Any) -> None:
        from server.memory.domain.temporal_capture import current_intent

        seen["memory"].extend(rows)
        seen.setdefault("intents", []).append(current_intent())
        kept = [r for r in rows if r["memory_key"] not in seen.get("refused", ())]
        for f in alongside:
            await f(kept)

    async def _stubs(rows: list[dict[str, Any]], **_: Any) -> None:
        seen["stubs"].extend(rows)

    async def _retire(items: list[tuple[Any, int]], **_: Any) -> int:
        seen["retire_calls"].append(items)
        seen["retired"].extend(items)
        return len(items)

    monkeypatch.setattr(store, "fetch_version_rows", _fetch)
    monkeypatch.setattr(store, "get_by_key", _get)
    monkeypatch.setattr(store, "insert_embedding_rows", _stubs)

    async def _fence(items: list[tuple[Any, int]], *, model: str, **_: Any) -> int:
        seen["fenced"].extend(items)
        return len(items)

    monkeypatch.setattr(store, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(store, "fence_vectors", _fence)

    async def _no_chunks(**_k: Any) -> int:
        return 0

    from server.memory.app import high_fence

    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _no_chunks)
    monkeypatch.setattr(rb, "insert_memory_versions", _memory)
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
    assert sorted(ident[5] for ident, _ in calls["retired"]) == ["ep", "gone_high", "secret"]


async def test_vectors_are_retired_in_one_call_at_each_rows_own_version(
    calls: dict[str, list[Any]],
) -> None:
    # One batched retire (entity vectors too), pinned to the version of the memory row
    # that replaces each key, so a concurrent newer embedded write keeps its vectors.
    plan = RollbackPlan(
        reinstate=[_action("secret", "reinstate", 11), _action("ep", "reinstate", 12)],
        remove=[_action("gone_high", "remove", 20)],
    )
    await rb.apply_rollback("test", "personal", plan, applied_at=_NOW, ch=object())

    assert len(calls["retire_calls"]) == 1
    written = {r["memory_key"]: r["version"] for r in calls["memory"]}
    assert {ident[5]: v for ident, v in calls["retired"]} == {
        k: written[k] for k in ("secret", "ep", "gone_high")
    }
    assert all(
        ident[:5] == ("test", "personal", "cursor", "-", "-") for ident, _ in calls["retired"]
    )
    # the high keys are fenced at the same versions: a plain write still embedding
    # lands below them
    assert {ident[5]: v for ident, v in calls["fenced"]} == {
        k: written[k] for k in ("secret", "gone_high")
    }


async def test_a_reinstated_stub_carries_its_memory_rows_version(
    calls: dict[str, list[Any]],
) -> None:
    """A stub drawn after the memory row's version would sit above a concurrent high
    write's fence; it takes the memory row's own version instead."""
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    await rb.apply_rollback("test", "personal", plan, applied_at=_NOW, ch=object())
    [stub] = calls["stubs"]
    [mem] = calls["memory"]
    assert stub["version"] == mem["version"]


async def test_a_row_the_publisher_refused_gets_no_projection(
    calls: dict[str, list[Any]],
) -> None:
    calls["refused"] = ["plain", "secret"]
    plan = RollbackPlan(
        reinstate=[_action("plain", "reinstate", 10), _action("secret", "reinstate", 11)],
        remove=[_action("gone", "remove", 21)],
    )
    applied = await rb.apply_rollback("test", "personal", plan, applied_at=_NOW, ch=object())
    assert applied == (0, 1)
    assert [r["memory_key"] for r in calls["stubs"]] == ["gone"]
    assert calls["retired"] == [] and calls["fenced"] == []


async def test_a_reinstated_value_holds_from_the_rollback(
    calls: dict[str, list[Any]],
) -> None:
    """A revision of the present: the dated value the key moved past becomes current
    again, from the rollback on."""
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    await rb.apply_rollback("test", "personal", plan, applied_at=_NOW, ch=object())
    [mem] = calls["memory"]
    assert (mem["valid_at"], mem["valid_to"]) == (_NOW, None)
    [intent] = calls["intents"]
    assert (intent.change_kind, intent.effective_at) == ("transition", _NOW.replace(tzinfo=UTC))
