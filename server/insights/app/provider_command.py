"""Enroll native CLI data-plane work for the duration of the entire command."""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from server.core import settings_overrides
from server.core.background import drain_task_sets, flush_registered_buffers
from server.core.ch import set_query_tag
from server.core.model_work_gate import get_model_work_gate
from server.insights.app.provider_barrier import register_process, retire_process
from server.insights.app.provider_runtime import load_active_configuration
from server.settings import settings

log = logging.getLogger(__name__)


async def _adopt_overrides() -> None:
    try:
        await settings_overrides.refresh()
    except Exception as e:  # noqa: BLE001 — the command runs on the values it has, said once
        log.warning("live-setting overrides not adopted: %s", e)


async def _keep_overrides() -> None:
    """A long command (a rechunk, a re-embed) follows an override set while it runs."""
    set_query_tag("cli.settings_overrides")
    while True:
        await asyncio.sleep(max(1.0, float(settings.runtime_overrides_refresh_seconds)))
        await _adopt_overrides()


@asynccontextmanager
async def model_command() -> AsyncIterator[None]:
    """A running command blocks a rebuild; a newborn loads the gate first. The
    deployment's live-setting overrides apply before the work starts, as they do
    in every server process, so a native command and the HTTP op agree."""
    await register_process()
    keeper: asyncio.Task[None] | None = None
    try:
        await load_active_configuration()
        await _adopt_overrides()
        keeper = asyncio.create_task(_keep_overrides(), name="loom-cli-settings-overrides")
        async with get_model_work_gate().admit():
            yield
    finally:
        if keeper is not None:
            keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await keeper
        if await drain_task_sets(timeout_s=15) and await flush_registered_buffers():
            await retire_process()
