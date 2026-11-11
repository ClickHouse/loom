"""The workers child's composition root, as `server/main.py` is the serving
process's: `python -m server.worker_main --lease-fd N --alive-fd M`, started by
`server.workers.child.ChildWorkers`.

It runs the supervisor and only what the workers use: the ClickHouse client,
the provider configuration (and the participant row a provider transition
waits for), the event emitter, the tracker and repository registries and the
similarity gates, and, with `LOOM_CELL` set, the cell's own rows (its passes verify
tokens). No port, no instance row, no migrations, no token state.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import threading
from typing import Any

from server import boot
from server.settings import settings
from server.workers import child, leader
from server.workers.supervisor import get_supervisor

log = logging.getLogger("loom.workers.child")


async def _start_services(ch: Any) -> list[asyncio.Task[None]]:
    from server.code.app.index import repo_registry
    from server.core.background import register_buffer_drainer
    from server.core.ch import query_tag, set_query_tag
    from server.core.model_work_gate import get_model_work_gate
    from server.insights.app.provider_barrier import register_process
    from server.insights.app.provider_runtime import load_active_configuration, refresh_loop
    from server.observability import get_emitter
    from server.tracker.app import registry_sources

    async def _trackers() -> None:
        set_query_tag("tracker.sources_refresh")  # its own task: the tag stays in it
        while True:
            await asyncio.sleep(max(5.0, float(settings.tracker_sources_refresh_seconds)))
            await registry_sources.refresh_db_sources()

    # `with`, not set_query_tag: the supervisor and the shutdown run in this context after.
    with query_tag("boot.workers_child"):
        register_buffer_drainer("boot-writes", boot.drain_writes)
        await register_process()
        await load_active_configuration()
        await get_emitter().start()
        await registry_sources.refresh_db_sources(force=True, ch=ch)
        if not get_model_work_gate().closed:
            await boot.calibrate_similarity_gates()
        tasks = [
            asyncio.create_task(refresh_loop(), name="provider-configuration-refresh"),
            asyncio.create_task(_trackers(), name="loom-tracker-sources-refresh"),
            await repo_registry.start_refresher(ch),
            # The workers read LIVE settings too: their passes see the deployment's overrides.
            boot.start_runtime_overrides(),
        ]
        await boot.await_warm_up(settings.boot_warmup_timeout_s, ("runtime_overrides",))
    return tasks


async def _drain() -> None:
    """The child's writes land before its client goes."""
    from server.clients import _http as _http_clients
    from server.observability import get_emitter, langfuse_scores, otel

    await boot.drain_writes()
    await langfuse_scores.drain(timeout_s=5.0)
    await get_emitter().stop()
    otel.shutdown()
    await _http_clients.aclose()


async def _serve(fd: int, alive_fd: int) -> None:
    from server import boot
    from server.core.ch import get_client
    from server.insights.app.provider_barrier import SHUTDOWN_RETIRE_S
    from server.memory.app import embedder_calibration

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    lease = child.ChildLease()
    leader.set_current(lease)
    threading.Thread(
        target=child.read_deadlines,
        args=(fd, lease, lambda: loop.call_soon_threadsafe(stop.set)),
        name="loom-workers-child-lease",
        daemon=True,
    ).start()
    threading.Thread(
        target=child.exit_past_deadline,
        args=(lease, leader.drain_timeout_s()),
        name="loom-workers-child-deadline",
        daemon=True,
    ).start()
    beat = asyncio.create_task(child.beat(alive_fd), name="loom-workers-child-beat")
    ch = get_client()
    await ch.connect()
    await boot.open_storage_authority()
    try:
        services = await _start_services(ch)
        if settings.cell:
            # The passes verify tokens and judge the gate as the parent does: the child
            # reads the cell's rows itself.
            services += await boot.start_cell_state()
        supervisor = get_supervisor()
        supervisor.start_all()
        await stop.wait()
        log.info("workers child %s stopping", os.getpid())
        embedder_calibration.stop_refresh_loop()
        for task in services:
            task.cancel()
        await asyncio.gather(*services, return_exceptions=True)
        await supervisor.stop_all(drain_timeout_s=leader.drain_timeout_s())
        await _drain()
    finally:
        beat.cancel()
        await boot.leave_deployment(ch, budget_s=SHUTDOWN_RETIRE_S)
        await boot.close_storage_authority()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m server.worker_main")
    parser.add_argument("--lease-fd", type=int, required=True)
    parser.add_argument("--alive-fd", type=int, required=True)
    args = parser.parse_args(argv)
    child.die_with_parent()
    from server.core.logging_config import configure_logging
    from server.observability import otel
    from server.wiring import wire

    configure_logging()
    wire()
    otel.init_otel()
    asyncio.run(_serve(args.lease_fd, args.alive_fd))


if __name__ == "__main__":
    main()
