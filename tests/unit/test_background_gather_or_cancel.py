"""`gather_or_cancel` re-raises the first error unwrapped, and only once every
sibling has been cancelled and has stopped."""

from __future__ import annotations

import asyncio

import pytest

from server.core.background import gather_or_cancel


@pytest.mark.asyncio
async def test_the_first_error_escapes_unwrapped_once_its_siblings_stopped() -> None:
    stopped: list[str] = []

    async def fails() -> None:
        await asyncio.sleep(0)
        raise ValueError("first")

    async def slow() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            stopped.append("slow")

    async def fails_on_cancel() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise RuntimeError("second") from None  # absorbed: the first error wins

    with pytest.raises(ValueError, match="first"):
        await gather_or_cancel(fails(), slow(), fails_on_cancel())
    assert stopped == ["slow"]  # stopped before the error reached the caller


@pytest.mark.asyncio
async def test_with_no_error_it_returns_every_result_in_order() -> None:
    async def value(n: int) -> int:
        await asyncio.sleep(0)
        return n

    assert await gather_or_cancel(value(1), value(2)) == [1, 2]
