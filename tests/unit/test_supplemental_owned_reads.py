"""Supplemental read scopes retire every sibling before returning a refusal."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from server.core.errors import VectorAdmissionError
from server.memory.app import retrieval
from server.memory.app.search import supplemental
from tests.unit.test_supplemental_leg_shares_the_main_leg import _ctx


class _ReadGroup:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.never = asyncio.Event()
        self.children: list[asyncio.Task[Any]] = []
        self.cleaning: set[str] = set()
        self.retired: set[str] = set()
        self.failure = VectorAdmissionError("first supplemental refusal", reason="org_queue")

    async def read(self, name: str) -> list[Any]:
        child = asyncio.current_task()
        assert child is not None
        self.children.append(child)
        if len(self.children) == 3:
            self.started.set()
        if name == "first":
            await self.started.wait()
            raise self.failure
        try:
            await self.never.wait()
        finally:
            self.cleaning.add(name)
            await self.release.wait()
            self.retired.add(name)
        return []


async def _observe_cleanup(caller: asyncio.Task[Any], ready: Any) -> None:
    for _ in range(50):
        if caller.done() or ready():
            return
        await asyncio.sleep(0)


def _owning_read(monkeypatch: pytest.MonkeyPatch, group: _ReadGroup, owner: str) -> Awaitable[Any]:
    ctx = _ctx()
    ctx.all_hits = []
    if owner == "namespace":
        ctx.namespaces = ["first", "second", "third"]

        async def search(query: Any) -> list[Any]:
            return await group.read(query.namespace)

        monkeypatch.setattr(retrieval, "vector_search", search)
        return supplemental._supplemental_leg(ctx, "sub q", 7, set())

    async def phrase(_ctx: Any, text: str, _top_k: int, _seen: Any) -> list[Any]:
        return await group.read(text)

    monkeypatch.setattr(supplemental, "_supplemental_leg", phrase)
    if owner == "phrase":
        ctx.set_expansion_phrases = ["first", "second", "third"]
        return supplemental._run_set_expansion(ctx)
    ctx.sub_queries = ["first", "second", "third"]
    ctx.query_shape = "simple"
    return supplemental._run_sub_queries(ctx)


@pytest.mark.parametrize("owner", ["namespace", "phrase", "subquery"])
@pytest.mark.parametrize("repeat_cancel", [False, True], ids=["refusal", "cancel-during-cleanup"])
async def test_supplemental_refusal_retires_owned_siblings(
    monkeypatch: pytest.MonkeyPatch, owner: str, repeat_cancel: bool
) -> None:
    group = _ReadGroup()
    caller = asyncio.ensure_future(_owning_read(monkeypatch, group, owner))
    try:
        await group.started.wait()
        await _observe_cleanup(caller, lambda: len(group.cleaning) == 2)
        if repeat_cancel:
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
        group.release.set()
        with pytest.raises(VectorAdmissionError) as raised:
            await caller
        assert raised.value is group.failure
        assert len(group.children) == 3
        assert all(child.done() for child in group.children)
        assert group.retired == {"second", "third"}
    finally:
        group.release.set()
        for child in group.children:
            if not child.done():
                child.cancel()
        await asyncio.gather(caller, *group.children, return_exceptions=True)


async def test_nested_subquery_refusal_survives_repeated_outer_cleanup_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx()
    ctx.all_hits = []
    ctx.namespaces = ["ns0", "ns1"]
    ctx.sub_queries = ["failed", "sibling"]
    ctx.query_shape = "simple"
    started, release_inner, release_outer, never = (asyncio.Event() for _ in range(4))
    children: list[asyncio.Task[Any]] = []
    cleaning: set[tuple[str, str]] = set()
    retired: set[tuple[str, str]] = set()
    failure = VectorAdmissionError("first nested refusal", reason="org_queue")

    async def search(query: Any) -> list[Any]:
        child = asyncio.current_task()
        assert child is not None
        children.append(child)
        if len(children) == 4:
            started.set()
        key = (query.query_text, query.namespace)
        if key == ("failed", "ns0"):
            await started.wait()
            raise failure
        try:
            await never.wait()
        finally:
            cleaning.add(key)
            await (release_inner if query.query_text == "failed" else release_outer).wait()
            retired.add(key)
        return []

    monkeypatch.setattr(retrieval, "vector_search", search)
    caller = asyncio.create_task(supplemental._run_sub_queries(ctx))
    try:
        await started.wait()
        await _observe_cleanup(caller, lambda: ("failed", "ns1") in cleaning)
        release_inner.set()
        await _observe_cleanup(caller, lambda: len(cleaning) == 3)
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        release_outer.set()
        with pytest.raises(VectorAdmissionError) as raised:
            await caller
        assert raised.value is failure
        assert len(children) == 4 and all(child.done() for child in children)
        assert retired == {("failed", "ns1"), ("sibling", "ns0"), ("sibling", "ns1")}
        assert ctx.all_hits == []
    finally:
        release_inner.set()
        release_outer.set()
        for child in children:
            if not child.done():
                child.cancel()
        await asyncio.gather(caller, *children, return_exceptions=True)
