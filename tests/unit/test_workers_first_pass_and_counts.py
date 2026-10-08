"""Every worker whose table is empty runs at boot, and every
pass says what it did.

The generic empty-table rule (`server.workers.first_pass`): a spec declares
the tables it fills and the namespaces it fills them for; one statement per
table names the namespaces held, the rest are due; an unreadable table is
due. The supervisor derives the empty-table hook from it, so a daily worker with an
empty table runs in the boot tick instead of after `crc32(name) % interval`.
The status row reads the pass's own `items_planned` / `items_done` /
`rows_written` when the worker states them (idle = all three zero), else the
name heuristic. CH-free: fake clients record the statements."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.observability import events as ev_mod
from server.settings import settings
from server.workers import first_pass as fp
from server.workers import pass_context
from server.workers import status as wstatus
from server.workers import status_store as store
from server.workers import supervisor as sup
from server.workers.registry import WorkerSpec
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json


class _FakeCH:
    """`held` maps a table name to the (org, namespace) pairs it holds;
    a table in `broken` raises."""

    def __init__(self, held: dict[str, set[tuple[str, str]]], broken: set[str] = frozenset()):
        self.held, self.broken = held, set(broken)
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.statements.append((sql, dict(params or {})))
        table = sql.split(" FROM ", 1)[1].split(" ", 1)[0]
        if table in self.broken:
            raise RuntimeError(f"Unknown table {table}")
        return [{"org": o, "namespace": n} for o, n in sorted(self.held.get(table, set()))]

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        return {"cursor": ""}

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


LINKS = fp.OwnedTable("loom.qa_link_candidates", namespace_column="qa_namespace")
DAYS = fp.OwnedTable("loom.qa_rollup_days", namespace_column="qa_namespace")
STATS = fp.OwnedTable("loom.code_symbol_stats")
SCOPE = [("o", "tracker-a"), ("o", "tracker-b"), ("o", "tracker-c")]


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    class _Emitter:
        def emit(self, event) -> None:
            pass

    monkeypatch.setattr(ev_mod, "get_emitter", lambda: _Emitter())
    wstatus.get_registry().reset()
    yield
    store.reset_for_tests()
    wstatus.get_registry().reset()


# --------------------------------------------------------------------------- the rule


def test_the_held_read_is_one_plain_statement_on_the_key_prefix() -> None:
    sql = LINKS.held_sql()
    assert sql.startswith(
        "SELECT org, qa_namespace AS namespace FROM loom.qa_link_candidates WHERE "
    )
    assert "FINAL" not in sql and "PREWHERE" not in sql
    assert (
        "org IN {orgs:Array(String)}" in sql and "qa_namespace IN {namespaces:Array(String)}" in sql
    )
    assert sql.endswith("GROUP BY org, namespace")
    # a code-plane table names its columns as they are
    assert STATS.held_sql().startswith("SELECT org, namespace FROM loom.code_symbol_stats WHERE ")
    assert LINKS.short == "qa_link_candidates"


async def test_held_namespaces_is_scoped_and_empty_scope_reads_nothing() -> None:
    ch = _FakeCH(
        {
            "loom.qa_link_candidates": {
                ("o", "tracker-a"),
                ("o", "tracker-z"),
                ("other", "tracker-b"),
            }
        }
    )
    held = await fp.held_namespaces(LINKS, SCOPE, ch=ch)
    assert held == {("o", "tracker-a")}  # rows outside the scope do not count
    assert len(ch.statements) == 1
    assert ch.statements[0][1] == {
        "orgs": ["o"],
        "namespaces": ["tracker-a", "tracker-b", "tracker-c"],
    }
    assert await fp.held_namespaces(LINKS, [], ch=ch) == set() and len(ch.statements) == 1


async def test_empty_namespaces_names_the_first_empty_table_one_statement_per_table() -> None:
    ch = _FakeCH(
        {
            "loom.qa_rollup_days": {("o", "tracker-a"), ("o", "tracker-b")},
            "loom.qa_test_daily": {("o", "tracker-a")},
        }
    )
    due = await fp.empty_namespaces(
        (DAYS, fp.OwnedTable("loom.qa_test_daily", "qa_namespace")), SCOPE, ch=ch
    )
    assert due == {"o/tracker-c": "qa_rollup_days_empty", "o/tracker-b": "qa_test_daily_empty"}
    assert len(ch.statements) == 2  # never one per namespace
    # the second table is asked only about the namespaces the first one held
    assert ch.statements[1][1]["namespaces"] == ["tracker-a", "tracker-b"]


async def test_every_namespace_held_is_not_due_and_the_remaining_tables_are_not_asked() -> None:
    ch = _FakeCH({"loom.qa_rollup_days": set(SCOPE), "loom.qa_test_daily": set(SCOPE)})
    assert (
        await fp.empty_namespaces(
            (DAYS, fp.OwnedTable("loom.qa_test_daily", "qa_namespace")), SCOPE, ch=ch
        )
        == {}
    )
    assert len(ch.statements) == 2
    ch2 = _FakeCH({"loom.qa_rollup_days": set(SCOPE)})
    assert await fp.empty_namespaces((DAYS,), SCOPE, ch=ch2) == {}
    assert await fp.empty_namespaces((DAYS,), [], ch=ch2) == {} and len(ch2.statements) == 1


async def test_an_unreadable_table_reads_as_due_with_its_own_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ch = _FakeCH({}, broken={"loom.qa_test_daily"})
    with caplog.at_level("WARNING"):
        due = await fp.empty_namespaces(
            (fp.OwnedTable("loom.qa_test_daily", "qa_namespace"),), SCOPE[:1], ch=ch
        )
    assert due == {"o/tracker-a": "qa_test_daily_unknown"}
    assert "could not be read" in caplog.text


# --------------------------------------------------------------------------- the spec


async def _noop() -> None:
    return None


def test_the_spec_derives_the_hook_from_owned_tables_and_scope_and_its_own_hook_wins() -> None:
    async def _own() -> bool:
        return True

    plain = WorkerSpec("w", 3600, _noop)
    assert plain.first_pass_hook() is None
    derived = WorkerSpec("w", 3600, _noop, owned_tables=(LINKS,), due_scope=lambda: SCOPE)
    hook = derived.first_pass_hook()
    assert hook is not None and hook.__name__ == "w_first_pass_due"
    own = WorkerSpec(
        "w", 3600, _noop, first_pass_due=_own, owned_tables=(LINKS,), due_scope=lambda: SCOPE
    )
    assert own.first_pass_hook() is _own
    # tables without a scope, or a scope without tables, derive nothing
    assert WorkerSpec("w", 3600, _noop, owned_tables=(LINKS,)).first_pass_hook() is None
    assert WorkerSpec("w", 3600, _noop, due_scope=lambda: SCOPE).first_pass_hook() is None


async def test_the_derived_hook_answers_from_the_store_and_logs_each_due_namespace(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ch = _FakeCH({"loom.qa_link_candidates": {("o", "tracker-a")}})
    monkeypatch.setattr(fp, "_client", lambda: ch)
    spec = WorkerSpec(
        "tracker_links", 86400, _noop, owned_tables=(LINKS,), due_scope=lambda: SCOPE[:2]
    )
    with caplog.at_level("INFO"):
        assert await sup.first_pass_is_due(spec) is True
    assert "tracker_links: first pass due for o/tracker-b: qa_link_candidates_empty" in caplog.text
    ch.held["loom.qa_link_candidates"] = set(SCOPE)
    assert await sup.first_pass_is_due(spec) is False


# --------------------------------------------------------------------------- the supervisor


async def _run_supervised(spec: WorkerSpec, wait_s: float) -> None:
    s = sup.WorkerSupervisor()
    task = asyncio.create_task(s._supervise(spec))
    await asyncio.sleep(wait_s)
    s._stopping.set()
    await asyncio.wait_for(task, timeout=2)


async def test_a_daily_worker_with_an_empty_table_runs_in_the_boot_tick_with_trigger_first_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _FakeCH({"loom.qa_link_candidates": set()})
    monkeypatch.setattr(fp, "_client", lambda: ch)
    store.reset_for_tests(client=ch)
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1
        with wstatus.record_pass("tracker_links") as rec:
            pass_context.set_namespace("tracker-a")
            rec.outcome = {"items_planned": 3, "items_done": 3, "rows_written": 12, "wall_ms": 40}

    spec = WorkerSpec(
        "tracker_links", 86400, _pass, owned_tables=(LINKS,), due_scope=lambda: SCOPE[:1]
    )
    assert (
        sup.first_pass_delay(spec) == 82035.0
    )  # the stagger without the empty-table rule (hash of the worker's name; 27537 as `qa_links`)
    await _run_supervised(spec, 0.2)
    await store.flush()
    assert calls["n"] == 1
    rows = [r for _t, rows in ch.inserts for r in rows]
    assert len(rows) == 1
    row = rows[0]
    assert row["trigger"] == "first_pass" and row["status"] == "ok"
    assert (row["items_planned"], row["items_done"], row["rows_written"]) == (3, 3, 12)
    assert row["namespace"] == "tracker-a"


async def test_a_daily_worker_whose_table_is_held_keeps_its_stagger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _FakeCH({"loom.qa_link_candidates": set(SCOPE)})
    monkeypatch.setattr(fp, "_client", lambda: ch)
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1

    spec = WorkerSpec("tracker_links", 86400, _pass, owned_tables=(LINKS,), due_scope=lambda: SCOPE)
    await _run_supervised(spec, 0.2)
    assert calls["n"] == 0 and len(ch.statements) == 1


# --------------------------------------------------------------------------- the registry


def test_the_registry_wires_the_two_tracker_workers_with_their_tables_and_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers.registry import enabled_workers

    monkeypatch.setattr(settings, "tracker_links_enabled", True)
    monkeypatch.setattr(settings, "tracker_test_daily_enabled", True)
    monkeypatch.setattr(settings, "tracker_org", "clickhouse")
    # Which CI history the daily roll-up reads is configured,
    # here the ClickHouse deployment's (its public CI, for its public repository)
    monkeypatch.setattr(
        settings,
        "tracker_checks_from",
        "remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', '')",
    )
    monkeypatch.setattr(settings, "tracker_checks_repo", "ClickHouse/ClickHouse")
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(
            '{"ClickHouse/ClickHouse": "tracker-ch", "ClickHouse/example-private": "tracker-priv", "Other/repo": "tracker-other"}',
            restricted="tracker-priv",
        ),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    specs = {s.name: s for s in enabled_workers()}
    links, daily = specs["tracker_links"], specs["tracker_test_daily"]
    assert [t.table for t in links.owned_tables] == ["loom.qa_link_candidates"]
    assert links.owned_tables[0].namespace_column == "qa_namespace"
    assert [t.table for t in daily.owned_tables] == ["loom.qa_rollup_days", "loom.qa_test_daily"]
    assert links.first_pass_hook() is not None and daily.first_pass_hook() is not None
    # the restricted namespace is skipped by the pass, so it is not in scope
    assert links.due_scope() == [("clickhouse", "tracker-ch"), ("clickhouse", "tracker-other")]
    # only a namespace serving the public repository has a checks history
    assert daily.due_scope() == [("clickhouse", "tracker-ch")]
    # code_graph_stats keeps its own, finer hook (stale stamps are due too)
    monkeypatch.setattr(settings, "code_graph_stats_enabled", True)
    cgs = {s.name: s for s in enabled_workers()}["code_graph_stats"]
    assert cgs.first_pass_due is not None and cgs.owned_tables == ()


# --------------------------------------------------------------------------- the counts


def test_stated_counts_win_over_the_name_heuristic_field_by_field() -> None:
    # every key present: read exactly, the `_rows` keys are not summed in
    assert store.counts(
        {"items_planned": 5, "items_done": 2, "rows_written": 7, "test_map_rows": 99}
    ) == (5, 2, 7)
    # a stated key beside heuristic ones: stated for that field, heuristic for the rest
    assert store.counts(
        {"rows_written": 7, "symbol_stats_rows": 99, "scanned": 4, "processed": 3}
    ) == (4, 3, 7)
    # `rows_written` is itself a stated key: read exactly, `unit_risk_rows` is not summed in;
    # the name heuristic still reads planned and done
    assert store.counts(
        {"scanned": 10, "processed": 4, "rows_written": 3, "unit_risk_rows": 2}
    ) == (10, 4, 3)
    # no stated key at all: the name heuristic
    assert store.counts(
        {"scanned": 10, "processed": 4, "test_map_rows": 3, "unit_risk_rows": 2}
    ) == (10, 4, 5)
    assert store.counts({"items_planned": -1, "items_done": 2.9}) == (0, 2, 0)
    assert store.COUNT_KEYS == ("items_planned", "items_done", "rows_written")


def test_a_pass_that_states_zero_counts_is_idle_whatever_else_it_measured() -> None:
    zero = {"items_planned": 0, "items_done": 0, "rows_written": 0, "wall_ms": 812, "namespaces": 1}
    assert store.classify(success=True, outcome=zero) == "idle"
    assert store.classify(success=True, outcome={**zero, "items_done": 1}) == "ok"
    assert store.classify(success=True, outcome={**zero, "rows_written": 1}) == "ok"
    # planned but nothing done or written is still idle (the pass found work and did none of it
    # without failing a leg: the counts say so on the row)
    assert store.classify(success=True, outcome={**zero, "items_planned": 4}) == "ok"
    # the name rule for workers without stated counts
    assert store.classify(success=True, outcome={"processed": 0, "skipped": 0}) == "idle"
    assert store.classify(success=True, outcome={"processed": 0, "wall_ms": 5}) == "ok"
    assert store.classify(success=True, partial_failures=1, outcome=zero) == "partial"


# --------------------------------------------------------------------------- the writers


def test_tracker_links_states_its_counts_first_and_tracker_test_daily_counts_every_row() -> None:
    from server.tracker.workers.links import TrackerLinksResult
    from server.tracker.workers.test_daily import TrackerTestDailyResult

    r = TrackerLinksResult(
        issues_seen=40,
        issues_scored=30,
        issues_empty=5,
        issues_final=3,
        issues_unavailable=2,
        rows_written=600,
        wall_ms=1200,
        namespaces=1,
    )
    out = r.outcome()
    assert list(out)[:3] == ["items_planned", "items_done", "rows_written"]
    assert (out["items_planned"], out["items_done"], out["rows_written"]) == (40, 38, 600)
    assert store.counts(out) == (40, 38, 600) and store.classify(success=True, outcome=out) == "ok"
    idle = TrackerLinksResult(namespaces=1, wall_ms=300).outcome()
    assert store.classify(success=True, outcome=idle) == "idle"

    d = TrackerTestDailyResult(namespaces=1, days_planned=6, days_rolled=4, rows=4000, hour_rows=60)
    assert d.days_planned == 6 and d.hour_rows == 60


async def test_tracker_links_stamps_the_namespace_it_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.tracker.domain.errors import TrackerUnavailable
    from server.tracker.workers import links as wk

    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(
            '{"ClickHouse/ClickHouse": "tracker-ch", "ClickHouse/p": "tracker-priv"}',
            restricted="tracker-priv",
        ),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())

    class _Down:
        def __init__(self, org: str, ns: str) -> None:
            pass

        async def pr_token_stats(self, org: str, ns: str) -> dict[str, Any]:
            raise TrackerUnavailable("credential unset")

    ctx = pass_context.begin("tracker_links")
    try:
        result = wk.TrackerLinksResult()
        await wk._run(result, repo=object(), source_factory=_Down)
    finally:
        pass_context.clear()
    assert ctx.namespace == "tracker-ch" and result.skipped_restricted == 1 and ctx.count == 1
    assert result.outcome()["items_planned"] == 0


# --------------------------------------------------------------------------- the cancelled row


async def test_a_stop_during_the_interval_sleep_writes_no_cancelled_row_but_a_stop_mid_pass_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _FakeCH({})
    store.reset_for_tests(client=ch)

    async def _quick() -> None:
        with wstatus.record_pass("idler") as rec:
            rec.outcome = {"items_planned": 2, "items_done": 2, "rows_written": 9}

    async def _slow() -> None:
        with wstatus.record_pass("mid") as rec:
            rec.outcome = {"items_planned": 1}
            await asyncio.sleep(30)

    s = sup.WorkerSupervisor()
    idler = asyncio.create_task(s._supervise(WorkerSpec("idler", 1.0, _quick, max_pass_seconds=60)))
    mid = asyncio.create_task(s._supervise(WorkerSpec("mid", 1.0, _slow, max_pass_seconds=60)))
    await asyncio.sleep(
        0.2
    )  # idler's pass is done and it sleeps its interval; mid is inside its pass
    s._tasks.extend([idler, mid])
    await s.stop_all(drain_timeout_s=2.0)
    rows = [r for _t, rows in ch.inserts for r in rows]
    by_worker = {
        r["worker"]: [x["status"] for x in rows if x["worker"] == r["worker"]] for r in rows
    }
    # the idle worker's one row is its finished pass; the sleep that was cut is not a pass
    assert by_worker["idler"] == ["ok"]
    # the worker cut mid-pass says so
    assert by_worker["mid"] == ["cancelled"]
    (cut,) = [r for r in rows if r["worker"] == "mid"]
    assert cut["items_planned"] == 0  # the registry has no outcome for a pass that never finished
