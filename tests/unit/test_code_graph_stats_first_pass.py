"""A weekly pass runs first when its table is empty. The
supervisor asks the worker whether its output is missing or stale before
applying the boot stagger; code_graph_stats answers from its
three target tables; code.index_status says what the stats table holds."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.api.models_status import CodeIndexStatusOutput
from server.code.app import index_status
from server.code.workers import graph_stats as cgs
from server.workers import supervisor as sup
from server.workers.registry import WorkerSpec

WEEK = 7 * 24 * 3600
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "files": True,
        "ranks": True,
        "test_map": True,
        "symbol_stats_rows": 97_508,
        "symbol_stats_at": NOW - timedelta(hours=1),
    }
    base.update(over)
    return base


def _due(state: dict[str, Any], *, interval: float = WEEK) -> str:
    return cgs.pass_due(state, interval_seconds=interval, now=NOW)


# ---- the pure check ---------------------------------------------------------


def test_a_namespace_with_everything_written_and_fresh_is_not_due() -> None:
    assert _due(_state()) == ""


def test_a_namespace_without_live_files_is_never_due() -> None:
    # the pass would return at once on it; the stagger is the right wait
    assert _due(_state(files=False, ranks=False, test_map=False, symbol_stats_rows=0)) == ""


def test_each_empty_target_table_makes_the_pass_due_in_order() -> None:
    assert _due(_state(ranks=False)) == "ranks_empty"
    assert _due(_state(test_map=False)) == "test_map_empty"
    assert _due(_state(symbol_stats_rows=0, symbol_stats_at=None)) == "symbol_stats_empty"


def test_stats_older_than_the_interval_are_due_and_the_interval_is_measured_from_the_stamp() -> (
    None
):
    assert _due(_state(symbol_stats_at=NOW - timedelta(days=8))) == "symbol_stats_stale"
    assert _due(_state(symbol_stats_at=NOW - timedelta(days=6))) == ""
    # a naive stamp (ClickHouse DateTime read back) is UTC
    naive = (NOW - timedelta(days=8)).replace(tzinfo=None)
    assert _due(_state(symbol_stats_at=naive)) == "symbol_stats_stale"
    # rows without a stamp cannot be judged stale
    assert _due(_state(symbol_stats_at=None)) == ""
    # a shorter interval moves the line
    assert (
        _due(_state(symbol_stats_at=NOW - timedelta(hours=2)), interval=3600)
        == "symbol_stats_stale"
    )


# ---- the worker's hook over configured repos --------------------------------


class _Cfg:
    def __init__(self, ns: str) -> None:
        self.org, self.namespace, self.label = "o", ns, f"o/{ns}"


class _Repo:
    def __init__(self, states: dict[str, dict[str, Any]]) -> None:
        self.states, self.asked = states, []

    async def graph_stats_state(self, org: str, ns: str) -> dict[str, Any]:
        self.asked.append(ns)
        return self.states[ns]


def _wire(monkeypatch: pytest.MonkeyPatch, states: dict[str, dict[str, Any]]) -> _Repo:
    import server.code.domain.index.repos as repos_mod
    import server.code.store.index as cr

    repo = _Repo(states)
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    monkeypatch.setattr(repos_mod, "configured_repos", lambda: [_Cfg(ns) for ns in states])
    # The fixtures are stamped relative to NOW; the check must read the same
    # clock, or a week after the fixture date every fresh state reads stale.
    monkeypatch.setattr(cgs, "_utcnow", lambda: NOW)
    return repo


async def test_one_namespace_with_an_empty_table_makes_the_first_pass_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _wire(
        monkeypatch,
        {
            "code-a": _state(),
            "code-b": _state(symbol_stats_rows=0, symbol_stats_at=None),
            "code-c": _state(),
        },
    )
    assert await cgs.first_pass_due() is True
    # Every namespace is asked — the pass runs the cheap legs
    # only for the due ones, so the check and the work name the same set
    assert repo.asked == ["code-a", "code-b", "code-c"]
    assert await cgs.due_namespaces() == {"o/code-b": "symbol_stats_empty"}


async def test_every_namespace_fresh_means_not_due(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _wire(monkeypatch, {"code-a": _state(), "code-b": _state()})
    assert await cgs.first_pass_due() is False
    assert repo.asked == ["code-a", "code-b"]


# ---- the supervisor honours the answer --------------------------------------


async def _run_supervised(spec: WorkerSpec, wait_s: float) -> None:
    s = sup.WorkerSupervisor()
    task = asyncio.create_task(s._supervise(spec))
    await asyncio.sleep(wait_s)
    s._stopping.set()
    await asyncio.wait_for(task, timeout=2)


async def test_a_due_worker_runs_its_first_pass_before_the_stagger() -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1

    async def _due() -> bool:
        return True

    spec = WorkerSpec("code_graph_stats", 3600, _pass, first_pass_due=_due)
    assert sup.first_pass_delay(spec) > 60  # the stagger alone would hold it for minutes
    await _run_supervised(spec, 0.1)
    assert calls["n"] == 1


async def test_a_worker_that_is_not_due_keeps_the_stagger() -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1

    async def _not_due() -> bool:
        return False

    spec = WorkerSpec("code_graph_stats", 3600, _pass, first_pass_due=_not_due)
    await _run_supervised(spec, 0.1)
    assert calls["n"] == 0


async def test_a_failing_or_slow_check_reads_as_not_due(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _pass() -> None:
        calls["n"] += 1

    async def _boom() -> bool:
        raise RuntimeError("clickhouse away")

    async def _slow() -> bool:
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(sup, "FIRST_PASS_DUE_TIMEOUT_SECONDS", 0.05)
    assert await sup.first_pass_is_due(WorkerSpec("w", 3600, _pass, first_pass_due=_boom)) is False
    assert await sup.first_pass_is_due(WorkerSpec("w", 3600, _pass, first_pass_due=_slow)) is False
    assert await sup.first_pass_is_due(WorkerSpec("w", 3600, _pass)) is False  # no hook
    await _run_supervised(WorkerSpec("code_graph_stats", 3600, _pass, first_pass_due=_boom), 0.1)
    assert calls["n"] == 0


def test_the_registry_wires_the_hook_for_code_graph_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings
    from server.workers.registry import enabled_workers

    monkeypatch.setattr(settings, "code_graph_stats_enabled", True)
    spec = next(w for w in enabled_workers() if w.name == "code_graph_stats")
    assert spec.first_pass_due is cgs.first_pass_due
    assert spec.interval_seconds == settings.code_graph_stats_interval_seconds


# ---- code.index_status -------------------------------------------------------


def test_index_status_output_defaults_keep_old_clients_whole() -> None:
    out = CodeIndexStatusOutput(repo="r", branch="b", namespace="n")
    assert out.symbol_stats_rows == 0 and out.symbol_stats_at == ""


class _StatsRepo:
    def __init__(self, answer: Any) -> None:
        self.answer, self.reads = answer, 0

    async def symbol_stats_summary(self, org: str, ns: str) -> Any:
        self.reads += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def test_status_fields_read_the_stats_table_once_per_ten_minutes_and_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_status._SYMBOL_STATS_CACHE.clear()
    repo = _StatsRepo((97_508, datetime(2026, 9, 16, 8, 30)))
    monkeypatch.setattr(index_status, "CodeRepo", lambda: repo)
    assert await index_status.symbol_stats_summary_cached("o", "code-x") == (
        97_508,
        "2026-09-16T08:30:00+00:00",
    )
    assert await index_status.symbol_stats_summary_cached("o", "code-x") == (
        97_508,
        "2026-09-16T08:30:00+00:00",
    )
    assert repo.reads == 1  # the second answer came from the cache
    # a namespace the pass never wrote
    monkeypatch.setattr(index_status, "CodeRepo", lambda: _StatsRepo((0, None)))
    assert await index_status.symbol_stats_summary_cached("o", "code-empty") == (0, "")
    # a deployment without the table: status stays whole
    monkeypatch.setattr(index_status, "CodeRepo", lambda: _StatsRepo(RuntimeError("no table")))
    assert await index_status.symbol_stats_summary_cached("o", "code-old") == (0, "")
    index_status._SYMBOL_STATS_CACHE.clear()
