"""Composed reads publish no mixed result after any child's proof is lost."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from server.core.diagnostics import codes_only
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.app.source_read_results import settle


def child(name, events, *, marked=True, mirrored=True, lost=False):
    async def check():
        assert not marked or codes_only()
        events.append(name + ":check")

    def fresh():
        events.append(name + ":fresh")
        return True

    result = SimpleNamespace(
        marked=marked,
        lost=lost,
        output=name + ":mixed",
        proofs=SimpleNamespace(check=AsyncMock(side_effect=check), fresh=Mock(side_effect=fresh))
        if mirrored
        else None,
    )

    async def ordinary():
        assert not marked or codes_only()
        events.append(name + ":ordinary")
        result.output = name + ":ordinary"
        result.proofs, result.lost = None, True
        return result.output

    result.ordinary = AsyncMock(side_effect=ordinary)
    return result


async def test_every_proof_is_checked_before_all_pure_freshness_checks_and_publication():
    events = []
    first, second = child("first", events), child("second", events)
    await settle([first, second])
    assert events == ["first:check", "second:check", "first:fresh", "second:fresh"]
    assert (first.output, second.output) == ("first:mixed", "second:mixed")
    first.ordinary.assert_not_awaited()
    second.ordinary.assert_not_awaited()
    assert not codes_only()


@pytest.mark.parametrize("when", ["prior_loss", "check_loss", "last_check_loss", "expired"])
async def test_any_child_loss_rebuilds_all_children_before_returning(when):
    events = []
    first, second = child("first", events), child("second", events)
    if when == "prior_loss":
        first.lost = True
    elif when == "check_loss":
        first.proofs.check.side_effect = LostCorpus
    elif when == "last_check_loss":

        async def loss():
            first.lost = True

        second.proofs.check.side_effect = loss
    else:
        first.proofs.fresh.side_effect = lambda: False
    await settle([first, second])
    assert (first.output, second.output) == ("first:ordinary", "second:ordinary")
    first.ordinary.assert_awaited_once()
    second.ordinary.assert_awaited_once()
    if when == "prior_loss":
        assert events == ["first:ordinary", "second:ordinary"]


async def test_empty_or_ordinary_only_composition_needs_no_fallback_or_conditional_io():
    events = []
    ordinary = child("ordinary", events, marked=False, mirrored=False)
    await settle([])
    await settle([ordinary])
    assert events == [] and not codes_only()
    ordinary.ordinary.assert_not_awaited()


async def test_marked_composition_keeps_ordinary_sibling_fallback_private():
    events = []
    first = child("first", events, lost=True)
    second = child("ordinary", events, marked=False, mirrored=False)

    async def ordinary():
        assert codes_only()
        second.output = "ordinary:reply"

    second.ordinary.side_effect = ordinary
    await settle([first, second])
    assert second.output == "ordinary:reply" and not codes_only()


async def test_child_collection_is_snapshotted_before_any_check_awaits():
    events = []
    first, second = child("first", events), child("second", events)
    children = [first, second]

    async def remove():
        children.clear()
        raise LostCorpus()

    first.proofs.check.side_effect = remove
    await settle(children)
    assert first.output == "first:ordinary" and second.output == "second:ordinary"
    assert not children


async def test_unrelated_proof_error_propagates_without_returning_or_rebuilding_partial_results():
    events = []
    first, second = child("first", events), child("second", events)
    first.proofs.check.side_effect = RuntimeError("ordinary backend unavailable")
    with pytest.raises(RuntimeError, match="ordinary backend unavailable"):
        await settle([first, second])
    first.ordinary.assert_not_awaited()
    second.ordinary.assert_not_awaited()


async def test_failed_fallback_cancels_and_drains_its_owned_sibling_before_propagating():
    events = []
    first, second = child("first", events, lost=True), child("second", events)
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def fail():
        await entered.wait()
        raise RuntimeError("ordinary provider failed")

    async def sibling():
        entered.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    first.ordinary.side_effect, second.ordinary.side_effect = fail, sibling
    with pytest.raises(RuntimeError, match="ordinary provider failed"):
        await settle([first, second])
    assert stopped.is_set()


async def test_outer_cancellation_drains_all_owned_fallback_tasks():
    events = []
    first, second = child("first", events, lost=True), child("second", events)
    entered, stopped = [asyncio.Event(), asyncio.Event()], [asyncio.Event(), asyncio.Event()]

    async def wait(index):
        entered[index].set()
        try:
            await asyncio.Future()
        finally:
            stopped[index].set()

    async def first_wait():
        await wait(0)

    async def second_wait():
        await wait(1)

    first.ordinary.side_effect = first_wait
    second.ordinary.side_effect = second_wait
    task = asyncio.create_task(settle([first, second]))
    await asyncio.gather(*(event.wait() for event in entered))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(event.is_set() for event in stopped)
