"""The namespace_gc worker retires registered bench
namespaces by idleness through the list-scoped purge saga;
the census SQL it shares with the gauges names the registry, not a prefix."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.identity.store import namespace_census as census
from server.identity.workers import namespace_gc as gc


class _CH:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return list(self.rows)

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((sql, params or {}))
        return {"live_ns_unregistered": 4723, "live_rows_unregistered": 536939}


def _cand(org: str, ns: str, rows: int = 3) -> dict[str, Any]:
    return {"org": org, "namespace": ns, "owner_email": "b", "live_rows": rows, "last_write": None}


@pytest.mark.asyncio
async def test_candidates_are_registered_bench_rows_with_live_memory_and_no_recent_write() -> None:
    ch = _CH()
    await census.bench_gc_candidates(
        idle_hours=24, org="clickhouse", namespaces=("lme-a",), limit=5, ch=ch
    )
    sql, params = ch.calls[0]
    assert "kind = 'bench'" in sql
    assert "loom.namespace_write_marks" in sql
    assert "loom.memory FINAL" in sql and "tombstoned_at IS NULL" in sql
    assert "created_at < now() - INTERVAL {idle:UInt32} HOUR" in sql
    assert "retention_days" not in sql, "a bench namespace is never on a TTL"
    assert "LIKE" not in sql, "the registry, not a prefix, names the candidates"
    assert params == {"idle": 24, "org": "clickhouse", "nss": ["lme-a"], "lim": 5}


@pytest.mark.asyncio
async def test_pass_submits_one_purging_loom_only_list_saga_per_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted: list[Any] = []

    class _Out:
        erasure_id = "e1"
        status = "completed"
        residue_count = 0

    async def _fake_request(inp):
        submitted.append(inp)
        return _Out()

    import server.erasure.app.handlers as erasure_mod

    monkeypatch.setattr(erasure_mod, "handle_erasure_request", _fake_request)
    ch = _CH(
        [
            _cand("dev", "lme-a"),
            _cand("dev", "lme-b"),
            _cand("dev", "mb-c"),
            _cand("clickhouse", "td-d"),
        ]
    )
    res = await gc.run_pass(
        gc.GcConfig(idle_hours=24, batch=2, sagas_per_pass=10, external=False), ch=ch
    )
    assert res.candidates == 4 and res.candidate_rows == 12
    # dev: 3 names → 2 sagas (2 + 1); clickhouse: 1 saga
    assert res.sagas == 3 and res.erased == 4 and res.stopped_on == ""
    first = submitted[0]
    assert first.org == "dev" and first.scope_type == "namespace_list"
    assert first.purge is True and first.external is False
    assert json.loads(first.scope_value) == {
        "namespaces": ["lme-a", "lme-b"],
        "purge": True,
        "external": False,
    }
    assert first.requester == gc.REQUESTER
    assert {s.org for s in submitted} == {"dev", "clickhouse"}


@pytest.mark.asyncio
async def test_sagas_per_pass_bounds_the_work(monkeypatch: pytest.MonkeyPatch) -> None:
    submitted: list[Any] = []

    class _Out:
        erasure_id = "e"
        status = "completed"
        residue_count = 0

    async def _fake_request(inp):
        submitted.append(inp)
        return _Out()

    import server.erasure.app.handlers as erasure_mod

    monkeypatch.setattr(erasure_mod, "handle_erasure_request", _fake_request)
    ch = _CH([_cand("dev", f"lme-{i}") for i in range(5)])
    res = await gc.run_pass(gc.GcConfig(batch=2, sagas_per_pass=1), ch=ch)
    assert len(submitted) == 1 and res.sagas == 1 and res.erased == 2
    # the candidate read is bounded to what the pass can erase
    assert ch.calls[0][1]["lim"] == 2


@pytest.mark.asyncio
async def test_a_saga_that_does_not_complete_stops_the_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Out:
        erasure_id = "e9"
        status = "failed"
        residue_count = 500

    async def _fake_request(inp):
        return _Out()

    import server.erasure.app.handlers as erasure_mod

    monkeypatch.setattr(erasure_mod, "handle_erasure_request", _fake_request)
    ch = _CH([_cand("dev", "lme-a"), _cand("dev", "lme-b")])
    res = await gc.run_pass(gc.GcConfig(batch=1, sagas_per_pass=5), ch=ch)
    assert res.sagas == 1 and res.erased == 0 and res.stopped_on == "e9:failed"


@pytest.mark.asyncio
async def test_dry_run_submits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.erasure.app.handlers as erasure_mod

    async def _boom(inp):
        raise AssertionError("dry run must not submit")

    monkeypatch.setattr(erasure_mod, "handle_erasure_request", _boom)
    ch = _CH([_cand("dev", "lme-a")])
    res = await gc.run_pass(gc.GcConfig(dry_run=True), ch=ch)
    assert res.candidates == 1 and res.sagas == 0 and res.names == ["lme-a"]


def test_config_reads_the_settings() -> None:
    from server.settings import settings

    cfg = gc.GcConfig.from_settings()
    assert cfg.idle_hours == settings.namespace_gc_idle_hours == 24
    assert cfg.batch == settings.namespace_gc_batch == 200
    assert cfg.external is False
    assert cfg.purge is True


def test_worker_is_registered_under_its_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings
    from server.workers import registry

    monkeypatch.setattr(settings, "namespace_gc_worker_enabled", True)
    names = [s.name for s in registry.enabled_workers()]
    assert "namespace_gc" in names
    spec = next(s for s in registry.enabled_workers() if s.name == "namespace_gc")
    assert spec.interval_seconds == settings.namespace_gc_interval_seconds
    monkeypatch.setattr(settings, "namespace_gc_worker_enabled", False)
    assert "namespace_gc" not in [s.name for s in registry.enabled_workers()]


def test_gc_cli_default_prefixes_are_the_bench_prefix_setting() -> None:
    from server.cli import hygiene
    from server.identity.app.namespace_autoregister import bench_prefixes

    assert hygiene._gc_default_prefixes() == bench_prefixes()
    assert "lme-" in bench_prefixes() and "td-" in bench_prefixes()
