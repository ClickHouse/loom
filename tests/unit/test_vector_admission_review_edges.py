"""Vector dispatches and composed reads keep capacity attribution and child ownership."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.search import cards
from server.core.errors import VectorAdmissionError, VectorCapacityError
from server.core.vectors.admission import current_work
from server.memory.app import conflicts
from server.memory.app.retrieval import chunks
from server.settings import settings
from server.tracker.api.models import TrackerDuplicateCheckInput
from server.tracker.app import dupcheck_candidates as candidates
from tests.unit.test_memory_search_service import _q
from tests.unit.test_vector_read_admission import Driver, NativeBudgetError, client


class _DispatchDriver(Driver):
    def __init__(self, rows: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self.rows = rows
        self.work: list[Any] = []
        self.failure_at = 0

    async def query(self, sql: str, **kwargs: Any) -> Any:
        self.started += 1
        self.settings.append(kwargs["settings"])
        self.work.append(current_work())
        if self.started == self.failure_at:
            raise self.failure or RuntimeError("configured test failure")
        rows = self.rows.pop(0) if self.rows else []
        names = list(rows[0]) if rows else []
        return SimpleNamespace(
            result_rows=[tuple(row[name] for name in names) for row in rows],
            column_names=names,
            summary={},
        )


def _dispatch(monkeypatch: pytest.MonkeyPatch, rows: list[list[dict[str, Any]]]):
    ch, _ = client(monkeypatch)
    driver = _DispatchDriver(rows)
    monkeypatch.setattr(ch, "_client", driver)
    return ch, driver


def _assert_vector(driver: _DispatchDriver, index: int, org: str, namespace: str) -> None:
    work = driver.work[index]
    assert work is not None
    assert (work.org, work.namespaces, work.weight) == (org, (namespace,), 1)
    assert driver.settings[index]["max_rows_to_read"] == 1_000_000
    assert driver.settings[index]["read_overflow_mode"] == "throw"


@pytest.mark.parametrize("text", ["neighbor words", ""], ids=["neighbor-vector", "keyed-only"])
async def test_chunk_stage_two_attributes_only_its_actual_neighbor_cosine(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    ch, driver = _dispatch(
        monkeypatch, [[{"session_id": "session", "chunk_index": 3, "distance": 0.1}], []]
    )
    q = _q(entity_filter=[], query_text=text)
    assert await chunks._search_memory_chunks(ch, q, {"q": q.query_vec}) == []
    assert driver.started == 2
    _assert_vector(driver, 0, q.org, q.namespace)
    if text:
        _assert_vector(driver, 1, q.org, q.namespace)
    else:
        assert driver.work[1] is None
        assert "max_rows_to_read" not in driver.settings[1]


async def test_chunk_stage_two_native_refusal_does_not_become_an_empty_leg(monkeypatch) -> None:
    ch, driver = _dispatch(
        monkeypatch, [[{"session_id": "session", "chunk_index": 3, "distance": 0.1}]]
    )
    driver.failure_at = 2
    driver.failure = NativeBudgetError(158)
    q = _q(entity_filter=[], query_text="neighbor words")
    with pytest.raises(VectorCapacityError) as raised:
        await chunks._search_memory_chunks(ch, q, {"q": q.query_vec})
    assert raised.value.extra["reason"] == "rows"
    assert driver.started == 2


@pytest.mark.parametrize("outcome", ["success", "native", "ordinary"])
async def test_chunk_fallback_window_keeps_vector_budget_and_ordinary_fallback(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    ch, driver = _dispatch(monkeypatch, [[]])
    if outcome != "success":
        driver.failure_at = 1
        driver.failure = NativeBudgetError(158) if outcome == "native" else RuntimeError("optional")

    async def run():
        return await chunks._chunk_anchor_windows(
            ch,
            [("session", 3)],
            prefetched=[],
            window_rows=[],
            chunk_agent_pred="",
            chunk_params={"q": [0.1], "org": "one-org", "ns": "private", "model": "fake"},
        )

    if outcome == "native":
        with pytest.raises(VectorCapacityError) as raised:
            await run()
        assert raised.value.extra["reason"] == "rows"
    else:
        assert await run() == []
        if outcome == "success":
            _assert_vector(driver, 0, "one-org", "private")
    assert driver.started == 1


@pytest.mark.parametrize("outcome", ["success", "native", "empty"])
async def test_promote_shared_peer_scan_is_one_vector_probe(monkeypatch, outcome: str) -> None:
    ch, driver = _dispatch(monkeypatch, [[]])
    monkeypatch.setattr(conflicts, "get_client", lambda: ch)

    async def threshold(*_args, **_kwargs):
        return 0.8

    monkeypatch.setattr(conflicts, "_resolve_threshold", threshold)
    if outcome == "native":
        driver.failure_at = 1
        driver.failure = NativeBudgetError(158)

    async def run():
        return await conflicts.check_against_shared(
            org="one-org",
            namespace="private",
            target_key="new-key",
            memory_type="semantic",
            embedding=[] if outcome == "empty" else [0.1],
        )

    if outcome == "native":
        with pytest.raises(Exception) as raised:
            await run()
        assert isinstance(raised.value, VectorCapacityError)
        assert raised.value.extra["reason"] == "rows"
    else:
        assert await run() == []
        if outcome == "success":
            _assert_vector(driver, 0, "one-org", "private")
    assert driver.started == (0 if outcome == "empty" else 1)


class _OwnedReads:
    def __init__(self, count: int) -> None:
        self.count = count
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.never = asyncio.Event()
        self.children: list[asyncio.Task[Any]] = []
        self.cleaning: set[str] = set()
        self.retired: set[str] = set()
        self.failure = VectorAdmissionError("first composed refusal", reason="org_queue")

    async def read(self, name: str, *, fail: bool) -> Any:
        task = asyncio.current_task()
        assert task is not None
        self.children.append(task)
        if len(self.children) == self.count:
            self.started.set()
        if fail:
            await self.started.wait()
            raise self.failure
        try:
            await self.never.wait()
        finally:
            self.cleaning.add(name)
            await self.release.wait()
            self.retired.add(name)
        return []


async def _refusal_joins(coro: Any, group: _OwnedReads, repeat_cancel: bool) -> None:
    caller = asyncio.ensure_future(coro)
    try:
        await group.started.wait()
        for _ in range(60):
            if caller.done() or len(group.cleaning) == group.count - 1:
                break
            await asyncio.sleep(0)
        if repeat_cancel:
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
        group.release.set()
        with pytest.raises(VectorCapacityError) as raised:
            await caller
        assert raised.value is group.failure
        assert len(group.children) == group.count
        assert all(child.done() for child in group.children)
        assert len(group.retired) == group.count - 1
    finally:
        group.release.set()
        for child in group.children:
            if not child.done():
                child.cancel()
        await asyncio.gather(caller, *group.children, return_exceptions=True)


@pytest.mark.parametrize("branch", ["source-refuses", "source-owned", "direct-concept"])
@pytest.mark.parametrize("repeat_cancel", [False, True], ids=["refusal", "cleanup-cancel"])
async def test_code_card_owner_joins_both_branches_and_explicit_source(
    monkeypatch: pytest.MonkeyPatch, branch: str, repeat_cancel: bool
) -> None:
    group = _OwnedReads(2)
    monkeypatch.setattr(settings, "code_search_card_leg_mode", "skeleton")

    class Repo:
        async def vector_search_cards(self, *_args, skip_tests=False, **_kwargs):
            fail = skip_tests if branch == "source-refuses" else not skip_tests
            return await group.read("source" if skip_tests else "cards", fail=fail)

    if branch == "direct-concept":

        async def concept(*_args, **_kwargs):
            return await group.read("concept", fail=False)

        monkeypatch.setattr(cards, "_concept_leg_rows", concept)

    async def vector():
        return [0.1]

    await _refusal_joins(
        cards._card_and_concept_rows(
            Repo(),
            "one-org",
            "private",
            "fake",
            query="query",
            query_vec=vector,
            use_cards=True,
            card_gate=0.0,
            concept_model="" if branch == "direct-concept" else "cards:5",
            path_prefix="",
            scope=None,
            work={},
            card_stats={"files": 0},
            concept_stats={"terms": []},
        ),
        group,
        repeat_cancel,
    )


@pytest.mark.parametrize("repeat_cancel", [False, True], ids=["refusal", "cleanup-cancel"])
async def test_duplicate_candidates_join_all_four_owned_reads(monkeypatch, repeat_cancel) -> None:
    group = _OwnedReads(4)

    async def query(*_args, **_kwargs):
        return SimpleNamespace(), {}, None

    async def resolve(*_args, **_kwargs):
        return None

    async def legs(*_args, **_kwargs):
        return await group.read("legs", fail=True)

    async def record(*_args, **_kwargs):
        return await group.read("record", fail=False)

    async def flight(*_args, **_kwargs):
        return await group.read("flight", fail=False)

    async def fingerprint(*_args, **_kwargs):
        return await group.read("fingerprint", fail=False)

    monkeypatch.setattr(candidates.tsim, "_similar_query", query)
    monkeypatch.setattr(candidates, "_shape_query", lambda *_args: None)
    monkeypatch.setattr(candidates, "_resolve_error_name", resolve)
    monkeypatch.setattr(candidates, "_draft_paths", lambda *_args: [])
    monkeypatch.setattr(candidates.tsim, "_similar_legs", legs)
    monkeypatch.setattr(candidates.tsim, "_similar_record", record)
    monkeypatch.setattr(candidates, "_fix_in_flight_leg", flight)
    monkeypatch.setattr(candidates, "_fingerprint_leg", fingerprint)
    draft = TrackerDuplicateCheckInput(org="one-org", namespace="qa-example", title="draft")
    await _refusal_joins(
        candidates.candidates(None, draft, wall=None, status={}), group, repeat_cancel
    )
