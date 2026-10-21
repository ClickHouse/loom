"""The point-write intent batcher: queued ids leave in one mark across their orgs, a
failed mark leaves them for the repair, a cancelled one keeps them for the next flush,
and the flusher starts again once its last run ended."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.memory.app import intent_marks, projection_failures


@pytest.fixture
def marks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], list[str]]]:
    calls: list[tuple[list[str], list[str]]] = []

    async def mark_resolved(orgs: Any, ids: list[str], **kw: Any) -> None:
        calls.append((list(orgs), list(ids)))

    monkeypatch.setattr(projection_failures, "mark_resolved", mark_resolved)
    monkeypatch.setattr(intent_marks, "_pending", {})
    monkeypatch.setattr(intent_marks, "_flusher", None)
    monkeypatch.setattr(intent_marks, "FLUSH_S", 0.01)
    return calls


async def test_a_drain_marks_every_org_in_one_mark(marks: Any) -> None:
    intent_marks.resolve_soon("o1", ["b", "a"])
    intent_marks.resolve_soon("o2", ["c"])
    await intent_marks.drain()
    assert marks == [(["o1", "o2"], ["a", "b", "c"])]
    assert intent_marks._pending == {}


async def test_a_failed_mark_leaves_the_intents_to_the_repair(
    marks: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*a: Any, **kw: Any) -> None:
        raise RuntimeError("mutation refused")

    monkeypatch.setattr(projection_failures, "mark_resolved", down)
    intent_marks.resolve_soon("o1", ["a"])
    await intent_marks.drain()  # no raise: the rows stay active for the repair
    assert intent_marks._pending == {}


async def test_a_mark_cancelled_mid_flush_keeps_its_ids(
    marks: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()

    async def slow(*a: Any, **kw: Any) -> None:
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(projection_failures, "mark_resolved", slow)
    intent_marks.resolve_soon("o1", ["a"])
    await started.wait()
    flusher = intent_marks._flusher
    assert flusher is not None
    flusher.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flusher
    assert intent_marks._pending == {"o1": {"a"}}


async def test_the_flusher_starts_again_after_its_run_ended(marks: Any) -> None:
    intent_marks.resolve_soon("o1", ["a"])
    first = intent_marks._flusher
    assert first is not None
    await first  # flushed, then nothing pending: it ends
    intent_marks.resolve_soon("o1", ["b"])
    assert intent_marks._flusher is not first
    await intent_marks._flusher  # type: ignore[misc]
    assert marks == [(["o1"], ["a"]), (["o1"], ["b"])]
