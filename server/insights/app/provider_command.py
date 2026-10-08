"""Enroll native CLI data-plane work for the duration of the entire command."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from server.core.background import drain_task_sets, flush_registered_buffers
from server.core.model_work_gate import get_model_work_gate
from server.insights.app.provider_barrier import register_process, retire_process
from server.insights.app.provider_runtime import load_active_configuration


@asynccontextmanager
async def model_command() -> AsyncIterator[None]:
    """A running command blocks a rebuild; a newborn loads the gate first."""
    await register_process()
    try:
        await load_active_configuration()
        async with get_model_work_gate().admit():
            yield
    finally:
        if await drain_task_sets(timeout_s=15) and await flush_registered_buffers():
            await retire_process()
