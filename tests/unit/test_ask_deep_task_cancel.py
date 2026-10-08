"""The up-front deep-evidence selection memory.ask starts before its first
answer is cancelled on every exit: a first answer that raises must not leave
the selection's model call running after the ask has failed."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app.ask import cascade
from server.settings import settings


def _run() -> Any:
    return SimpleNamespace(
        search_input=SimpleNamespace(query="when did I move?"),
        question="when did I move?",
        settings=settings,
        ranked=[],
        memories=[],
    )


async def _no_turns(*a: Any, **kw: Any) -> tuple[str, str]:
    return "", ""


@pytest.mark.asyncio
async def test_a_failed_first_answer_cancels_the_deep_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()

    async def _slow_deep_pass(*a: Any, **kw: Any) -> Any:
        started.set()
        await asyncio.sleep(3600)

    async def _boom(*a: Any, **kw: Any) -> None:
        await started.wait()
        raise RuntimeError("synthesis failed")

    monkeypatch.setattr(cascade, "_run_deep_pass", _slow_deep_pass)
    monkeypatch.setattr(cascade, "_order_window_turns", _no_turns)
    monkeypatch.setattr(cascade, "_first_answer", _boom)

    ans = cascade._AskAnswer(tier="strong", reason="", client=object())
    with pytest.raises(RuntimeError, match="synthesis failed"):
        await cascade._answer_with_deep_pass(
            _run(),
            SimpleNamespace(),
            ans,
            llm=None,
            llm_cheap=None,
            depth="raw",
            temporal_question=False,
            deep_select="top",
            deep_first=False,
        )

    assert ans.deep_task is not None
    await asyncio.sleep(0)  # let the cancellation land
    assert ans.deep_task.cancelled()


@pytest.mark.asyncio
async def test_the_normal_path_still_consumes_the_deep_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deep answer awaits the task started up front; the cleanup leaves
    a finished task alone."""
    consumed: list[Any] = []

    async def _deep_pass(*a: Any, **kw: Any) -> Any:
        return SimpleNamespace(block="")

    async def _first(*a: Any, **kw: Any) -> None:
        return None

    async def _deep(run: Any, reader: Any, ans: Any, **kw: Any) -> None:
        consumed.append(await ans.deep_task)

    monkeypatch.setattr(cascade, "_run_deep_pass", _deep_pass)
    monkeypatch.setattr(cascade, "_order_window_turns", _no_turns)
    monkeypatch.setattr(cascade, "_first_answer", _first)
    monkeypatch.setattr(cascade, "_deep_answer", _deep)

    ans = cascade._AskAnswer(tier="strong", reason="", client=object())
    await cascade._answer_with_deep_pass(
        _run(),
        SimpleNamespace(),
        ans,
        llm=None,
        llm_cheap=None,
        depth="raw",
        temporal_question=False,
        deep_select="top",
        deep_first=False,
    )

    assert ans.deep_task is not None and ans.deep_task.done()
    assert not ans.deep_task.cancelled()
    assert consumed and consumed[0].block == ""
