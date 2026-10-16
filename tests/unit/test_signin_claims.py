"""claims.swap when the request is cancelled between Keeper committing the
write and the read-back that would report it: the write is undone, so the
credential is left as the request found it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import claims

pytestmark = pytest.mark.asyncio


class _Keeper:
    """Takes writes at once; the first read-back answers only once `answer` is set."""

    def __init__(self, value: str | None) -> None:
        self.value = value
        self.writes: list[str] = []
        self.reading = asyncio.Event()
        self.answer = asyncio.Event()
        self._hung = False

    async def execute(self, sql: str, params: dict[str, Any]) -> None:
        self.writes.append(sql.split()[0])
        if sql.startswith("INSERT"):
            self.value = params["v"]
        elif "DELETE" in sql:
            if self.value == params["v"]:
                self.value = None
        elif self.value == params["old"]:
            self.value = params["v"]

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if not self._hung:
            self._hung = True
            self.reading.set()
            await self.answer.wait()  # the read-back the cancellation lands in
        return {"value": self.value} if self.value is not None else None


@pytest.fixture(autouse=True)
def _keeper_present(monkeypatch) -> None:
    monkeypatch.setattr(claims, "_atomic", True)


async def _cancel_during_read_back(ch: _Keeper, old: str | None) -> None:
    task = asyncio.create_task(claims.swap(ch, "family:f", old, "mine", 2_000_000_000))
    await ch.reading.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert ch.writes[-1] in ("INSERT", "ALTER") and len(ch.writes) == 1  # no undo yet
    ch.answer.set()  # the write's own read-back comes back after the cancellation
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_new_key_cancelled_before_its_read_back_is_dropped() -> None:
    ch = _Keeper(None)
    await _cancel_during_read_back(ch, None)
    assert ch.value is None and ch.writes == ["INSERT", "ALTER"]


async def test_a_change_cancelled_before_its_read_back_is_undone() -> None:
    ch = _Keeper("0:token")
    await _cancel_during_read_back(ch, "0:token")
    assert ch.value == "0:token"
