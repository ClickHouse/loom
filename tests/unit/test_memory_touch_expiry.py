"""Memory.touch changes only the expiry — clear it, or set
it on many keys at once — without touching value, entities or
reinforcement."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from server.memory.api.models_crud import MemoryTouchInput
from server.memory.app import handlers as mem_handlers
from server.memory.app import restore as _restore_mod
from server.memory.app import stack as _stack_mod
from tests.unit.test_memory_write_path_mirror import _mk_row


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))


def _row(key: str, **kw: Any) -> Any:
    r = _mk_row()
    return r.model_copy(update={"memory_key": key, **kw})


@pytest.fixture()
def stubs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    ch = _CH()
    calls: dict[str, Any] = {"many": [], "one": [], "reemit_many": [], "reemit_one": []}
    rows = {k: _row(k, entities=["mergetree"], reinforcement_count=4) for k in ("a", "b")}

    async def _get_many(
        ident: Any, keys: list[str], *, ch: Any = None, strong: bool = False
    ) -> list[Any]:
        calls["many"].append((list(keys), strong))
        return [rows[k] for k in keys if k in rows]

    async def _get_one(ident: Any, key: str, **kw: Any) -> Any:
        calls["one"].append(key)
        return rows.get(key)

    async def _reemit_many(items: list[Any], *, ch: Any = None, batch: int = 500) -> int:
        calls["reemit_many"].append(items)
        return len(items)

    async def _reemit_one(*args: Any, ch: Any = None, **ov: Any) -> int:
        calls["reemit_one"].append((args, ov))
        return 1

    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.store.get_many_by_keys", _get_many)
    monkeypatch.setattr("server.memory.store.get_by_key", _get_one)
    monkeypatch.setattr("server.memory.store.reemit_embedding_rows_for_keys", _reemit_many)
    monkeypatch.setattr("server.memory.store.reemit_embedding_rows_for_key", _reemit_one)
    return {"ch": ch, "calls": calls}


@pytest.mark.asyncio
async def test_clear_expiry_on_one_key(stubs: dict[str, Any]) -> None:
    out = await mem_handlers.handle_memory_touch(
        MemoryTouchInput(org="o", namespace="n", agent="a", memory_key="a", clear_expiry=True)
    )
    assert out.touched and out.expires_at is None and out.results == []
    ((table, rows),) = stubs["ch"].inserts
    assert table == "loom.memory" and rows[0]["expires_at"] is None
    assert rows[0]["entities"] == ["mergetree"] and rows[0]["reinforcement_count"] == 4
    ((args, ov),) = stubs["calls"]["reemit_one"]
    assert args[-1] == "a" and ov == {"expires_at": None}


@pytest.mark.asyncio
async def test_many_keys_are_one_read_one_insert_one_reemit(stubs: dict[str, Any]) -> None:
    out = await mem_handlers.handle_memory_touch(
        MemoryTouchInput(
            org="o", namespace="n", agent="a", memory_keys=["a", "b", "missing"], clear_expiry=True
        )
    )
    assert out.touched_count == 2 and out.touched
    assert [(r.memory_key, r.touched) for r in out.results] == [
        ("a", True),
        ("b", True),
        ("missing", False),
    ]
    assert stubs["calls"]["many"] == [
        (["a", "b", "missing"], True)
    ]  # strong: the heads feed a write
    assert len(stubs["ch"].inserts) == 1 and len(stubs["ch"].inserts[0][1]) == 2
    assert all(
        r["expires_at"] is None and r["reinforcement_count"] == 4 for r in stubs["ch"].inserts[0][1]
    )
    (items,) = stubs["calls"]["reemit_many"]
    assert [k[-1] for k, _ in items] == ["a", "b"] and all(
        ov == {"expires_at": None} for _, ov in items
    )
    assert stubs["calls"]["reemit_one"] == []


@pytest.mark.asyncio
async def test_many_keys_with_a_new_ttl(stubs: dict[str, Any]) -> None:
    out = await mem_handlers.handle_memory_touch(
        MemoryTouchInput(
            org="o", namespace="n", agent="a", memory_keys=["a", "b"], ttl_seconds=3600
        )
    )
    assert out.touched_count == 2 and out.expires_at is not None
    assert {r.expires_at for r in out.results} == {out.expires_at}


def test_arguments_name_exactly_one_target_and_one_expiry() -> None:
    for bad in (
        {"memory_key": "a"},  # neither ttl nor clear
        {"memory_key": "a", "ttl_seconds": 60, "clear_expiry": True},  # both
        {"ttl_seconds": 60},  # no key
        {"memory_key": "a", "memory_keys": ["b"], "ttl_seconds": 60},  # both key forms
    ):
        with pytest.raises(ValidationError):
            MemoryTouchInput(org="o", namespace="n", agent="a", **bad)
    with pytest.raises(ValidationError):
        MemoryTouchInput(
            org="o",
            namespace="n",
            agent="a",
            memory_keys=[str(i) for i in range(1001)],
            clear_expiry=True,
        )
    assert (
        MemoryTouchInput(
            org="o", namespace="n", agent="a", memory_key="a", ttl_seconds=60
        ).ttl_seconds
        == 60
    )


class _SQLCapture:
    """Answers the locate with one head, then records the fetch."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if len(self.sql) == 1:
            return [
                {
                    "memory_id": "m",
                    "version": 1,
                    "agent": "a",
                    "user_id": "-",
                    "session_id": "-",
                    "memory_key": "k",
                    "created_at": None,
                }
            ]
        return []


@pytest.mark.asyncio
async def test_the_heads_a_touch_writes_from_are_whole_rows() -> None:
    """The strong bulk read feeds build_memory_version_row, so
    its fetch selects every column the builder carries forward. A bulk_get-style
    subset without duplicate_of and last_reinforced_at would fail the insert on
    the None DateTime, or zero duplicate_of. Locate
    under FINAL, fetch without it."""
    import re

    from server.memory import store as mr

    ident = mr.Identity(org="o", namespace="n", agent="a", user_id="-", session_id="-")
    written = mr.build_memory_version_row(_mk_row(), version=1)
    ch = _SQLCapture()
    await mr.get_many_by_keys(ident, ["k"], ch=ch, strong=True)  # type: ignore[arg-type]
    assert len(ch.sql) == 2 and "FINAL" not in ch.sql[1]
    select = ch.sql[1].split("FROM loom.memory", 1)[0]
    missing = [c for c in written if not re.search(rf"\b{c}\b", select)]
    assert missing == []
    # the plain bulk read (memory.bulk_get) keeps its narrower fetch
    ch = _SQLCapture()
    await mr.get_many_by_keys(ident, ["k"], ch=ch)  # type: ignore[arg-type]
    assert "duplicate_of" not in ch.sql[1].split("FROM loom.memory", 1)[0]
