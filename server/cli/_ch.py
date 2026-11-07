"""One ClickHouse connection for the whole of a CLI command."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.core.ch import CHClient


async def connect_checked(ch: CHClient) -> None:
    """Connect `ch`, and refuse (disconnected) a command configured for another memory
    authority than the deployment's, as a replica refuses: an erasure or an import run
    against the wrong one would leave or write the other store's rows."""
    from server.core.pg_profile import check_configured

    await ch.connect()
    try:
        await check_configured(ch)
    except BaseException:
        await disconnect(ch)
        raise


async def disconnect(ch: CHClient) -> None:
    """Close the Postgres pool a command opened, then `ch`."""
    from server.core import pg

    pg.expect_deployment(None)
    try:
        await pg.close_pool()
    finally:
        await ch.disconnect()


def run_connected[T](run: Callable[[CHClient], Awaitable[T]]) -> T:
    """`asyncio.run(run(ch))` on a connected client (`connect_checked`), disconnected on
    every path — an open client at loop close leaks its HTTP session."""
    from server import boot
    from server.core.background import register_buffer_drainer
    from server.core.ch import get_client
    from server.insights.app.provider_command import model_command

    register_buffer_drainer("boot-writes", boot.drain_writes)

    async def _go() -> T:
        ch = get_client()
        await connect_checked(ch)
        try:
            async with model_command():
                return await run(ch)
        finally:
            await disconnect(ch)

    return asyncio.run(_go())
