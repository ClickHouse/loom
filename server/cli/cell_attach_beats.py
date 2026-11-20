"""The first attach's judgment of the cell's processes, from their heartbeats
(docs/architecture/cells.md, "Attaching a cell" and "The heartbeat's keys").

Every check reads both heartbeat tables across every deployment sharing the ClickHouse, as
prepare does, stopped rows included (`instances.read_recent`), and judges in one order:

- `cell` first: a live process whose `LOOM_CELL` is unset or another cell refuses at once;
  a stopped one, or a row with no cell row that is no longer live, may still be writing
  for the widened window after its stop beat, and is refused, or waited on until it ages
  out, as the caller's `stopped_unset` says;
- then the keys a current build reports (`serves`, `marked`, `router`): a cell row
  without them refuses, even while booting;
- then booting: a live instance with no cell row yet, no tenancy mode yet, `marked` null,
  or `router` null where `serves` is true, is waited on; `marked` false, or `router` false
  where `serves` is true, refuses, naming the process and the router's cause;
- and the fingerprint: each live process's matches `loom.cell_self`'s current or next.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from server.cli.cell_attach_pg import Wait
from server.core import cell, instances
from server.core.cell_ch import Refusal, Refused

StoppedUnset = Literal["refuse", "wait"]


def widened_window_s() -> float:
    """The live window plus what a stopping process still does after its stop beat: it
    drains its ClickHouse writes, then settles cut-off ones. A process whose last beat,
    stopped or not, is younger than this may still write."""
    from server.boot import SHUTDOWN_DRAIN_BUDGET_S
    from server.insights.app.provider_barrier import SHUTDOWN_RETIRE_S

    return instances.live_window_s() + SHUTDOWN_DRAIN_BUDGET_S + SHUTDOWN_RETIRE_S


@dataclass(frozen=True)
class Beat:
    """One process: its instance row (`live`: inside the live window and not stopped) and
    its cell row (`cell` None when it wrote none), the fingerprint JSON decoded."""

    instance_id: str
    live: bool
    stopped: bool
    keys_fp: str
    last_beat: str
    cell: str | None = None
    mode: str = ""
    value: dict[str, Any] | None = None


async def read_beats(ch: Any, *, since: str | None = None) -> list[Beat]:
    rows = await instances.read_recent(ch, window_s=widened_window_s(), since=since)
    ids = [str(r["instance_id"]) for r in rows]
    cells = await ch.query_all(
        f"SELECT instance_id, cell, tenancy_mode, fingerprint FROM {instances.CELL_TABLE} "
        "WHERE has({ids:Array(String)}, instance_id)",
        {"ids": ids},
    )
    by_id = {str(r["instance_id"]): r for r in cells}
    out = []
    for r in rows:
        found = by_id.get(str(r["instance_id"]))
        beat = Beat(
            instance_id=str(r["instance_id"]),
            live=bool(r["live"]),
            stopped=bool(r["stopped"]),
            keys_fp=str(r["keys_fp"]),
            last_beat=str(r["last_beat"]),
            cell=None if found is None else str(found["cell"]),
            mode="" if found is None else str(found["tenancy_mode"]),
            value=None if found is None else _decoded(str(found["fingerprint"])),
        )
        out.append(beat)
    return out


def _decoded(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


@dataclass
class Verdict:
    """What one read found: the refusals, the processes waited on (booting, or a stopped
    process with `LOOM_CELL` unset aging out), the live processes judged, their `keys_fp`
    values, and whether one reports SCIM on."""

    refusals: list[Refusal] = field(default_factory=list)
    booting: list[str] = field(default_factory=list)
    aging: list[str] = field(default_factory=list)
    live: int = 0
    keys_fps: set[str] = field(default_factory=set)
    scim: bool = False


def judge(
    beats: Iterable[Beat],
    name: str,
    accepted: Iterable[Mapping[str, Any] | None],
    stopped_unset: StoppedUnset,
) -> Verdict:
    """The verdict on `beats` for the cell `name`, whose processes run `accepted` (its
    `loom.cell_self` current and next)."""
    values = [v for v in accepted if v is not None]
    out = Verdict()
    for b in beats:
        who = f"process {b.instance_id}"
        if b.cell is None:
            if b.live:
                out.booting.append(b.instance_id)
            else:
                _stopped(out, b, "stopped before reporting its LOOM_CELL", stopped_unset)
            continue
        if b.cell != name:
            runs = f"runs with LOOM_CELL={b.cell}" if b.cell else "runs with LOOM_CELL unset"
            if b.live:
                out.refusals.append(Refusal(who, f"{runs}: set LOOM_CELL={name} on it"))
            else:
                _stopped(out, b, f"{runs} and stopped", stopped_unset)
            continue
        if not b.live:
            continue
        value = b.value or {}
        if not all(k in value for k in ("serves", "marked", "router")):
            out.refusals.append(
                Refusal(who, "reports no serves, marked or router key: upgrade it to this release")
            )
            continue
        if value["marked"] is False:
            out.refusals.append(
                Refusal(
                    who,
                    "its Postgres sessions lose the tenancy marker (a pooler?): point "
                    "LOOM_PG_DSN straight at Postgres",
                )
            )
            continue
        serves = value["serves"] is True
        if serves and value["router"] is False:
            why = value.get("router_why") or "no cause reported"
            out.refusals.append(Refusal(who, f"cannot read its router: {why}"))
            continue
        if value["marked"] is None or (serves and value["router"] is None) or not b.mode:
            out.booting.append(b.instance_id)
            continue
        fields = value.get("fields")
        if not isinstance(fields, dict) or not any(cell.matches(fields, v) for v in values):
            out.refusals.append(
                Refusal(who, "runs neither loom.cell_self's current nor its next configuration")
            )
            continue
        out.live += 1
        out.keys_fps.add(b.keys_fp)
        out.scim = out.scim or bool(fields.get("scim"))
    return out


def _stopped(out: Verdict, b: Beat, what: str, stopped_unset: StoppedUnset) -> None:
    if stopped_unset == "wait":
        out.aging.append(b.instance_id)
        return
    out.refusals.append(
        Refusal(
            f"process {b.instance_id}",
            f"{what} at {b.last_beat}, and may still be writing (its drain and retirement "
            "after its stop beat)",
        )
    )


def final_refusals(verdict: Verdict) -> list[Refusal]:
    out = []
    if not verdict.live and not verdict.booting:
        out.append(Refusal("processes", "no live process of this cell reports a heartbeat"))
    if len(verdict.keys_fps) > 1:
        out.append(
            Refusal(
                "processes",
                "the live processes report different root-secret fingerprints (keys_fp): "
                "finish the root-secret rollout first",
            )
        )
    return out


async def settle(
    read: Callable[[], Awaitable[list[Beat]]],
    judge_beats: Callable[[list[Beat]], Verdict],
    wait: Wait,
    *,
    extend_s: float = 0.0,
) -> Verdict:
    """Read and judge until nothing is waited on: Refused at the first refusal, and once
    `wait` runs out naming what was waited on. With `extend_s`, the deadline is extended
    once, to `extend_s` past the first sight of the last process first seen booting."""
    with wait.counting():
        return await _settle(read, judge_beats, wait, extend_s)


async def _settle(
    read: Callable[[], Awaitable[list[Beat]]],
    judge_beats: Callable[[list[Beat]], Verdict],
    wait: Wait,
    extend_s: float,
) -> Verdict:
    first_seen: dict[str, float] = {}
    extended = False
    while True:
        verdict = judge_beats(await read())
        if verdict.refusals:
            raise Refused(verdict.refusals)
        if not verdict.booting and not verdict.aging:
            if refusals := final_refusals(verdict):
                raise Refused(refusals)
            return verdict
        for i in verdict.booting:
            first_seen.setdefault(i, wait.used())
        if await wait.pause():
            continue
        if extend_s and not extended and verdict.booting:
            extended = True
            wait.seconds = max(wait.seconds, max(first_seen[i] for i in verdict.booting) + extend_s)
            if await wait.pause():
                continue
        raise Refused(
            [Refusal(f"process {i}", "still booting at the end of --wait") for i in verdict.booting]
            + [
                Refusal(f"process {i}", "stopped with LOOM_CELL unset, not aged out within --wait")
                for i in verdict.aging
            ]
        )


# -- the rescan's watch ----------------------------------------------------------------

_READ_TIMEOUT_S = 30.0  # not a setting: one heartbeat read's bound
_SKEW_S = 10.0  # not a setting: a margin for clock skew between ClickHouse hosts


def watch_period_s() -> float:
    """`T'`: how long a row whose window reaches past the rescan's start is kept before the
    leader's sweep may drop it, less a margin for clock skew."""
    return instances.PRUNE_AFTER_S - widened_window_s() - _SKEW_S


def prune_window_refusals() -> list[Refusal]:
    """A heartbeat interval so long that the leader may sweep a stopped process's rows
    before step 4's watch reads them refuses before any write."""
    if watch_period_s() < 2 * _READ_TIMEOUT_S:
        return [
            Refusal(
                "LOOM_INSTANCE_HEARTBEAT_SECONDS",
                "so long that a stopped process's rows can be swept before attach reads "
                "them: lower it",
            )
        ]
    return []


class Watch:
    """The processes seen during step 4's rescan, read every `T' / 2` from the rescan's
    start (`since`), so a process with `LOOM_CELL` unset that ran during it is refused at
    the end even once the leader swept its rows."""

    def __init__(
        self, ch: Any, since: str, name: str, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.ch, self.since, self.name = ch, since, name
        self._clock = clock
        self.cell_ids: set[str] = set()
        self.unset: dict[str, str] = {}
        self.no_cell: dict[str, str] = {}
        self.gap = False
        self._good = clock()

    async def read_once(self) -> list[Beat]:
        try:
            async with asyncio.timeout(_READ_TIMEOUT_S):
                beats = await read_beats(self.ch, since=self.since)
        except Exception:  # A failed read still checks whether the heartbeat watch has a gap.
            self._check_gap()
            raise
        self._check_gap()
        self._good = self._clock()
        for b in beats:
            if b.cell == self.name:
                self.cell_ids.add(b.instance_id)
            elif b.cell is not None:
                self.unset[b.instance_id] = b.last_beat
            elif b.stopped:
                self.no_cell[b.instance_id] = b.last_beat
        return beats

    def _check_gap(self) -> None:
        if self._clock() - self._good > watch_period_s():
            self.gap = True

    async def run(self) -> None:
        """Read until cancelled; a failed read is retried at the next period."""
        while True:
            await asyncio.sleep(watch_period_s() / 2)
            try:
                await self.read_once()
            except Exception:  # noqa: BLE001 — a failed read is retried; a gap refuses
                continue

    def refusals(self) -> list[Refusal]:
        out = [
            Refusal(
                f"process {i}",
                f"ran with LOOM_CELL unset or another cell during the rescan (last beat {at})",
            )
            for i, at in sorted(self.unset.items())
        ]
        out += [
            Refusal(
                f"process {i}",
                f"stopped during the rescan before reporting its LOOM_CELL (last beat {at})",
            )
            for i, at in sorted(self.no_cell.items())
            if i not in self.cell_ids
        ]
        if self.gap:
            out.append(
                Refusal(
                    "heartbeats",
                    "not read for longer than the leader keeps a stopped process's rows: "
                    "rerun attach",
                )
            )
        return out
