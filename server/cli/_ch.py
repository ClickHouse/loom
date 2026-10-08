"""One ClickHouse connection for the whole of a CLI command."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.core.ch import CHClient


def run_connected[T](run: Callable[[CHClient], Awaitable[T]]) -> T:
    """`asyncio.run(run(ch))` on a connected client, disconnected on every
    path — an open client at loop close leaks its HTTP session."""
    from server import boot
    from server.core.background import register_buffer_drainer
    from server.core.ch import get_client
    from server.insights.app.provider_command import model_command

    register_buffer_drainer("boot-writes", boot.drain_writes)

    async def _go() -> T:
        ch = get_client()
        await ch.connect()
        try:
            async with model_command():
                return await run(ch)
        finally:
            await ch.disconnect()

    return asyncio.run(_go())
