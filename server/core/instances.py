"""loom.instances: one heartbeat row per running Loom process.

Every process writes its row at boot and every `LOOM_INSTANCE_HEARTBEAT_SECONDS`:
its build and schema version (the build's last migration number), the
fingerprints of what must match across the deployment
(`deployment_config.fingerprints`), whether it serves traffic, the workers
lease it contends for right now and its start time. `heartbeat_at` is the server's
clock. A row not renewed for three intervals, or one its process marked
stopped at shutdown, is not live.

The live rows give the rate limiter its replica count (the hosts of serving
rows), the workers lease the newer builds contending for it
(`leader.newer_build`) and the leader the configurations still in service
(`deployment_config.publish`). Each beat also compares this process with the
published snapshot (`deployment_config.check`) and runs the `on_beat` read (the
workers lease, `leader.read_holder`).

The table is KeeperMap, one key per process that the leader sweeps an hour after
it stops beating.

Each beat also writes the process's `loom.cell_heartbeats` row (its `LOOM_CELL`,
tenancy mode, fingerprint and the keys beside it, `cell_row`), the values the `loom cell`
commands (prepare, attach, update) compare across a cell's live processes
(docs/architecture/cells.md); the leader sweeps its old keys as it sweeps the instances'.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from functools import cache
from typing import Any

from server.core import cell, cell_router, deployment_config, readiness
from server.core.ch import query_tag, set_query_tag
from server.settings import settings

log = logging.getLogger(__name__)

TABLE = "loom.instances"
CELL_TABLE = "loom.cell_heartbeats"
QUERY_TAG = "instances.heartbeat"

# A row is live while its last beat is younger than this many intervals.
_LIVE_INTERVALS = 3
# How often the leader drops the KeeperMap keys of processes gone an hour.
_PRUNE_EVERY_S = 600.0
# How old a row's last beat is before the leader drops its key. A contract across builds,
# not a tunable: attach reads stopped rows back that far (`read_recent`) while the leader
# that sweeps them may run another build, so a change is a design change of its own.
PRUNE_AFTER_S = 3600  # not a setting: a contract across builds

# (pid, instance id, start time) of this process.
_process: tuple[int, str, datetime] | None = None
_label = ""


def _no_lease() -> str:
    return ""


async def _no_read(_ch: Any) -> None:
    pass


_workers_lease: Callable[[], str] = _no_lease
_on_beat: Callable[[Any], Awaitable[None]] = _no_read
_pruned_at = -math.inf  # monotonic counts from boot: a fresh host prunes at once
# (monotonic read time, the live rows of this deployment); None = never read.
_live: tuple[float, list[dict[str, Any]]] | None = None


def _this_process() -> tuple[int, str, datetime]:
    """Fresh per process, a forked worker included."""
    global _process
    if _process is None or _process[0] != os.getpid():
        _process = (os.getpid(), uuid.uuid4().hex, datetime.now(UTC))
    return _process


def instance_id() -> str:
    return _this_process()[1]


def _host() -> str:
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"


@cache
def schema_version() -> int:
    """This build's last migration number."""
    from server.core.paths import MIGRATIONS_DIR
    from server.core.schema.journal import migration_number

    return max((migration_number(f.name) or 0 for f in MIGRATIONS_DIR.glob("*.sql")), default=0)


def row(*, stopped: bool = False) -> dict[str, Any]:
    return {
        "instance_id": instance_id(),
        "deployment": int(bool(settings.deployment)),
        "label": _label,
        "host": _host(),
        "pid": os.getpid(),
        "build_commit": settings.build_commit,
        "schema_version": schema_version(),
        # A draining or drifted process, or one the cell gate refuses, answers no traffic:
        # the live replicas' rate-limit shares leave it out.
        "serving": int(
            bool(settings.serves_traffic)
            and not readiness.draining()
            and not deployment_config.drift()
            and not cell.gate_reason()
        ),
        "workers_lease": _workers_lease(),
        **deployment_config.fingerprints(settings),
        "started_at": _this_process()[2],
        "stopped": int(stopped),
    }


def live_window_s() -> float:
    return _LIVE_INTERVALS * float(settings.instance_heartbeat_seconds)


def boot_allowance_s() -> float:
    """How long a booting process takes to report its first cell row and its first router
    answer: two beats and one router read."""
    return 2 * float(settings.instance_heartbeat_seconds) + float(settings.cell_router_timeout_s)


async def beat(ch: Any, *, stopped: bool = False) -> None:
    """Write this process's row. A KeeperMap insert replaces the key."""
    await ch.insert(TABLE, [row(stopped=stopped)])


def _attach_observation(participant_id: str) -> dict[str, Any] | None:
    """Unsupported lifecycle or sub-millisecond cadence reports no attach capability."""
    from server.core.provider_process import has_certificate, participant_proof
    from server.core.retirement import get_gate

    role = get_gate().role
    if role not in ("server", "worker"):
        return None
    try:
        proof = participant_proof()
    except ValueError:
        return None
    period = Decimal(str(settings.instance_heartbeat_seconds))
    if not has_certificate(proof) or not period.is_finite():
        return None
    numerator, denominator = period.as_integer_ratio()
    period_ms, remainder = divmod(numerator * 1000, denominator)
    if period_ms <= 0 or remainder:
        return None
    return {
        "protocol": 1,
        "role": role,
        "participant_id": participant_id,
        "host_proof": proof,
        "keys_fp": deployment_config.fingerprints(settings)["keys_fp"],
        "accepting": not cell.gate_reason(),
        "period_ms": period_ms,
    }


def cell_row() -> dict[str, Any]:
    """What this process reports about its cell (`loom.cell_heartbeats`). Beside the
    fingerprint's version and fields, its JSON carries keys no fingerprint compares
    (docs/architecture/cells.md, "The heartbeat's keys"): `serves` (LOOM_SERVES_TRAFFIC
    on a server, false on a worker), `attach_observation` (this process's guarded birth),
    `participant_id` (this process's registered provider identity), `marked`
    (whether this process's pooled sessions keep the tenancy marker, null until a
    cell-state read lands), and with LOOM_CELL set, until the `first` row is read and then
    on a first cell only, `router` and `router_why` (`cell_router.router_key`)."""
    from server.core.provider_process import process_identity
    from server.core.retirement import get_gate

    participant_id = str(process_identity())
    reported = {
        **cell.as_value(cell.mine()),
        "serves": bool(settings.serves_traffic) and get_gate().role != "worker",
        "participant_id": participant_id,
        "attach_observation": _attach_observation(participant_id),
        "marked": cell.marked(),
        **cell_router.router_key(),
    }
    return {
        "instance_id": instance_id(),
        "cell": settings.cell,
        "tenancy_mode": cell.reported_mode(),
        "fingerprint": json.dumps(reported, sort_keys=True),
    }


async def cell_beat(ch: Any) -> None:
    await ch.insert(CELL_TABLE, [cell_row()])


async def _cell_loop(ch: Any) -> None:
    set_query_tag(QUERY_TAG)
    while True:
        await asyncio.sleep(float(settings.instance_heartbeat_seconds))
        try:
            await cell_beat(ch)
        except Exception as error:  # noqa: BLE001 — the next tick retries a missed observation.
            log.warning("cell heartbeat failed: %s: %s", type(error).__name__, error)


async def start_cell_heartbeat(ch: Any) -> asyncio.Task[None]:
    """Acknowledge the child's first cell row before its supervised work starts."""
    with query_tag(QUERY_TAG):
        await cell_beat(ch)
    return asyncio.create_task(_cell_loop(ch), name="loom-worker-cell-heartbeat")


async def read_live(ch: Any, *, all_deployments: bool = False) -> list[dict[str, Any]]:
    """Newest live beat per process. Ordinary reads cache this process's kind;
    security checks can include every process sharing the database."""
    global _live
    rows = await ch.query_all(
        f"""
        SELECT instance_id,
               argMax(host, heartbeat_at) AS host,
               argMax(label, heartbeat_at) AS label,
               argMax(build_commit, heartbeat_at) AS build_commit,
               argMax(schema_version, heartbeat_at) AS schema_version,
               argMax(serving, heartbeat_at) AS serving,
               argMax(workers_lease, heartbeat_at) AS workers_lease,
               argMax(settings_fp, heartbeat_at) AS settings_fp,
               argMax(keys_fp, heartbeat_at) AS keys_fp,
               argMax(config_fp, heartbeat_at) AS config_fp,
               argMax(stopped, heartbeat_at) AS last_stopped,
               toString(min(started_at)) AS started_at,
               toString(max(heartbeat_at)) AS last_beat
        FROM {TABLE}
        WHERE ({{all_deployments:Bool}} OR deployment = {{dep:UInt8}})
          AND heartbeat_at > now64(3) - toIntervalMillisecond({{ms:UInt32}})
        GROUP BY instance_id
        HAVING last_stopped = 0
        """,
        params={
            "all_deployments": all_deployments,
            "dep": int(bool(settings.deployment)),
            "ms": int(live_window_s() * 1000),
        },
    )
    out = [dict(r) for r in rows]
    if not all_deployments:
        _live = (time.monotonic(), out)
    return out


async def read_recent(
    ch: Any, *, window_s: float, since: str | None = None
) -> list[dict[str, Any]]:
    """Every process of every deployment sharing this ClickHouse whose last beat plus
    `window_s` reaches past `since` (a ClickHouse `DateTime64(3)` as text), or past now
    without one: stopped rows included, which `read_live` drops. `stopped` is its
    newest row's flag and `live` whether that row is inside the live window and not
    stopped, both judged in ClickHouse."""
    rows = await ch.query_all(
        f"""
        SELECT instance_id,
               argMax(keys_fp, heartbeat_at) AS keys_fp,
               argMax(stopped, heartbeat_at) AS stopped,
               toString(max(heartbeat_at)) AS last_beat,
               max(heartbeat_at) > now64(3) - toIntervalMillisecond({{live:UInt32}})
                   AND stopped = 0 AS live
        FROM {TABLE}
        GROUP BY instance_id
        HAVING max(heartbeat_at) + toIntervalMillisecond({{wide:UInt32}})
               > if({{since:String}} = '', now64(3), toDateTime64({{since:String}}, 3))
        """,
        params={
            "live": int(live_window_s() * 1000),
            "wide": int(window_s * 1000),
            "since": since or "",
        },
    )
    return [dict(r) for r in rows]


def live_rows() -> list[dict[str, Any]]:
    """The live rows as of the last read; none before the first read or once
    that read is older than the live window (ClickHouse unreachable)."""
    if _live is None or time.monotonic() - _live[0] > live_window_s():
        return []
    return _live[1]


def live_hosts() -> int | None:
    """Distinct hosts with a live serving row in this deployment, as of the
    last read: a workers-only process (LOOM_SERVES_TRAFFIC=false) shares no
    load. None outside the deployment, or when no such row was read."""
    if not settings.deployment:
        return None
    hosts = len({r.get("host") for r in live_rows() if r.get("serving")})
    return hosts or None


async def _prune(ch: Any) -> None:
    """Drop the KeeperMap keys of processes gone an hour. Leader only."""
    global _pruned_at
    if time.monotonic() - _pruned_at < _PRUNE_EVERY_S:
        return
    _pruned_at = time.monotonic()
    # Each table's sweep stands alone: one failing leaves the other to run.
    try:
        await ch.execute(
            "ALTER TABLE loom.instances DELETE "
            f"WHERE heartbeat_at < now64(3) - toIntervalSecond({PRUNE_AFTER_S})"
        )
    except Exception as e:  # noqa: BLE001 — a later sweep retries
        log.warning("instance row sweep failed: %s", e)
    try:
        await ch.execute(
            "ALTER TABLE loom.cell_heartbeats DELETE "
            f"WHERE heartbeat_at < now64(3) - toIntervalSecond({PRUNE_AFTER_S})"
        )
    except Exception as e:  # noqa: BLE001 — a later sweep retries
        log.warning("cell heartbeat sweep failed: %s", e)


async def tick(ch: Any, *, leader: bool) -> None:
    """One beat: write the row and the cell row, read the live ones, read what `on_beat`
    reads (the workers lease), compare this process with the published configuration
    and, on the leader, publish this one when the deployment has moved to it and sweep
    old keys. Never raises."""
    live: list[dict[str, Any]] | None = None
    try:
        await beat(ch)
        live = await read_live(ch)
    except Exception as e:  # noqa: BLE001 — a missed beat must not stop the loop
        log.warning("instance heartbeat failed: %s: %s", type(e).__name__, e)
    if live is not None:  # the beat reached ClickHouse
        try:
            await _on_beat(ch)
        except Exception as e:  # noqa: BLE001 — a missed read must not stop the loop
            log.warning("instance beat read failed: %s: %s", type(e).__name__, e)
    try:
        await cell_beat(ch)
    except Exception as e:  # noqa: BLE001 — a missed cell beat must not stop the loop
        log.warning("cell heartbeat failed: %s: %s", type(e).__name__, e)
    await deployment_config.check(ch)
    if leader and live is not None:
        await deployment_config.publish(ch, live=live, published_by=_label)
        await _prune(ch)  # each table's sweep catches its own failure


async def publish_config(ch: Any) -> None:
    """The leader's publish at boot or on winning the lease (`tick` retries it)."""
    with query_tag(QUERY_TAG):
        try:
            live = await read_live(ch)
        except Exception as e:  # noqa: BLE001 — the next beat retries
            log.warning(
                "deployment config not published: live instances unread: %s: %s",
                type(e).__name__,
                e,
            )
            return
        await deployment_config.publish(ch, live=live, published_by=_label)


async def _loop(ch: Any, is_leader: Callable[[], bool]) -> None:
    set_query_tag(QUERY_TAG)
    while True:
        await asyncio.sleep(float(settings.instance_heartbeat_seconds))
        await tick(ch, leader=is_leader())


async def start(
    ch: Any,
    is_leader: Callable[[], bool],
    *,
    label: str,
    workers_lease: Callable[[], str] = _no_lease,
    on_beat: Callable[[Any], Awaitable[None]] = _no_read,
) -> asyncio.Task[None]:
    """Beat and check once before the process serves, then on a timer. `label`
    is the process's LOOM_INSTANCE_LABEL; `workers_lease` says, at each beat,
    the lease it contends for ('' when none); `on_beat` is a read each beat runs."""
    global _label, _workers_lease, _on_beat
    _label = label
    _workers_lease = workers_lease
    _on_beat = on_beat
    with query_tag(QUERY_TAG):
        await tick(ch, leader=is_leader())
    return asyncio.create_task(_loop(ch, is_leader), name="loom-instance-heartbeat")


# A shutdown's stop write gets this long: past it the row ages out instead.
STOP_S = 5.0


async def stop(ch: Any) -> None:
    """Mark this process's row stopped, so it stops counting at once; best effort,
    within `STOP_S`."""
    from server.core.ch import statement_timeout

    try:
        async with asyncio.timeout(STOP_S):
            with query_tag(QUERY_TAG), statement_timeout(STOP_S):
                await beat(ch, stopped=True)
    except Exception as e:  # noqa: BLE001 — shutdown goes on; the row ages out
        log.warning("instance row not marked stopped: %s: %s", type(e).__name__, e)


def _reset_for_tests() -> None:
    global _live, _pruned_at, _on_beat
    _live = None
    _pruned_at = -math.inf
    _on_beat = _no_read


__all__ = [
    "CELL_TABLE",
    "PRUNE_AFTER_S",
    "TABLE",
    "beat",
    "boot_allowance_s",
    "cell_beat",
    "cell_row",
    "instance_id",
    "live_hosts",
    "live_rows",
    "live_window_s",
    "publish_config",
    "read_live",
    "read_recent",
    "schema_version",
    "start",
    "start_cell_heartbeat",
    "stop",
    "tick",
]
