"""loom.instances: each process's heartbeat row, the live replica count the rate
limiter divides by, and the beat's drift check and leader publish."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core import deployment_config as dc
from server.core import instances, ratelimit
from server.core.ratelimit import Limit, Limiter
from server.settings import settings


class _Ch:
    """Holds instance rows by key, as KeeperMap does, and the config row."""

    def __init__(self, live: list[dict[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.extra_live = live or []
        self.executed: list[str] = []
        self.config: dict[str, Any] | None = None

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        if "system.tables" in sql:
            return {"engine": "KeeperMap"}
        if self.config is None:
            return None
        import json

        return {"value": json.dumps(self.config), "published_by": "", "published_at": ""}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        assert params is not None
        mine = [
            {**r, "last_stopped": r["stopped"]}
            for r in self.rows.values()
            if r["deployment"] == params["dep"] and not r["stopped"]
        ]
        return mine + self.extra_live

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        if table == instances.TABLE:
            for r in rows:
                self.rows[r["instance_id"]] = r
        else:
            import json

            self.config = json.loads(str(rows[0]["value"]))

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        self.executed.append(sql)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Any:
    instances._reset_for_tests()
    monkeypatch.setattr(settings, "deployment", True)
    monkeypatch.setattr(settings, "replica_count", 1)
    monkeypatch.setattr(settings, "web_workers", 1)
    monkeypatch.setitem(dc.STATE, "drift", [])
    yield
    instances._reset_for_tests()


def test_a_beat_writes_this_process_row_with_its_fingerprints() -> None:
    ch = _Ch()
    asyncio.run(instances.tick(ch, leader=False))
    row = ch.rows[instances.instance_id()]
    assert row["deployment"] == 1 and row["stopped"] == 0
    assert row["build_commit"] == settings.build_commit
    assert row["config_fp"] == dc.fingerprints(settings)["config_fp"]
    assert {"settings_fp", "keys_fp", "started_at", "host", "pid"} <= set(row)
    assert row["serving"] == 1 and row["schema_version"] == instances.schema_version() > 0


@pytest.mark.parametrize("why", ["draining", "drifted"])
def test_a_draining_or_drifted_process_takes_no_rate_limit_share(
    monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    """It answers no traffic, so the live replicas' shares leave it out."""
    if why == "draining":
        monkeypatch.setattr("server.core.readiness.draining", lambda: True)
    else:
        monkeypatch.setattr(dc, "drift", lambda: ["LOOM_EMBEDDING_MODEL differs"])
    assert instances.row(stopped=False)["serving"] == 0


def test_a_stopped_process_stops_counting() -> None:
    ch = _Ch()
    asyncio.run(instances.tick(ch, leader=False))
    assert asyncio.run(instances.read_live(ch))
    asyncio.run(instances.stop(ch))
    assert asyncio.run(instances.read_live(ch)) == []


def test_the_replica_count_is_the_live_hosts() -> None:
    """Three hosts live: each one's cap is a third, though the setting says 1."""
    others = [
        {"instance_id": f"i{n}", "host": f"h{n}", "config_fp": "", "serving": 1} for n in (1, 2)
    ]
    asyncio.run(instances.tick(_Ch(others), leader=False))
    assert instances.live_hosts() == 3
    assert ratelimit.replica_count() == 3
    assert ratelimit.effective_divisor(shared=True) == 3


def test_two_workers_of_one_host_are_one_replica(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "web_workers", 2)
    twin = [{"instance_id": "w2", "host": instances._host(), "config_fp": "", "serving": 1}]
    asyncio.run(instances.tick(_Ch(twin), leader=False))
    assert ratelimit.replica_count() == 1
    assert ratelimit.effective_replicas() == 2


def test_a_workers_host_that_serves_no_traffic_is_not_a_replica() -> None:
    """One web host and one workers-only host: the web host keeps its whole cap."""
    workers = [{"instance_id": "w", "host": "workers-host", "config_fp": "", "serving": 0}]
    asyncio.run(instances.tick(_Ch(workers), leader=False))
    assert instances.live_hosts() == 1
    assert ratelimit.effective_divisor(shared=True) == 1
    assert ratelimit.effective_divisor(shared=False) == 1


def test_a_process_that_serves_no_traffic_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "serves_traffic", False)
    ch = _Ch()
    asyncio.run(instances.tick(ch, leader=False))
    assert ch.rows[instances.instance_id()]["serving"] == 0
    assert instances.live_hosts() is None  # nothing serving was read: the setting counts


def test_each_beat_names_the_lease_the_process_contends_for_then(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contends = ["workers_supervisor"]
    monkeypatch.setattr(instances, "_workers_lease", lambda: contends[0])
    ch = _Ch()
    asyncio.run(instances.tick(ch, leader=False))
    assert ch.rows[instances.instance_id()]["workers_lease"] == "workers_supervisor"
    contends[0] = ""  # it gave the lease up, or began draining
    asyncio.run(instances.tick(ch, leader=False))
    assert ch.rows[instances.instance_id()]["workers_lease"] == ""


def test_the_setting_is_the_floor_and_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "replica_count", 4)
    assert ratelimit.replica_count() == 4  # never read
    asyncio.run(instances.tick(_Ch(), leader=False))
    assert ratelimit.replica_count() == 4  # one live host, floored
    read_at, rows = instances._live or (0.0, [])
    instances._live = (read_at - instances.live_window_s() - 1, rows * 9)
    assert instances.live_hosts() is None  # stale: the setting again
    assert ratelimit.replica_count() == 4


def test_outside_the_deployment_the_setting_alone_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deployment", False)
    others = [{"instance_id": "x", "host": "elsewhere", "config_fp": "", "serving": 1}]
    asyncio.run(instances.tick(_Ch(others), leader=False))
    assert ratelimit.replica_count() == 1


def test_live_buckets_follow_a_replica_joining(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ratelimit_shared", "off")
    lim = Limiter()
    lim.set_namespace_limit("ns", "write", Limit(capacity=100, rate=10))
    assert lim.check("ns", "memory.set").limit == 100
    monkeypatch.setattr(instances, "live_hosts", lambda: 2)
    res = lim.check("ns", "memory.set")
    assert res.limit == 50
    assert res.remaining <= 50


@pytest.mark.parametrize("shared", ["off", "on"])
def test_a_one_request_limit_stays_usable_when_a_replica_joins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, shared: str
) -> None:
    """Halved, a 1/1 limit would hold half a token and refuse every request
    forever; it keeps a one-request burst, refilled at the halved rate."""
    now = [1000.0]
    monkeypatch.setattr(settings, "ratelimit_shared", shared)
    monkeypatch.setattr(settings, "ratelimit_shared_path", str(tmp_path / "rl"))
    monkeypatch.setattr(settings, "web_workers", 1)
    lim = Limiter(clock=lambda: now[0])
    lim.set_namespace_limit("ns", ratelimit.op_class_for("memory.set"), Limit(capacity=1, rate=1))
    assert lim.check("ns", "memory.set").allowed
    monkeypatch.setattr(instances, "live_hosts", lambda: 2)
    admitted = 0
    for _ in range(600):  # ten minutes, one request a second
        now[0] += 1.0
        admitted += lim.check("ns", "memory.set").allowed
    assert admitted >= 290  # half the rate, one per two seconds


def test_a_beat_flags_drift_and_the_leader_replaces_only_a_snapshot_it_agrees_with() -> None:
    ch = _Ch()
    stored = dc.snapshot(settings.model_copy(update={"embedding_dim": 7}))
    ch.config = stored
    asyncio.run(instances.tick(ch, leader=False))
    assert any("LOOM_EMBEDDING_DIM" in p for p in dc.drift())
    # A drifted leader keeps the deployment's snapshot: `loom deployment publish` replaces it.
    asyncio.run(instances.tick(ch, leader=True))
    assert ch.config == stored and dc.drift()
    assert ch.executed and "DELETE" in ch.executed[0]  # the leader sweeps old keys
    # No other live instance runs the stored one: the leader adopts its own.
    ch.config = {**dc.snapshot(settings), "note": "another build's snapshot"}
    asyncio.run(instances.tick(ch, leader=True))
    assert ch.config == dc.snapshot(settings) and dc.drift() == []


def test_a_failed_beat_never_raises() -> None:
    class _Down(_Ch):
        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            raise ConnectionError("down")

    asyncio.run(instances.tick(_Down(), leader=True))
    asyncio.run(instances.stop(_Down()))


def test_a_forked_worker_gets_its_own_id(monkeypatch: pytest.MonkeyPatch) -> None:
    first = instances.instance_id()
    monkeypatch.setattr(instances.os, "getpid", lambda: -1)
    assert instances.instance_id() != first


def test_the_boot_beat_and_the_stop_carry_the_heartbeat_tag() -> None:
    """They run in the lifespan's context: tagged for the call only."""
    from server.core.ch import current_query_tag

    tags: list[str] = []

    class _Tagged(_Ch):
        async def insert(self, table: str, rows: list[dict[str, Any]], **kw: Any) -> None:
            tags.append(current_query_tag())
            await super().insert(table, rows, **kw)

    async def _boot_and_stop() -> str:
        ch = _Tagged()
        task = await instances.start(ch, lambda: False, label="t")
        task.cancel()
        await instances.stop(ch)
        return current_query_tag()

    assert asyncio.run(_boot_and_stop()) == ""
    assert tags and set(tags) == {instances.QUERY_TAG}


def test_a_failing_beat_read_is_logged_once_and_the_beat_goes_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    checked: list[bool] = []

    async def fails(_ch: Any) -> None:
        raise RuntimeError("Code: 999. Keeper unavailable")

    async def quiet(*_: Any, **__: Any) -> Any:
        return []

    async def check(_ch: Any) -> None:
        checked.append(True)

    monkeypatch.setattr(instances, "_on_beat", fails)
    monkeypatch.setattr(instances, "beat", quiet)
    monkeypatch.setattr(instances, "read_live", quiet)
    monkeypatch.setattr(dc, "check", check)
    with caplog.at_level("WARNING"):
        asyncio.run(instances.tick(object(), leader=False))
    assert checked == [True]
    assert sum("beat read failed" in r.getMessage() for r in caplog.records) == 1


def test_a_beat_that_missed_clickhouse_runs_no_beat_read(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[bool] = []

    async def read(_ch: Any) -> None:
        reads.append(True)

    async def down(*_: Any, **__: Any) -> Any:
        raise RuntimeError("Code: 210. Connection refused")

    async def quiet(*_: Any, **__: Any) -> Any:
        return None

    monkeypatch.setattr(instances, "_on_beat", read)
    monkeypatch.setattr(instances, "beat", down)
    monkeypatch.setattr(dc, "check", quiet)
    asyncio.run(instances.tick(object(), leader=False))
    assert reads == []


def test_the_lifespan_reads_the_workers_lease_on_each_beat() -> None:
    # The other lifespan wiring tests read the source too: running the lifespan needs a server.
    import inspect

    from server import main

    assert "on_beat=_leader.read_holder" in inspect.getsource(main.lifespan)
