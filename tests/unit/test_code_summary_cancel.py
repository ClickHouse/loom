"""A code_summary pass cancelled mid fan-out (its deadline, or shutdown) takes
its in-flight cards with it: none keeps running, or writing, after the pass."""

from __future__ import annotations

import asyncio

import pytest

from server.code.workers.summary import _run_cards


@pytest.mark.parametrize("candidates", [3, 8])
async def test_cancelling_the_pass_cancels_every_card_in_flight(candidates: int) -> None:
    started: list[str] = []
    cancelled: list[str] = []

    async def _one(path: str) -> None:
        started.append(path)
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(path)
            raise

    paths = [f"f{i}.py" for i in range(candidates)]
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await _run_cards(paths, _one, 2, lambda: False)
    for _ in range(5):
        await asyncio.sleep(0)
    assert started
    assert sorted(cancelled) == sorted(started)


async def test_one_repositorys_failure_does_not_skip_the_rest(monkeypatch) -> None:
    """A read that raises before the per-file loop (live hashes, the budget
    check, the model client) does not end the pass for every repository after
    it; a cancellation still ends it."""
    from types import SimpleNamespace

    import server.code.domain.index.repos as repos
    from server.code.workers import summary as w
    from server.workers import pass_context

    monkeypatch.setattr(
        repos,
        "configured_repos",
        lambda: [SimpleNamespace(org="o", namespace=n) for n in ("a", "b", "c")],
    )
    seen: list[str] = []

    async def fake_repo(org, ns, result, **kw):
        seen.append(ns)
        if ns == "a":
            raise RuntimeError("live_file_hashes timed out")
        if ns == "c":
            raise asyncio.CancelledError

    monkeypatch.setattr(w, "_summarize_repo", fake_repo)
    result = w.CodeSummaryResult()
    ctx = pass_context.begin("code_summary")
    try:
        with pytest.raises(asyncio.CancelledError):
            await w._run(result)
    finally:
        pass_context.clear()
    assert seen == ["a", "b", "c"]
    assert result.errors == ["a: RuntimeError: live_file_hashes timed out"] and ctx.count == 1
