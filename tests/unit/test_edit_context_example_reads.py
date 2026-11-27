"""Edit-batch source reads retain each verified test site."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.code.app.enrich import _example_context, share_edit_example_reads
from server.code.store.index import CodeRepo
from server.settings import settings

_DIRECT = {"path": "tests/test_routes.py", "caller": "tests::test_route", "line": 20}
_INDIRECT = {**_DIRECT, "evidence": "indirect_call", "via": {"symbol": "register"}}


def _rows() -> list[dict[str, Any]]:
    return [
        {
            "indexed_commit": "head",
            "start_line": 18,
            "end_line": 22,
            "content": "".join(f"line {line}\n" for line in range(18, 23)),
        }
    ]


async def _read(
    repo: CodeRepo,
    site: dict[str, Any] = _DIRECT,
    *,
    org: str = "org",
    ns: str = "repo",
    radius: int = 2,
    model: str = "embedding",
    revision: str = "head",
) -> dict[str, Any]:
    return await _example_context(repo, org, ns, site, radius, model, revision)


@pytest.mark.asyncio
async def test_edit_batch_shares_exact_windows_and_keeps_separate_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CodeRepo()
    read = AsyncMock(return_value=_rows())
    monkeypatch.setattr(repo, "source_chunks_for_span", read)
    expected = [await _read(repo, _DIRECT), await _read(repo, _INDIRECT)]
    assert read.await_count == 2
    read.reset_mock()

    async with share_edit_example_reads():
        actual = [await _read(repo, _DIRECT), await _read(repo, _INDIRECT)]
    assert actual == expected
    assert actual[0]["context"] == actual[1]["context"]
    assert "via" not in actual[0] and actual[1]["via"] == _INDIRECT["via"]
    read.assert_awaited_once()

    async with share_edit_example_reads():
        assert await _read(repo) == expected[0]
    assert read.await_count == 2


@pytest.mark.asyncio
async def test_edit_batch_does_not_share_different_window_or_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CodeRepo()
    read = AsyncMock(return_value=_rows())
    monkeypatch.setattr(repo, "source_chunks_for_span", read)
    async with share_edit_example_reads():
        assert "context" in await _read(repo)
        assert "context" in await _read(repo, org="other-org")
        assert "context" in await _read(repo, ns="other-namespace")
        assert "context" in await _read(repo, model="other-model")
        assert "context" in await _read(repo, radius=1)
        assert "context" in await _read(repo, site={**_DIRECT, "path": "tests/test_other.py"})
        assert await _read(repo, revision="stale") == _DIRECT
    assert read.await_count == 7


@pytest.mark.asyncio
async def test_edit_batch_waiter_timeout_leaves_shared_read_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CodeRepo()
    entered, release, retired = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        entered.set()
        try:
            await release.wait()
            return _rows()
        finally:
            retired.set()

    tracked = AsyncMock(side_effect=read)
    monkeypatch.setattr(repo, "source_chunks_for_span", tracked)
    deadlines: asyncio.Queue[asyncio.Timeout] = asyncio.Queue()

    async def expiring() -> dict[str, Any]:
        async with asyncio.timeout(None) as deadline:
            deadlines.put_nowait(deadline)
            return await _read(repo)

    async with share_edit_example_reads():
        first = asyncio.create_task(expiring())
        deadline = await deadlines.get()
        await entered.wait()
        second = asyncio.create_task(_read(repo, _INDIRECT))
        deadline.reschedule(asyncio.get_running_loop().time())
        with pytest.raises(TimeoutError):
            await first
        assert not retired.is_set()
        release.set()
        assert (await second)["context"]["commit"] == "head"
    tracked.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_batch_retries_after_the_shared_read_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CodeRepo()
    retired = asyncio.Event()
    attempts = 0

    async def read(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            try:
                await asyncio.Future()
            finally:
                retired.set()
        return _rows()

    monkeypatch.setattr(settings, "code_enrich_timeout_s", 0.05)
    monkeypatch.setattr(repo, "source_chunks_for_span", read)
    async with share_edit_example_reads():
        assert await _read(repo) == _DIRECT
        assert (await _read(repo, _INDIRECT))["context"]["commit"] == "head"
        assert retired.is_set()
    assert attempts == 2


@pytest.mark.asyncio
async def test_edit_batch_retries_an_unavailable_source_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CodeRepo()
    read = AsyncMock(side_effect=[RuntimeError("temporary source read failure"), _rows()])
    monkeypatch.setattr(repo, "source_chunks_for_span", read)
    async with share_edit_example_reads():
        assert await _read(repo) == _DIRECT
        assert (await _read(repo, _INDIRECT))["context"]["commit"] == "head"
    assert read.await_count == 2


@pytest.mark.asyncio
async def test_edit_batch_followers_can_retry_a_shared_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CodeRepo()
    count = 0

    async def read(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal count
        count += 1
        if count == 1:
            await asyncio.sleep(0)
            raise RuntimeError("temporary source read failure")
        return _rows()

    monkeypatch.setattr(repo, "source_chunks_for_span", read)
    async with share_edit_example_reads():
        direct, indirect = await asyncio.gather(_read(repo), _read(repo, _INDIRECT))
    assert direct == _DIRECT
    assert indirect["context"]["commit"] == "head"
    assert count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source_error", [True, False])
async def test_edit_batch_waiter_keeps_its_attempt_after_two_shared_misses(
    monkeypatch: pytest.MonkeyPatch, source_error: bool
) -> None:
    repo = CodeRepo()
    started = asyncio.Event()
    attempts = 0
    sites = [_DIRECT, _INDIRECT, {**_DIRECT, "caller": "tests::test_other"}]

    async def read(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal attempts
        attempts += 1
        ordinal = attempts
        await started.wait()
        if ordinal < len(sites):
            if source_error:
                raise RuntimeError("temporary source read failure")
            return []
        return _rows()

    monkeypatch.setattr(repo, "source_chunks_for_span", read)
    async with share_edit_example_reads():
        waiters = [asyncio.create_task(_read(repo, site)) for site in sites]
        await asyncio.sleep(0)
        started.set()
        results = await asyncio.gather(*waiters)
    assert attempts == 3
    assert results[:2] == sites[:2]
    assert results[2]["context"]["commit"] == "head"
    assert results[2]["caller"] == sites[2]["caller"]


@pytest.mark.asyncio
async def test_edit_batch_owns_a_read_when_its_last_waiter_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CodeRepo()
    entered, retired = asyncio.Event(), asyncio.Event()

    async def read(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            retired.set()
        return []

    monkeypatch.setattr(repo, "source_chunks_for_span", read)
    async with share_edit_example_reads():
        waiter = asyncio.create_task(_read(repo))
        await entered.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not retired.is_set()
    assert retired.is_set()
