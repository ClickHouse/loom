"""Content-free Linux process and owned-store measurements for hosted scale cases."""

from __future__ import annotations

import asyncio
import json
import math
import os
import resource
from pathlib import Path

from server.core.ch import get_client, set_query_tag


def percentiles(values: list[float], prefix: str) -> dict[str, float | int]:
    assert values
    values = sorted(values)
    return {
        prefix + "_count": len(values),
        **{
            prefix + "_p" + str(p): values[max(0, math.ceil(p * len(values) / 100) - 1)]
            for p in (50, 95, 99)
        },
    }


def process_sample() -> tuple[int, int, float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    peak = int(usage.ru_maxrss * 1024)
    sockets = 0
    for fd in Path("/proc/self/fd").iterdir():
        try:
            sockets += os.readlink(fd).startswith("socket:")
        except FileNotFoundError:
            continue
    return peak, sockets, usage.ru_utime + usage.ru_stime


def _bytes(value: str) -> float:
    for suffix, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024), ("B", 1)):
        if value.endswith(suffix):
            return float(value.removesuffix(suffix)) * scale
    raise ValueError("scale_store_memory_unit")


async def store_sample() -> tuple[float, float]:
    names = [os.environ["LOOM_CH_CONTAINER"], "mcp-source-scale-objects"]
    if os.environ["LOOM_STORAGE_AUTHORITY"] == "postgres":
        names += [os.environ["LOOM_PG_CONTAINER"], os.environ["LOOM_PG_CONTAINER"] + "-standby"]
    process = await asyncio.create_subprocess_exec(
        "docker",
        "stats",
        "--no-stream",
        "--format",
        "{{json .}}",
        *names,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(10):
            output, _ = await process.communicate()
        assert process.returncode == 0 and len(output) <= 16384
        rows = [json.loads(line) for line in output.splitlines()]
        assert len(rows) == len(names)
        return (
            sum(_bytes(row["MemUsage"].split(" / ")[0]) for row in rows),
            sum(float(row["CPUPerc"].removesuffix("%")) for row in rows),
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


class Sampler:
    def __init__(self) -> None:
        self.rss = self.sockets = self.cpu = self.store_rss = self.store_cpu = 0.0
        self.idle = 0
        self.task: asyncio.Task[None] | None = None
        self.samples = 0

    async def sample(self) -> None:
        rss, sockets, cpu = await asyncio.to_thread(process_sample)
        self.rss, self.sockets, self.cpu = max(self.rss, rss), max(self.sockets, sockets), cpu
        if self.samples % 20 == 0:
            rss_store, cpu_store = await store_sample()
            self.store_rss = max(self.store_rss, rss_store)
            self.store_cpu = max(self.store_cpu, cpu_store)
        self.samples += 1

    async def start(self) -> None:
        await self.sample()
        self.idle = int(self.rss)
        self.task = asyncio.create_task(self.loop())

    async def loop(self) -> None:
        set_query_tag("worker.knowledge_source_sync")
        while True:
            await asyncio.sleep(0.1)
            await self.sample()

    async def close(self) -> None:
        assert self.task is not None
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        # A sampler failure invalidates metrics rather than disappearing in gather.
        if not self.task.cancelled():
            self.task.result()
        await self.sample()

    def metrics(self) -> dict[str, float | int]:
        return {
            "process_peak": 1,
            "rss_peak_bytes": self.rss,
            "rss_delta_bytes": max(0, self.rss - self.idle),
            "cpu_seconds": self.cpu,
            "process_sockets_peak": self.sockets,
            "source_sockets_peak": 0,
            "store_rss_peak_bytes": self.store_rss,
            "store_cpu_percent_peak": self.store_cpu,
        }


async def store_work(started: str) -> dict[str, int]:
    set_query_tag("worker.knowledge_source_sync")
    # system.query_log arrives asynchronously; flush only the job's disposable store.
    await get_client().execute("SYSTEM FLUSH LOGS")
    row = await get_client().query_one(
        "SELECT count() AS store_requests, sum(read_rows) AS store_read_rows, "
        "sum(read_bytes) AS store_read_bytes, sum(written_rows) AS store_written_rows, "
        "sum(written_bytes) AS store_written_bytes, max(memory_usage) AS store_memory_peak_bytes "
        "FROM system.query_log WHERE type = 'QueryFinish' "
        "AND event_time_microseconds >= parseDateTime64BestEffort({started:String}) "
        "AND is_initial_query AND current_database = 'loom'",
        params={"started": started},
    )
    assert row is not None
    return {key: int(value) for key, value in row.items()}
