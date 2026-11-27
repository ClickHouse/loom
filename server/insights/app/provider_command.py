"""Enroll native CLI data-plane work for the duration of the entire command."""

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from server.core import retirement, settings_overrides
from server.core.background import drain_task_sets, flush_registered_buffers
from server.core.ch import get_client, set_query_tag
from server.core.ch_errors import error_line
from server.core.model_work_gate import get_model_work_gate
from server.core.provider_process import participant_proof, process_identity
from server.insights.app.provider_barrier import _retire_until, register_process
from server.insights.app.provider_runtime import load_active_configuration
from server.settings import settings

log = logging.getLogger(__name__)


async def _adopt_overrides(*, failing: bool = False) -> bool:
    """Whether the stored overrides loaded. A failure is logged when a run of them
    starts: a command that closes its own client fails every refresh until it
    reopens it."""
    try:
        await settings_overrides.refresh()
        return True
    except Exception as e:  # noqa: BLE001 — the command runs on the values it has
        if not failing:
            log.warning("live-setting overrides not adopted: %s", e)
        return False


async def _keep_overrides(
    loaded: bool, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> SystemExit | KeyboardInterrupt | None:
    """A long command (a rechunk, a re-embed) follows an override set while it runs."""
    set_query_tag("cli.settings_overrides")
    try:
        while True:
            await sleep(max(1.0, float(settings.runtime_overrides_refresh_seconds)))
            loaded = await _adopt_overrides(failing=not loaded)
    except (SystemExit, KeyboardInterrupt) as error:
        # A task's fatal exception must reach its owner before the loop stops.
        return error


async def _drain_participant_until(end: float) -> None:
    loop = asyncio.get_running_loop()
    async with asyncio.timeout_at(end):
        if not await drain_task_sets(timeout_s=max(0.0, end - loop.time())):
            raise RuntimeError("Native command deferred writes did not drain")
        if not await flush_registered_buffers():
            raise RuntimeError("Native command buffers did not flush")
        try:
            certified = await _retire_until(get_client(), end)
        except retirement.Uncertifiable as e:
            log.warning(
                "provider participant %s not retired, its writes settled: %s", process_identity(), e
            )
            return
        if certified is not True:
            raise RuntimeError("Native command retirement was not certified")


async def _finish_participant() -> None:
    gate = retirement.get_gate()
    gate.close()
    from server.memory.app import embedder_calibration

    loop = asyncio.get_running_loop()
    end = loop.time() + settings.native_command_drain_timeout_s
    first = gate.producer_failure
    stopped = False
    try:
        embedder_calibration.stop_refresh_loop()
        stopped = True
    except BaseException as error:  # noqa: BLE001 — finish safe cleanup, then raise the first failure
        if first is None:
            first = error
    producers = tuple(gate.producers)
    for producer in producers:
        producer.cancel()
    try:
        if producers:
            _, pending = await asyncio.wait(producers, timeout=max(0.0, end - loop.time()))
            if pending:
                raise RuntimeError("Native command producers did not stop")
    except BaseException as error:  # noqa: BLE001 — finish safe cleanup, then raise the first failure
        if first is None:
            first = error
        stopped = False
    if first is None:
        first = gate.producer_failure
    if stopped:
        gate.stopped()
        owner = asyncio.current_task()
        while True:
            cancelling = owner.cancelling() if owner is not None else 0
            try:
                if loop.time() >= end:
                    raise TimeoutError("Native command cleanup deadline expired")
                await _drain_participant_until(end)
            except asyncio.CancelledError as error:
                if first is None:
                    first = error
                if owner is not None and owner.cancelling() > cancelling:
                    continue  # a new owner cancellation still allows cleanup within the same end
            except BaseException as error:  # noqa: BLE001 — report the first required cleanup failure
                if first is None:
                    first = error
            break
    if first is not None:
        raise first


@asynccontextmanager
async def participant_command() -> AsyncIterator[None]:
    """Register all native org/principal work, including commands without models."""
    retirement.get_gate().install("command")
    await register_process()
    try:
        async with retirement.get_gate().admit():
            yield
    except BaseException:  # Keep the body failure while attempting required retirement cleanup.
        with contextlib.suppress(BaseException):
            await _finish_participant()
        raise
    else:
        try:
            await _finish_participant()
        except Exception as error:
            # A tracked producer's own failure is not the shutdown's: it keeps its traceback.
            if error is not retirement.get_gate().producer_failure:
                error.add_note(retirement.UNFINISHED)  # one line from `loom`
            raise


def unfinished_line(error: BaseException) -> str | None:
    """What `loom` prints, in place of a traceback, when a command's body returned and
    its shutdown then failed (`retirement.unfinished`); None for any other error."""
    if not retirement.unfinished(error):
        return None
    me = process_identity()
    remedy = (
        f"retire it with `loom participant retire --participant {me}` on this host"
        if participant_proof()
        else "no command can retire it: this host gives no process identity"
    )
    return (
        f"the command returned, but its shutdown did not complete ({error_line(error)}): "
        f"a deferred write may not have landed, and a provider change waits for participant {me}; "
        f"{remedy}"
    )


@asynccontextmanager
async def model_command() -> AsyncIterator[None]:
    """The permanent lifecycle also holds the reversible model-work admission."""
    async with participant_command():
        keeper: asyncio.Task[SystemExit | KeyboardInterrupt | None] | None = None
        try:
            await load_active_configuration()
            loaded = await _adopt_overrides()
            keeper = asyncio.create_task(
                _keep_overrides(loaded), name="loom-cli-settings-overrides"
            )
            async with get_model_work_gate().admit():
                yield
        finally:
            first = sys.exception()
            if keeper is not None:
                owner = asyncio.current_task()
                assert owner is not None
                cancelling = owner.cancelling()
                requested = keeper.cancel()
                try:
                    try:
                        fatal = await keeper
                    except asyncio.CancelledError:
                        if not requested or owner.cancelling() != cancelling:
                            raise
                    else:
                        if fatal is not None:
                            raise fatal
                except BaseException:  # Keep the body's first failure through keeper cleanup.
                    if first is None:
                        raise
