"""`loom cell attach` (server/cli/cell_attach.py, cell_attach_beats.py, cell_attach_pg.py) on
fakes: every refusal the heartbeats, the Postgres reads and the router's answers make, the
waits and their budget, the placement batches and the rescan's watch. The real Postgres,
ClickHouse and router behaviour is tests/integration/test_cell_attach.py. CH-free."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from server.cli import cell_attach, cell_control, cell_scan
from server.cli import cell_attach_beats as beats
from server.cli import cell_attach_pg as apg
from server.cli.cell_ch import Refusal, Refused
from server.cli.cell_scan import Table
from server.core import cell, instances
from server.router.limits import ATTACH_BATCH, MAX_BODY_BYTES
from server.settings import settings
from tests.unit.cell_control_owner import owner as _owner
from tests.unit.clock_fake import ManualClock

FIELDS = {**cell.DEFAULTS, "deployment_id": "dep-1"}
CURRENT = cell.as_value(FIELDS)
NEXT = cell.as_value({**FIELDS, "oidc_client_id": "next"})


def _value(**keys: Any) -> dict[str, Any]:
    return {**CURRENT, "serves": True, "marked": True, "router": True, "router_why": "", **keys}


def _beat(
    i: str = "p1",
    *,
    cell_name: str | None = "first",
    live: bool = True,
    stopped: bool = False,
    mode: str = "recording",
    value: dict[str, Any] | None = None,
    keys_fp: str = "k",
) -> beats.Beat:
    return beats.Beat(
        instance_id=i,
        live=live,
        stopped=stopped,
        keys_fp=keys_fp,
        last_beat="2026-01-01 00:00:00.000",
        cell=cell_name,
        mode=mode if cell_name is not None else "",
        value=(value if value is not None else _value()) if cell_name is not None else None,
    )


def _judge(found: list[beats.Beat], stopped_unset: beats.StoppedUnset = "refuse") -> Any:
    return beats.judge(found, "first", (CURRENT, NEXT), stopped_unset)


def _reasons(verdict: beats.Verdict) -> list[str]:
    return [str(r) for r in verdict.refusals]


# -- the heartbeats --------------------------------------------------------------------


def test_a_healthy_cell_passes_and_reports_its_keys() -> None:
    nxt = {**_value(), **NEXT}
    verdict = _judge([_beat("a"), _beat("b", value=nxt)])
    assert (verdict.refusals, verdict.booting, verdict.live, verdict.keys_fps) == ([], [], 2, {"k"})


def test_a_live_process_with_loom_cell_unset_or_another_cell_refuses_at_once() -> None:
    verdict = _judge([_beat("a", cell_name=""), _beat("b", cell_name="other")], "wait")
    assert _reasons(verdict) == [
        "process a: runs with LOOM_CELL unset: set LOOM_CELL=first on it",
        "process b: runs with LOOM_CELL=other: set LOOM_CELL=first on it",
    ]


def test_a_stopped_unset_process_refuses_or_is_waited_on_as_asked() -> None:
    stopped = [
        _beat("a", cell_name="", live=False, stopped=True),
        _beat("b", cell_name=None, live=False, stopped=True),
    ]
    refused = _judge(stopped, "refuse")
    assert [r.subject for r in refused.refusals] == ["process a", "process b"]
    assert "may still be writing" in refused.refusals[0].reason
    waited = _judge(stopped, "wait")
    assert (waited.refusals, waited.aging) == ([], ["a", "b"])


def test_a_live_instance_with_no_cell_row_is_booting() -> None:
    verdict = _judge([_beat("a", cell_name=None)])
    assert (verdict.refusals, verdict.booting) == ([], ["a"])


def test_a_cell_row_without_the_current_keys_refuses_even_while_booting() -> None:
    old = {k: v for k, v in _value().items() if k not in ("serves", "marked", "router")}
    verdict = _judge([_beat("a", value=old, mode="")])
    assert _reasons(verdict) == [
        "process a: reports no serves, marked or router key: upgrade it to this release"
    ]


@pytest.mark.parametrize(
    "keys,booting,refused",
    [
        ({"marked": False}, [], "lose the tenancy marker"),
        ({"marked": None}, ["a"], ""),
        ({"router": False, "router_why": "a 404"}, [], "cannot read its router: a 404"),
        ({"router": None}, ["a"], ""),
        # A workers-only pod places no org: it needs no router credential.
        ({"serves": False, "router": False}, [], ""),
        ({"serves": False, "router": None}, [], ""),
    ],
)
def test_marked_and_router_refuse_or_wait(
    keys: dict[str, Any], booting: list[str], refused: str
) -> None:
    verdict = _judge([_beat("a", value=_value(**keys))])
    assert verdict.booting == booting
    assert (refused in " ".join(_reasons(verdict))) and bool(verdict.refusals) == bool(refused)


def test_no_tenancy_mode_yet_is_booting() -> None:
    # A pod whose first cell-state read failed: mode '', marked and router null.
    verdict = _judge([_beat("a", mode="", value=_value(marked=None, router=None))])
    assert (verdict.refusals, verdict.booting) == ([], ["a"])


def test_a_fingerprint_on_neither_current_nor_next_refuses() -> None:
    drifted = {**_value(), **cell.as_value({**FIELDS, "oidc_client_id": "drifted"})}
    assert _reasons(_judge([_beat("a", value=drifted)])) == [
        "process a: runs neither loom.cell_self's current nor its next configuration"
    ]


def test_the_final_refusals() -> None:
    scim = {**_value(), **cell.as_value({**FIELDS, "scim": True})}
    verdict = beats.judge(
        [_beat("a", value=scim)], "first", (cell.as_value({**FIELDS, "scim": True}),), "refuse"
    )
    assert [r.reason for r in beats.final_refusals(verdict)] == [
        "SCIM is on: attach with SCIM on needs the per-cell SCIM release"
    ]
    two = _judge([_beat("a"), _beat("b", keys_fp="other")])
    assert "root-secret fingerprints" in beats.final_refusals(two)[0].reason
    assert "no live process" in beats.final_refusals(_judge([]))[0].reason


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    c = _Clock()

    async def _sleep(s: float) -> None:
        c.now += s

    monkeypatch.setattr(apg, "_sleep", _sleep)
    return c


@pytest.mark.asyncio
async def test_settle_waits_for_a_booting_process(clock: _Clock) -> None:
    reads = [[_beat("a", cell_name=None)], [_beat("a", cell_name=None)], [_beat("a")]]

    async def read() -> list[beats.Beat]:
        return reads.pop(0)

    wait = apg.Wait(10, clock)
    verdict = await beats.settle(read, _judge, wait)
    assert (verdict.live, wait.spent) == (1, 2.0)


@pytest.mark.asyncio
async def test_settle_refuses_at_the_end_of_wait_naming_what_it_waited_on(clock: _Clock) -> None:
    async def read() -> list[beats.Beat]:
        return [_beat("a", cell_name=None), _beat("b", cell_name="", live=False, stopped=True)]

    with pytest.raises(Refused) as e:
        await beats.settle(read, lambda f: _judge(f, "wait"), apg.Wait(3, clock))
    assert [str(r) for r in e.value.refusals] == [
        "process a: still booting at the end of --wait",
        "process b: stopped with LOOM_CELL unset, not aged out within --wait",
    ]


@pytest.mark.asyncio
async def test_settle_extends_once_for_a_process_first_seen_booting(clock: _Clock) -> None:
    seen = 0

    async def read() -> list[beats.Beat]:
        nonlocal seen
        seen += 1
        # A stopped process aging out first; then one booting from the third read (2 s
        # spent), done at the seventh, past the first deadline.
        if seen < 3:
            return [_beat("a"), _beat("x", cell_name="", live=False, stopped=True)]
        return [_beat("a"), _beat("b", cell_name=None)] if seen < 7 else [_beat("a"), _beat("b")]

    wait = apg.Wait(3, clock)
    verdict = await beats.settle(read, lambda f: _judge(f, "wait"), wait, extend_s=4)
    assert (verdict.live, wait.seconds) == (2, 6)

    async def forever() -> list[beats.Beat]:
        return [_beat("b", cell_name=None)]

    wait = apg.Wait(2, clock)
    with pytest.raises(Refused, match="still booting"):
        await beats.settle(forever, _judge, wait, extend_s=1)
    # Seen booting from the start: first sight plus the extension is inside the deadline.
    assert wait.seconds == 2


@pytest.mark.asyncio
async def test_wait_is_wall_time_lock_tries_included_and_no_scan(clock: _Clock) -> None:
    """Each mode-commit try waits its lock timeout before it times out: the tries and the
    pauses between them end within --wait, and time outside the wait is never charged."""
    asked: list[str] = []

    def lock(sql: str, *_a: Any) -> str:
        if "lock_timeout" in sql:
            asked.append(sql)
            clock.now += int(sql.split("'")[1].removesuffix("ms")) / 1000
            raise _Timeout()
        return "OK"

    conn = FakeConn(
        [
            ("SET LOCAL lock_timeout", lambda *a: lock(conn.executed[-1])),
        ]
    )

    async def nothing() -> None:
        pass

    async def ready() -> list[str]:
        return []

    wait = apg.Wait(4, clock)
    clock.now += 100  # a scan before the wait
    with pytest.raises(Refused, match="not taken within --wait"):
        await apg.commit_enforced(
            _owner(conn),
            nothing,
            ready,
            wait,
            lambda _t: None,
            lambda _m: None,
            expected_cell="first",
            expected_fp=(CURRENT, NEXT),
        )
    assert clock.now - 100 <= 4 and wait.spent <= 4
    # A 2 s try, a 1 s pause, then a try that waits only the second left.
    assert [a.split("'")[1] for a in asked] == ["2000ms", "1000ms"]


@pytest.mark.asyncio
async def test_the_trigger_pass_refuses_at_once_a_table_it_cannot_alter(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    from server.core import cell_triggers

    async def planned(_c: Any) -> list[str]:
        return ["scope_generations"]

    async def holders(_c: Any, _t: str) -> list[Any]:
        return []

    async def ensure_table(*_a: Any, **_kw: Any) -> Any:
        return cell_triggers.Outcome("denied")

    monkeypatch.setattr(cell_triggers, "planned_tables", planned)
    monkeypatch.setattr(cell_triggers, "holders", holders)
    monkeypatch.setattr(cell_triggers, "ensure_table", ensure_table)
    conn = FakeConn([("deadlock_timeout", 1000)])
    wait = apg.Wait(60, clock)
    (refusal,) = await apg.ensure_triggers(_owner(conn), wait, lambda _t: None)
    assert refusal.subject == "postgres loom.scope_generations"
    assert "cannot alter it" in refusal.reason and wait.spent < 1

    holders = iter(["pid 7", "pid 7", "pid 7", "pid 8"] + ["pid 8"] * 100)

    async def busy(*_a: Any, **_kw: Any) -> Any:
        clock.now += 0.1
        return cell_triggers.Outcome("busy", next(holders))

    monkeypatch.setattr(cell_triggers, "ensure_table", busy)
    wait = apg.Wait(10, clock)
    logged: list[str] = []
    (refusal,) = await apg.ensure_triggers(_owner(conn), wait, logged.append)
    # The last try ran with the floor's lock wait, after --wait was all but spent.
    assert "not taken within --wait" in refusal.reason
    assert wait.spent <= 10 + apg.TRY_FLOOR_MS / 1000 + 1e-9
    # Said once per change of holders, not once a try.
    assert logged == [
        "loom.scope_generations: its lock is busy (pid 7); retrying",
        "loom.scope_generations: its lock is busy (pid 8); retrying",
    ]


# -- the rescan's watch ----------------------------------------------------------------


def test_the_prune_contract_is_an_hour() -> None:
    assert instances.PRUNE_AFTER_S == 3600


@pytest.mark.asyncio
async def test_the_watch_refuses_every_unset_process_it_saw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rounds = [
        [
            _beat("a", cell_name=""),
            _beat("c"),
            _beat("d", cell_name=None, live=False, stopped=True),
        ],
        # a's rows were swept; c's cell row alone was swept: it was seen with the cell.
        [_beat("c", cell_name=None, live=False, stopped=True)],
    ]

    async def read_beats(_ch: Any, *, since: str | None = None) -> list[beats.Beat]:
        assert since == "2026-01-01 00:00:00.000"
        return rounds.pop(0)

    monkeypatch.setattr(beats, "read_beats", read_beats)
    clock = _Clock()
    watch = beats.Watch(None, "2026-01-01 00:00:00.000", "first", clock)
    await watch.read_once()
    await watch.read_once()
    assert [r.subject for r in watch.refusals()] == ["process a", "process d"]
    clock.now += beats.watch_period_s() + 1
    monkeypatch.setattr(beats, "read_beats", read_beats)
    rounds.append([])
    await watch.read_once()
    assert watch.refusals()[-1].subject == "heartbeats"


def test_the_watch_refuses_up_front_with_a_very_long_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "instance_heartbeat_seconds", 1180)
    assert beats.prune_window_refusals()
    monkeypatch.setattr(settings, "instance_heartbeat_seconds", 10)
    assert beats.prune_window_refusals() == []


# -- batches, ordering, arguments --------------------------------------------------------


def test_batches_keep_the_count_and_the_byte_bound() -> None:
    many = [f"org-{i:05d}" for i in range(ATTACH_BATCH + 1)]
    assert [len(b) for b in cell_attach.batches(many, "dep")] == [ATTACH_BATCH, 1]
    # Escaping grows a non-ASCII name six times: the encoded body is what counts.
    wide = ["é" * 2000 + str(i) for i in range(10)]
    out = cell_attach.batches(wide, "dep")
    assert len(out) > 1 and sum(map(len, out)) == 10
    for b in out:
        body = json.dumps({"deployment_id": "dep", "orgs": b}, separators=(",", ":"))
        assert len(body.encode()) <= MAX_BODY_BYTES


def test_move_sources_are_scanned_first() -> None:
    tables = [Table("loom", n, "MergeTree") for n in ("a", "memory_extraction_queue", "b", "src")]
    ordered = cell_scan.order_sources(tables, {"loom.src"})
    assert [t.name for t in ordered] == ["memory_extraction_queue", "src", "a", "b"]


def _args(**over: Any) -> argparse.Namespace:
    fields: dict[str, Any] = {
        "cell": "first",
        "wait": 10,
        "scan_budget": 100,
        "dry_run": False,
        "ch_allow_user": [],
        "ch_allow_user_directory": [],
        "ch_allow_object": [],
        "ch_allow_replica": [],
        "pg_allow_role": [],
        "router": "http://127.0.0.1:7900",
    }
    return argparse.Namespace(**{**fields, **over})


def test_the_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "storage_authority", "postgres")
    monkeypatch.setattr(settings, "pg_dsn", "postgresql://cell.internal/loom")
    assert cell_attach._args_problem(_args()) == ""
    assert "LOOM_CELL" in cell_attach._args_problem(_args(cell="other"))
    assert "--scan-budget" in cell_attach._args_problem(_args(scan_budget=0))
    monkeypatch.setattr(settings, "storage_authority", "clickhouse")
    assert "Postgres" in cell_attach._args_problem(_args())


def test_an_org_past_the_limit_is_named_with_its_sources() -> None:
    found = cell_scan.ScanResult()
    found.add("postgres loom.tenancy", "x" * (cell.ORG_MAX_BYTES + 1))
    (refusal,) = cell_attach._too_long(found)
    assert refusal.subject.startswith(f"org of {cell.ORG_MAX_BYTES + 1} bytes")
    assert "found in postgres loom.tenancy" in refusal.reason


# -- the router ------------------------------------------------------------------------


def _router(*responses: httpx.Response) -> cell_attach.Router:
    answers = list(responses)

    def handler(_request: httpx.Request) -> httpx.Response:
        return answers.pop(0)

    return cell_attach.Router(
        httpx.URL("http://127.0.0.1:7900"), "first", httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_the_router_calls_refuse_as_the_router_answers() -> None:
    ok = {"cell": "first", "state": "attaching"}
    assert await _router(httpx.Response(200, json=ok)).call("") == ok
    with pytest.raises(Refused, match="not upgraded"):
        await _router(httpx.Response(404, json={"error": "no route"})).call("/attach/place", {})
    with pytest.raises(Refused, match=r"refused /attach/begin \(409\): use loom cell update"):
        await _router(httpx.Response(409, json={"error": "use loom cell update"})).call(
            "/attach/begin", {}
        )
    with pytest.raises(Refused, match="did not answer as the router does"):
        await _router(httpx.Response(502, text="bad gateway")).call("")


# -- the Postgres reads, on a fake session ---------------------------------------------


@dataclass
class FakeConn:
    """Answers by the first needle the SQL holds; a callable answer is called with the
    arguments; an exception answer is raised."""

    routes: list[tuple[str, Any]] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)

    def _answer(self, sql: str, args: tuple[Any, ...]) -> Any:
        for needle, answer in self.routes:
            if needle in sql:
                if isinstance(answer, BaseException):
                    raise answer
                return answer(*args) if callable(answer) else answer
        if sql.startswith("SELECT (SELECT is_first FROM loom.cell_first)"):
            return {
                "is_first": True,
                "cell": "first",
                "mode": "recording",
                "has_own": True,
                "has_tenancy": True,
            }
        if sql == apg._HELD:
            return True
        if sql == "SELECT mode FROM loom.tenancy_mode":
            return "recording"
        if sql == "SELECT is_first, cell FROM loom.cell_first":
            return {"is_first": True, "cell": "first"}
        if sql == "SELECT current_fp, next_fp FROM loom.cell_self":
            return {"current_fp": CURRENT, "next_fp": NEXT}
        raise AssertionError(f"unrouted: {sql[:80]}")

    async def fetch(self, sql: str, *args: Any) -> Any:
        return self._answer(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._answer(sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._answer(sql, args)

    async def execute(self, sql: str, *args: Any) -> str:
        assert "pg_terminate_backend" not in sql and "pg_cancel_backend" not in sql
        self.executed.append(sql)
        for needle, answer in self.routes:
            if needle in sql:
                if isinstance(answer, BaseException):
                    raise answer
                return answer(*args) if callable(answer) else str(answer)
        return "OK"

    def transaction(self) -> Any:
        conn = self

        class _Tx:
            async def start(self) -> None:
                await conn.execute("BEGIN")

            async def commit(self) -> None:
                await conn.execute("COMMIT")

            async def rollback(self) -> None:
                await conn.execute("ROLLBACK")

            async def __aenter__(self) -> None:
                await self.start()

            async def __aexit__(self, *exc: Any) -> None:
                if exc[0]:
                    await self.rollback()
                else:
                    await self.commit()

        return _Tx()

    def terminate(self) -> None:
        self.executed.append("TERMINATE")

    async def close(self, **kwargs: Any) -> None:
        assert kwargs["timeout"] > 0
        self.executed.append("CLOSE")

    def is_closed(self) -> bool:
        return bool(self.executed and self.executed[-1] in {"CLOSE", "TERMINATE"})


def _short_phases(monkeypatch: pytest.MonkeyPatch, bounds: list[float] | None = None) -> None:
    # The work timer stays real; scheduling cannot consume the fake close budget.
    phase_clock = ManualClock()
    monkeypatch.setattr(cell_control, "time", phase_clock)
    monkeypatch.setattr(cell_control.pg, "time", phase_clock)
    supervise = cell_control.Owner.call

    async def bounded(self: Any, work: Any, *, total_s: float) -> Any:
        if bounds is not None:
            bounds.append(total_s)
        return await supervise(self, work, total_s=0.01)

    monkeypatch.setattr(cell_control.Owner, "call", bounded)


def _seq(*values: Any) -> Callable[..., Any]:
    pending = list(values)
    return lambda *_a: pending.pop(0)


@pytest.mark.asyncio
async def test_the_session_checks_refuse_a_pooler() -> None:
    moved = FakeConn(
        [
            ("pg_backend_pid() AS pid", _seq((1, "t0"), (2, "t1"))),
            ("current_setting($1, true)", None),
        ]
    )
    refusals = await apg.direct_refusals(moved, "attach")
    assert [r.reason.split(":")[0] for r in refusals] == [
        "attach's session moved between backends",
        "attach's session loses loom.tenancy_checked",
    ]
    direct = FakeConn([("pg_backend_pid() AS pid", (1, "t0")), ("current_setting($1, true)", "on")])
    assert await apg.direct_refusals(direct, "attach") == []
    assert "RESET ALL" in direct.executed


@pytest.mark.asyncio
async def test_the_lock_refuses_when_held_or_not_on_this_backend() -> None:
    holder = [{"pid": 7, "application_name": "loom", "client": "10.0.0.1", "backend_start": "t"}]
    held = FakeConn([("pg_try_advisory_lock", False), ("FROM pg_locks l LEFT JOIN", holder)])
    with pytest.raises(Refused, match="held by pid 7 \\(loom, from 10.0.0.1"):
        await apg.take_lock(held, "attach")
    pooled = FakeConn([("pg_try_advisory_lock", True), ("SELECT EXISTS", False)])
    with pytest.raises(Refused, match="a pooler"):
        await apg.take_lock(pooled, "attach")
    assert any("tcp_keepalives_idle" in s for s in pooled.executed)


@pytest.mark.asyncio
async def test_a_database_not_utf8_refuses() -> None:
    assert await apg.encoding_refusals(FakeConn([("pg_encoding_to_char", "UTF8")])) == []
    (r,) = await apg.encoding_refusals(FakeConn([("pg_encoding_to_char", "SQL_ASCII")]))
    assert "SQL_ASCII, not UTF8" in r.reason


def test_privilege_holders_outside_the_allowed_refuse() -> None:
    holders = apg.Holders(
        own=["loom"],
        others={"ci": ["may insert into loom.tenancy"], "servers": ["owns loom.memory_current"]},
    )
    assert [r.subject for r in apg.privilege_refusals(holders, ["servers"])] == ["postgres role ci"]
    assert apg.privilege_refusals(holders, ["ci", "servers"]) == []


@pytest.mark.asyncio
async def test_settings_and_subscriptions_refuse() -> None:
    conn = FakeConn(
        [
            (
                "pg_db_role_setting",
                [{"role": "ci", "db": "every database", "cfg": "Loom.Tenancy_Checked=on"}],
            ),
            ("pg_subscription", [{"subname": "mirror", "subenabled": False}]),
        ]
    )
    refusals = await apg.unmarked_refusals(conn, True)
    assert [r.subject for r in refusals] == [
        "postgres unmarked session",
        "postgres setting for ci in every database",
        "postgres subscription mirror",
    ]


@pytest.mark.asyncio
async def test_own_role_lacks_and_prepared_transactions_refuse() -> None:
    conn = FakeConn(
        [
            ("SELECT current_user", "attacher"),
            ("WHERE NOT held", [{"need": "effective privileges of pg_read_all_stats"}]),
            ("has_table_privilege($1, 'SELECT')", lambda t: t != "loom.memory_current"),
            ("max_prepared_transactions", 5),
        ]
    )
    tables = [
        {"relname": "memory_current", "relkind": "r", "owned": True},
        # Its grants are held, but not its effective owner-role privileges: the pass could
        # not alter it, so it refuses before any write.
        {"relname": "scope_generations", "relkind": "r", "owned": False},
        {"relname": "tenancy", "relkind": "r", "owned": True},
    ]
    refusals = await apg.own_role_refusals(conn, tables)
    assert [str(r) for r in refusals] == [
        "postgres role attacher: attach's role lacks effective privileges of pg_read_all_stats",
        "postgres role attacher: attach's role lacks SELECT on loom.memory_current",
        "postgres role attacher: attach's role lacks effective privileges of the owner role of "
        "loom.scope_generations",
        "postgres max_prepared_transactions: is 5: a prepared transaction outlives every "
        "session, so step 4 could not wait for it; set it to 0",
    ]


def _row(**over: Any) -> dict[str, Any]:
    return {
        "pid": 9,
        "backend_type": "client backend",
        "application_name": "ci",
        "xact_start": "t",
        "ours": True,
        "xid_old": False,
        "xmin_old": False,
        "hidden": False,
        **over,
    }


@pytest.mark.asyncio
async def test_step_4_refuses_what_it_cannot_wait_for(clock: _Clock) -> None:
    base = [("max_prepared_transactions", 0), ("pg_prepared_xacts", 0)]
    hidden = FakeConn([*base, ("pg_stat_activity s", [_row(hidden=True)])])
    with pytest.raises(Refused, match="hidden from attach's role"):
        await apg.wait_transactions(hidden, 100, apg.Wait(5, clock))
    prepared = FakeConn([("max_prepared_transactions", 0), ("pg_prepared_xacts", 1)])
    with pytest.raises(Refused, match="prepared transaction"):
        await apg.wait_transactions(prepared, 100, apg.Wait(5, clock))
    held = FakeConn(
        [
            *base,
            (
                "pg_stat_activity s",
                [_row(xmin_old=True), _row(pid=10, ours=False, xid_old=True), _row(pid=11)],
            ),
        ]
    )
    with pytest.raises(Refused) as e:
        await apg.wait_transactions(held, 100, apg.Wait(2, clock))
    assert [r.reason for r in e.value.refusals] == [
        "may have read `recording`: still open",
        "another database's transaction, holding the snapshot horizon",
    ]
    done = FakeConn([*base, ("pg_stat_activity s", [_row(pid=10, ours=False, xid_old=True)])])
    await apg.wait_transactions(done, 100, apg.Wait(0, clock))


class _Timeout(Exception):
    sqlstate = "55P03"


@pytest.mark.asyncio
async def test_the_mode_commit_retries_a_busy_lock_without_ending_other_backends(
    clock: _Clock,
) -> None:
    tries = _seq(_Timeout(), "UPDATE 1")

    def lock(*_a: Any) -> str:
        answer = tries()
        if isinstance(answer, Exception):
            raise answer
        return "LOCK TABLE"

    conn = FakeConn(
        [
            ("LOCK TABLE", lock),
            ("UPDATE loom.tenancy_mode", "UPDATE 1"),
        ]
    )
    confirmed: list[bool] = []

    async def confirm() -> None:
        confirmed.append(True)

    async def ready() -> list[str]:
        return []

    logged: list[str] = []
    await apg.commit_enforced(
        _owner(conn),
        confirm,
        ready,
        apg.Wait(10, clock),
        logged.append,
        lambda _m: None,
        expected_cell="first",
        expected_fp=(CURRENT, NEXT),
    )
    assert logged == ["the mode's lock is busy; retrying"]
    # Before each try, with no termination of a different backend.
    assert len(confirmed) == 2
    assert conn.executed.count("ROLLBACK") == 1
    assert "TERMINATE" not in conn.executed
    assert not any(
        "pg_terminate_backend" in sql or "pg_cancel_backend" in sql for sql in conn.executed
    )
    assert conn.executed.count("COMMIT") == 1
    assert sum(sql.startswith("UPDATE loom.tenancy_mode") for sql in conn.executed) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_s", [0, 2])
async def test_the_mode_commit_refuses_past_wait_and_skips_a_try_while_booting(
    clock: _Clock,
    monkeypatch: pytest.MonkeyPatch,
    wait_s: int,
) -> None:
    conn = FakeConn()
    run = cell_attach.Run(None, _args(), None, lambda _t: None)  # type: ignore[arg-type]
    run.owner = _owner(FakeConn())
    run.own_fp = (CURRENT, NEXT)

    async def read(_ch: Any) -> list[beats.Beat]:
        return [_beat("a", cell_name=None), _beat("b", cell_name=None)]

    monkeypatch.setattr(beats, "read_beats", read)

    async def confirm() -> None:
        pass

    with pytest.raises(Refused) as error:
        await apg.commit_enforced(
            _owner(conn),
            confirm,
            run.ready_for_commit,
            apg.Wait(wait_s, clock),
            lambda _t: None,
            lambda _m: None,
            expected_cell="first",
            expected_fp=(CURRENT, NEXT),
        )
    assert [str(r) for r in error.value.refusals] == [
        "process a: still booting at the end of --wait",
        "process b: still booting at the end of --wait",
    ]
    assert not any("LOCK TABLE" in s for s in conn.executed)


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_s,bound_s", [(0, 8.1), (10, 10.0)])
async def test_the_mode_try_bounds_a_commit_that_never_answers(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, wait_s: int, bound_s: float
) -> None:
    class _StalledCommit(FakeConn):
        async def execute(self, sql: str, *args: Any) -> str:
            result = await super().execute(sql, *args)
            if sql == "COMMIT":
                await asyncio.Future()
            return result

    conn = _StalledCommit([("UPDATE loom.tenancy_mode", "UPDATE 1")])
    bounds: list[float] = []
    _short_phases(monkeypatch, bounds)
    marks: list[str] = []

    async def confirm() -> None:
        pass

    async def ready() -> list[str]:
        return []

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            apg.commit_enforced(
                _owner(conn),
                confirm,
                ready,
                apg.Wait(wait_s, clock),
                lambda _t: None,
                marks.append,
                expected_cell="first",
                expected_fp=(CURRENT, NEXT),
            ),
            timeout=0.2,
        )
    assert "COMMIT" in conn.executed
    assert conn.executed[-1] == "CLOSE"
    assert "TERMINATE" not in conn.executed
    assert bounds == pytest.approx([bound_s], abs=0.001)
    assert marks == ["unknown"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", ["LOCK TABLE", "UPDATE", "ROLLBACK"])
async def test_a_mode_deadline_never_waits_for_rollback(
    monkeypatch: pytest.MonkeyPatch, stall: str
) -> None:
    class _StalledRollback(FakeConn):
        async def execute(self, sql: str, *args: Any) -> str:
            result = await super().execute(sql, *args)
            if stall == "ROLLBACK" and sql.startswith("LOCK TABLE"):
                raise _Timeout()
            if sql.startswith(stall) or sql == "ROLLBACK":
                await asyncio.Future()
            return result

    conn = _StalledRollback()
    _short_phases(monkeypatch)
    marks: list[str] = []
    task = asyncio.create_task(
        apg._mode_try(
            _owner(conn), 100, marks.append, expected_cell="first", expected_fp=(CURRENT, NEXT)
        )
    )
    done, _ = await asyncio.wait([task], timeout=0.2)
    if not done:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task in done, "mode deadline is stuck awaiting rollback"
    assert isinstance(task.exception(), TimeoutError)
    assert conn.executed[-1] == "CLOSE"
    assert "TERMINATE" not in conn.executed
    assert marks == (["unknown"] if stall == "UPDATE" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_read", [1, 2])
@pytest.mark.parametrize("failure", ["cancel", "error"])
async def test_a_failed_direct_session_read_never_waits_for_rollback(
    failed_read: int, failure: str
) -> None:
    class _StalledRollback(FakeConn):
        reads = 0

        async def fetchrow(self, sql: str, *args: Any) -> Any:
            self.reads += 1
            if self.reads == failed_read:
                if failure == "error":
                    raise ValueError("identity read failed")
                await asyncio.Future()
            return (1, "t0")

        async def execute(self, sql: str, *args: Any) -> str:
            result = await super().execute(sql, *args)
            if sql == "ROLLBACK":
                await asyncio.Future()
            return result

    conn = _StalledRollback()
    task = asyncio.create_task(apg.direct_refusals(conn, "attach"))
    await asyncio.sleep(0)
    if failure == "cancel":
        task.cancel()
    done, _ = await asyncio.wait([task], timeout=0.2)
    if not done:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert task in done, "direct-session failure is stuck awaiting rollback"
    if failure == "cancel":
        assert task.cancelled()
    else:
        assert isinstance(task.exception(), ValueError)
    assert "CLOSE" not in conn.executed
    assert "TERMINATE" not in conn.executed
    assert "ROLLBACK" not in conn.executed


@pytest.mark.asyncio
async def test_an_interrupt_waiting_for_the_mode_lock_leaves_recording(clock: _Clock) -> None:
    conn = FakeConn([("LOCK TABLE", KeyboardInterrupt())])
    marks: list[str] = []

    async def confirm() -> None:
        pass

    async def ready() -> list[str]:
        return []

    with pytest.raises(cell_control._ForwardFatal) as fatal:
        await apg.commit_enforced(
            _owner(conn),
            confirm,
            ready,
            apg.Wait(2, clock),
            lambda _t: None,
            marks.append,
            expected_cell="first",
            expected_fp=(CURRENT, NEXT),
        )
    assert isinstance(fatal.value.fatal.error, KeyboardInterrupt)
    assert conn.executed[-1] == "CLOSE"
    assert "TERMINATE" not in conn.executed
    assert marks == []


@pytest.mark.asyncio
async def test_the_durable_read_must_see_enforced_and_x_above_its_xid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg_profile

    async def synchronous() -> str:
        return "synchronous"

    monkeypatch.setattr(pg_profile, "standby_now", synchronous)
    run = cell_attach.Run(None, _args(), None, lambda _t: None)  # type: ignore[arg-type]
    run.owner = _owner(FakeConn())
    run.lock_conn = FakeConn([("pg_snapshot_xmax", "100")])

    async def read(mode: str, xid: int) -> apg.Durable:
        return apg.Durable(mode=mode, first=(True, "first"), xid=xid)

    monkeypatch.setattr(apg, "durable_read", lambda _conn: read("recording", 50))
    with pytest.raises(Refused, match="did not land"):
        await run.durable()
    monkeypatch.setattr(apg, "durable_read", lambda _conn: read("enforced", 100))
    with pytest.raises(Refused, match="is not above 100"):
        await run.durable()
    monkeypatch.setattr(apg, "durable_read", lambda _conn: read("enforced", 99))
    assert await run.durable() == 100

    async def waiting() -> str:
        return "waiting"

    monkeypatch.setattr(pg_profile, "standby_now", waiting)
    with pytest.raises(Refused, match="not synchronous before"):
        await run.durable()


@pytest.mark.asyncio
async def test_a_serving_row_is_done_only_once_the_cell_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from server.core import pg

    state = ["enforced"]
    conn = FakeConn([("SELECT deployment_id FROM loom.authority", "dep-1")])
    monkeypatch.setattr(settings, "cell", "first")
    monkeypatch.setattr(settings, "document_s3_bucket", "")
    monkeypatch.setattr(settings, "document_s3_prefix", "")

    @asynccontextmanager
    async def dedicated(**_kw: Any) -> Any:
        yield conn

    async def read_mode(_conn: Any) -> tuple[str, Any]:
        return state[0], (True, "first")

    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(apg, "read_mode", read_mode)
    run = cell_attach.Run(None, _args(), None, lambda _t: None)  # type: ignore[arg-type]
    run.owner = _owner(FakeConn())
    run.row = {"deployment_id": "dep-1", "cell": "first", "bucket": "", "bucket_prefix": ""}
    assert await run.done()
    state[0] = "recording"
    with pytest.raises(Refused, match="attach lowers and repairs neither"):
        await run.done()


def test_refusal_text() -> None:
    assert str(Refusal("a", "b")) == "a: b"


@pytest.mark.asyncio
async def test_a_root_secret_change_during_attach_refuses_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = cell_attach.Run(None, _args(), None, lambda _t: None)  # type: ignore[arg-type]
    run.owner = _owner(FakeConn())
    run.begun_keys_fp = "before"

    async def settle(*_a: Any, **_kw: Any) -> beats.Verdict:
        return beats.Verdict(live=1, keys_fps={"after"})

    monkeypatch.setattr(beats, "settle", settle)
    with pytest.raises(Refused, match="a root-secret rotation"):
        await run.final_checks(beats.Watch(None, "", "first"))
    run.begun_keys_fp = "after"
    await run.final_checks(beats.Watch(None, "", "first"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [Refused([Refusal("postgres", "cut")]), OSError("gone")])
async def test_a_stop_between_a_landed_commit_and_the_durable_read_says_the_outcome_is_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: Exception
) -> None:
    from contextlib import asynccontextmanager

    from server.core import pg

    @asynccontextmanager
    async def dedicated(**_kw: Any) -> Any:
        yield None

    async def landed(*a: Any, **_kw: Any) -> None:
        a[-1]("unknown")  # the try sent its UPDATE; the commit landed, its answer may not have

    async def call(_self: Any, _path: str, _body: Any = None) -> dict[str, Any]:
        return {"cell": "first"}

    async def run(self: Any) -> int:
        self.owner = _owner(FakeConn())
        self.row = {"deployment_id": "dep-1"}
        await self.commit_mode()
        raise failure  # what stops the run before the durable read finishes

    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(apg, "commit_enforced", landed)
    monkeypatch.setattr(cell_attach.Router, "call", call)
    monkeypatch.setattr(cell_attach.Run, "mutate", call)
    monkeypatch.setattr(cell_attach.Run, "run", run)
    url = httpx.URL("http://127.0.0.1:7900")
    assert await cell_attach.attach(None, _args(), url) == 1
    err = capsys.readouterr().err
    assert "the mode commit's outcome is unknown: rerun attach" in err
    assert "stays recording" not in err


@pytest.mark.asyncio
async def test_doctor_names_both_catalog_checks_when_the_catalog_is_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.cli import doctor_cell

    async def before(_ch: Any) -> str:
        return ""

    async def marked() -> tuple[str, bool, list[Any]]:
        return "enforced", True, []

    async def unmarked() -> bool:
        return False

    async def unread(_roles: Any) -> Any:
        raise OSError("the catalog read failed")

    monkeypatch.setattr(doctor_cell, "_deployment_check", before)
    monkeypatch.setattr(doctor_cell, "_marked_session", marked)
    monkeypatch.setattr(doctor_cell, "_unmarked_session", unmarked)
    monkeypatch.setattr(doctor_cell, "_writer_reads", unread)
    lines = {n: (g, d) for n, g, d in await doctor_cell._checks(None)}
    for name in ("cell_tenancy_privilege", "cell_unchecked_writers"):
        assert lines[name] == ("WARN", "not read: OSError: the catalog read failed")


@pytest.mark.asyncio
async def test_the_ddl_lock_bound_is_taken_after_the_migration_lock(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    """A migration lock that took the rest of --wait leaves the DDL its floor: one try, never
    the full 100 ms when less is left, never none."""
    from server.core import cell_triggers

    def migration_lock(*_a: Any) -> str:
        clock.now += 1.95  # all but 50 ms of --wait
        return "OK"

    asked: list[str] = []

    async def ensure_table(*_a: Any, lock_timeout: str, **_kw: Any) -> Any:
        asked.append(lock_timeout)
        return cell_triggers.Outcome("applied")

    monkeypatch.setattr(cell_triggers, "ensure_table", ensure_table)
    conn = FakeConn([("pg_advisory_lock", migration_lock), ("pg_advisory_unlock", "OK")])
    wait = apg.Wait(2, clock)
    with wait.counting():
        outcome = await apg._ensure_one(_owner(conn), "scope_generations", 500, wait)
    assert outcome.answer == "applied"
    assert asked == [f"{apg.TRY_FLOOR_MS}ms"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", ["migration", "reset", "ddl", "commit", "rollback", "unlock"])
async def test_a_trigger_try_deadline_covers_transaction_and_lock_cleanup(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, stall: str
) -> None:
    import asyncpg

    from server.core import cell_triggers

    class _Stalled(FakeConn):
        async def execute(self, sql: str, *args: Any) -> str:
            result = await super().execute(sql, *args)
            if stall == "rollback" and sql == "CREATE TRIGGER stub":
                raise asyncpg.LockNotAvailableError("busy")
            needles = {
                "migration": "SELECT pg_advisory_lock",
                "reset": "RESET lock_timeout",
                "ddl": "CREATE TRIGGER stub",
                "commit": "COMMIT",
                "rollback": "ROLLBACK",
                "unlock": "SELECT pg_advisory_unlock",
            }
            if sql.startswith(needles[stall]) or (stall == "ddl" and sql == "ROLLBACK"):
                await asyncio.Future()
            return result

    async def ensure_table(conn: Any, table: str, **kwargs: Any) -> Any:
        return await cell_triggers._run_ddl(
            conn, table, ["CREATE TRIGGER stub"], kwargs["lock_timeout"], 1, warn=False
        )

    _short_phases(monkeypatch)
    monkeypatch.setattr(cell_triggers, "ensure_table", ensure_table)
    conn = _Stalled()
    task = asyncio.create_task(
        apg._ensure_one(_owner(conn), "scope_generations", 100, apg.Wait(0, clock))
    )
    done, _ = await asyncio.wait([task], timeout=0.2)
    try:
        assert task in done, f"trigger deadline is stuck at {stall}"
        assert isinstance(task.exception(), TimeoutError)
        assert conn.executed[-1] == "CLOSE"
        assert "TERMINATE" not in conn.executed
        if stall in {"migration", "ddl", "commit", "rollback"}:
            assert apg._MIGRATION_UNLOCK not in conn.executed
    finally:
        for _ in range(3):
            if task.done():
                break
            task.cancel()
            await asyncio.wait([task], timeout=0.01)
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", ["migration", "ddl"])
async def test_a_failed_trigger_try_does_not_mask_its_error_with_cleanup(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock, failed: str
) -> None:
    from server.core import cell_triggers

    async def ensure_table(*_a: Any, **_kw: Any) -> Any:
        raise ValueError("DDL failed")

    monkeypatch.setattr(cell_triggers, "ensure_table", ensure_table)
    conn = FakeConn(
        [
            ("pg_advisory_lock", ValueError("migration failed") if failed == "migration" else "OK"),
            ("RESET", RuntimeError("reset failed") if failed == "migration" else "OK"),
            ("pg_advisory_unlock", RuntimeError("unlock failed")),
        ]
    )
    with pytest.raises(ValueError, match=f"{failed if failed == 'migration' else 'DDL'} failed"):
        await apg._ensure_one(_owner(conn), "scope_generations", 100, apg.Wait(0, clock))
    assert conn.executed[-1] == "CLOSE"
    assert "TERMINATE" not in conn.executed


@pytest.mark.asyncio
async def test_a_busy_migration_lock_is_named_as_such(
    monkeypatch: pytest.MonkeyPatch, clock: _Clock
) -> None:
    from server.core import cell_triggers

    class _Busy(Exception):
        sqlstate = "55P03"

    holder = [{"pid": 42, "application_name": "loom", "backend_start": "t0"}]
    conn = FakeConn(
        [
            ("pg_advisory_lock", _Busy()),
            ("l.locktype = 'advisory'", holder),
            ("deadlock_timeout", 1000),
        ]
    )

    async def planned(_c: Any) -> list[str]:
        return ["scope_generations"]

    async def no_holders(_c: Any, _t: str) -> list[Any]:
        return []

    monkeypatch.setattr(cell_triggers, "planned_tables", planned)
    monkeypatch.setattr(cell_triggers, "holders", no_holders)
    logged: list[str] = []
    (refusal,) = await apg.ensure_triggers(_owner(conn), apg.Wait(2, clock), logged.append)
    named = "the migration lock (held by pid 42, loom, since t0)"
    assert logged == [f"loom.scope_generations: its lock is busy ({named}); retrying"]
    assert refusal.reason == f"its triggers were not created within --wait: {named}"


@pytest.mark.asyncio
async def test_wait_0_runs_one_try_of_each_wait(
    clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On an idle database --wait 0 commits the mode and creates the triggers in one try
    each, and the heartbeats pass when nothing boots."""
    from server.core import cell_triggers

    conn = FakeConn(
        [
            ("LOCK TABLE", "LOCK TABLE"),
            ("UPDATE loom.tenancy_mode", "UPDATE 1"),
        ]
    )
    marks: list[str] = []

    async def ok() -> list[str]:
        return []

    async def nothing() -> None:
        pass

    await apg.commit_enforced(
        _owner(conn),
        nothing,
        ok,
        apg.Wait(0, clock),
        lambda _t: None,
        marks.append,
        expected_cell="first",
        expected_fp=(CURRENT, NEXT),
    )
    assert marks == ["unknown", "enforced"]
    assert any(f"lock_timeout = '{apg.TRY_FLOOR_MS}ms'" in sql for sql in conn.executed)

    async def planned(_c: Any) -> list[str]:
        return ["scope_generations"]

    async def no_holders(_c: Any, _t: str) -> list[Any]:
        return []

    async def applied(*_a: Any, **_kw: Any) -> Any:
        return cell_triggers.Outcome("applied")

    async def no_defects(_c: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cell_triggers, "planned_tables", planned)
    monkeypatch.setattr(cell_triggers, "holders", no_holders)
    monkeypatch.setattr(cell_triggers, "ensure_table", applied)
    monkeypatch.setattr(cell_triggers, "missing", no_defects)
    ddl = FakeConn([("deadlock_timeout", 1000)])
    assert await apg.ensure_triggers(_owner(ddl), apg.Wait(0, clock), lambda _t: None) == []

    async def read() -> list[beats.Beat]:
        return [_beat("a")]

    assert (await beats.settle(read, _judge, apg.Wait(0, clock))).live == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("refused_before_any_try", [True, False])
async def test_a_mode_commit_refused_without_a_landed_try_says_recording(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    clock: _Clock,
    refused_before_any_try: bool,
) -> None:
    """The closing line says recording when no try sent its UPDATE (the heartbeats refused
    before one) and when every try timed out and rolled back."""

    def lock(*_a: Any) -> str:
        raise _Timeout()

    conn = FakeConn([("LOCK TABLE", lock)])

    async def call(_self: Any, _path: str, _body: Any = None) -> dict[str, Any]:
        return {"cell": "first"}

    async def ready(_self: Any) -> list[str]:
        if refused_before_any_try:
            raise Refused([Refusal("process p1", "runs with LOOM_CELL unset")])
        return []

    async def confirm(_self: Any) -> None:
        pass

    async def run(self: Any) -> int:
        self.owner = _owner(FakeConn())
        self.row, self.mode = {"deployment_id": "dep-1"}, "recording"
        self.lock_conn = conn
        self.owner = _owner(conn)
        self.wait = apg.Wait(2, clock)
        await self.commit_mode()
        return 0

    from contextlib import asynccontextmanager

    from server.core import pg

    @asynccontextmanager
    async def dedicated(**_kw: Any) -> Any:
        yield conn

    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(cell_attach.Router, "call", call)
    monkeypatch.setattr(cell_attach.Run, "mutate", call)
    monkeypatch.setattr(cell_attach.Run, "ready_for_commit", ready)
    monkeypatch.setattr(cell_attach.Run, "confirm_lock", confirm)
    monkeypatch.setattr(cell_attach.Run, "run", run)
    assert await cell_attach.attach(None, _args(), httpx.URL("http://127.0.0.1:7900")) == 1
    assert capsys.readouterr().err.splitlines()[-1] == "loom cell attach: the cell stays recording"


def _stub_attach(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    from contextlib import asynccontextmanager

    from server.core import pg

    @asynccontextmanager
    async def dedicated(**_kw: Any) -> Any:
        yield None

    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(cell_attach.Run, "run", body)


@pytest.mark.asyncio
async def test_a_router_refusal_of_attach_enforced_leaves_recording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def refuse(_self: Any, path: str, _body: Any = None) -> dict[str, Any]:
        raise Refused([Refusal("router", f"refused {path}")])

    async def run(self: Any) -> int:
        self.owner = _owner(FakeConn())
        self.row, self.mode = {"deployment_id": "dep-1"}, "recording"
        await self.commit_mode()
        return 0

    monkeypatch.setattr(cell_attach.Router, "call", refuse)
    monkeypatch.setattr(cell_attach.Run, "mutate", refuse)
    _stub_attach(monkeypatch, run)
    assert await cell_attach.attach(None, _args(), httpx.URL("http://127.0.0.1:7900")) == 1
    assert capsys.readouterr().err.splitlines()[-1] == "loom cell attach: the cell stays recording"


@pytest.mark.asyncio
async def test_a_durable_read_that_sees_recording_says_recording(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.core import pg_profile

    async def synchronous() -> str:
        return "synchronous"

    async def read(_conn: Any) -> apg.Durable:
        return apg.Durable(mode="recording", first=(True, "first"), xid=1)

    async def run(self: Any) -> int:
        self.owner = _owner(FakeConn())
        self.mode = "unknown"  # a mode commit whose outcome was lost
        await self.durable()
        return 0

    monkeypatch.setattr(pg_profile, "standby_now", synchronous)
    monkeypatch.setattr(apg, "durable_read", read)
    _stub_attach(monkeypatch, run)
    assert await cell_attach.attach(None, _args(), httpx.URL("http://127.0.0.1:7900")) == 1
    assert capsys.readouterr().err.splitlines()[-1] == "loom cell attach: the cell stays recording"


@pytest.mark.asyncio
async def test_an_enforced_resume_refused_early_says_enforced(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from server.core import pg_profile

    async def recorded(_ch: Any) -> dict[str, Any]:
        return {"deployment_id": "other"}

    async def health(_self: Any, _host: str) -> str:
        return "dep-1"

    async def read_mode(_conn: Any) -> tuple[str, Any]:
        return "enforced", (True, "first")

    async def run(self: Any) -> int:
        self.owner = _owner(FakeConn())
        self.row = {"is_first": True, "state": "attaching", "deployment_id": "dep-1"}
        self.lock_conn = FakeConn([("FROM loom.authority", "dep-1")])
        if refusals := await self.stores_refusals():
            raise Refused(refusals)
        return 0

    monkeypatch.setattr(pg_profile, "recorded_authority", recorded)
    monkeypatch.setattr(cell_attach.Router, "health", health)
    monkeypatch.setattr(apg, "read_mode", read_mode)
    _stub_attach(monkeypatch, run)
    assert await cell_attach.attach(None, _args(), httpx.URL("http://127.0.0.1:7900")) == 1
    err = capsys.readouterr().err.splitlines()
    assert "clickhouse: records another deployment id" in err[0]
    assert err[-1] == (
        "loom cell attach: the cell stays enforced and attaching: rerun attach to resume"
    )


@pytest.mark.asyncio
async def test_an_interrupt_prints_the_mode_line_and_goes_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run(self: Any) -> int:
        self.owner = _owner(FakeConn())
        self.mode = "unknown"
        raise KeyboardInterrupt

    _stub_attach(monkeypatch, run)
    with pytest.raises(KeyboardInterrupt):
        await cell_attach.attach(None, _args(), httpx.URL("http://127.0.0.1:7900"))
    assert "the mode commit's outcome is unknown" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_the_stray_read_names_a_refused_table_and_reads_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from server.cli import cell_ch, doctor_cell
    from server.core import pg

    async def topology(_ch: Any) -> Any:
        return cell_ch.Topology("", 0)

    async def no_tables(*_a: Any) -> tuple[list[Any], list[Any]]:
        return [], []

    async def sizes(*_a: Any) -> dict[str, int]:
        return {}

    pg_rows = [{"name": "loom.a_unindexed", "n": 1}, {"name": "loom.b_data", "n": 2}]
    conn = FakeConn([("FROM loom.tenancy", []), ("pg_class c", pg_rows)])

    @asynccontextmanager
    async def dedicated(**_kw: Any) -> Any:
        yield conn

    async def pg_orgs(table: str, _budget: Any) -> list[str]:
        if table == "loom.a_unindexed":
            raise Refused([Refusal(f"postgres {table}", "has no plain btree index led by org")])
        return ["stray-org"]

    monkeypatch.setattr(cell_ch, "read_topology", topology)
    monkeypatch.setattr(cell_scan, "list_clickhouse", no_tables)
    monkeypatch.setattr(doctor_cell, "_table_sizes", sizes)
    monkeypatch.setattr(pg, "dedicated", dedicated)
    monkeypatch.setattr(cell_scan, "pg_orgs", pg_orgs)
    lines = {n: (g, d) for n, g, d in await doctor_cell._stray_reads(None, 60)}
    assert lines["cell_stray_orgs"] == ("WARN", "postgres loom.b_data: stray-org")
    assert lines["cell_stray_orgs_unread"] == (
        "WARN",
        "not read: postgres loom.a_unindexed: has no plain btree index led by org",
    )
    assert "cell_stray_orgs_unchecked" not in lines
