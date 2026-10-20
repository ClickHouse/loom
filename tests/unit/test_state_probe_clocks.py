"""The state probe reads cheaply on the fast clock and lets the
whole-table readings ride along from a slow one."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from server.observability import metrics_gauges, otel_probes

ROOT = Path(__file__).resolve().parents[2]
OTEL = ROOT / "server" / "observability" / "otel_probes.py"


class _Rows:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows


class _Client:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def query(self, sql: str) -> _Rows:
        self.queries.append(sql)
        return _Rows(self.rows)


def test_fast_probe_merges_the_cached_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    fast = _Client([(643122, 5, 0, 139644, 1764375, 0.0)])
    monkeypatch.setattr(otel_probes, "_probe_client", lambda: fast)
    monkeypatch.setattr(
        otel_probes,
        "_probe_invariants_cached",
        lambda: {
            "embed_pending": 3,
            "divergent_entities": 0,
            "orphan_vectors": 1,
            "mutations_pending": 2,
        },
    )
    out = otel_probes._probe_state()
    # The mutation level rides the census clock too.
    assert out["mutations_pending"] == 2
    assert out["vectors_total"] == 643122 and out["usearch_adds_total"] == 1764375
    # The vector table's patch-part count rides the fast probe.
    assert out["memory_embeddings_patch_parts"] == 5
    assert "startsWith(name, 'patch')" in fast.queries[0]
    assert out["embed_pending"] == 3 and out["orphan_vectors"] == 1
    sql = fast.queries[0]
    # The fast statement carries none of the whole-table readings…
    for whole_table in (
        "AS embed_pending",
        "AS divergent_entities",
        "AS orphan_vectors",
        "AS mutations_pending",  # two seconds a read on Cloud
    ):
        assert whole_table not in sql
    # …and classifies vector reads by the columns they touched, not by text.
    assert "has(columns, concat({loom_db:String}, '.memory_embeddings.embedding'))" in sql
    assert "position(query" not in sql


def test_invariants_refresh_once_per_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    calls: list[int] = []

    def _probe() -> dict[str, Any]:
        calls.append(1)
        return {"embed_pending": len(calls), "divergent_entities": 0, "orphan_vectors": 0}

    monkeypatch.setattr(otel_probes, "_probe_invariants", _probe)
    monkeypatch.setattr(settings, "otel_invariants_probe_s", 900)
    monkeypatch.setattr(otel_probes, "_INVARIANTS_CACHE", {"at": 0.0, "value": None})
    # The census is read where the supervisor lease is held.
    from server.workers import leader

    monkeypatch.setattr(leader, "_CURRENT", _lease(True))
    clock = {"t": 1000.0}
    monkeypatch.setattr(otel_probes.time, "monotonic", lambda: clock["t"])

    assert otel_probes._probe_invariants_cached()["embed_pending"] == 1
    clock["t"] += 100
    assert otel_probes._probe_invariants_cached()["embed_pending"] == 1  # within the TTL: cached
    clock["t"] += 900
    assert otel_probes._probe_invariants_cached()["embed_pending"] == 2  # TTL lapsed: refreshed
    assert len(calls) == 2


def test_invariants_statement_is_the_whole_table_half() -> None:
    src = otel_probes._invariants_sql()
    for key in (
        "AS mutations_pending",
        "AS embed_pending",
        "AS divergent_entities",
        "AS orphan_vectors",
        "AS orphan_entity_vectors",
        "AS orphan_graph_namespaces",
        "AS live_ns_unregistered",
        "AS live_rows_unregistered",
        "AS dim0_with_vector",
        "AS tombstone_status_drift",
        "AS active_past_valid_to",
        "AS high_with_embedding",
    ):
        assert key in src
    assert "_probe_client()" in inspect.getsource(otel_probes._probe_invariants)
    # One FINAL pass per table, each read once (no layer re-reads).
    for table in ("memory", "memory_embeddings", "entity_vectors"):
        assert src.count(f"FROM loom.{table} FINAL") == 1, table
    assert "split_intersecting_parts_ranges_into_layers_final = 0" in src
    assert "split_intersecting_parts_ranges_into_layers_final = 0" in otel_probes._CODE_COUNTS_SQL


def test_census_clocks_default_to_an_hour() -> None:
    from server.settings.catalog import ObservabilitySettings

    fields = ObservabilitySettings.model_fields
    assert fields["otel_invariants_probe_s"].default == 3600
    assert fields["otel_code_counts_cache_s"].default == 3600
    assert fields["otel_retention_census_s"].default == 3600
    # Under FINAL the length() -> size-subcolumn rewrite does not happen: the
    # censuses name the subcolumn, or they read every vector and every value.
    assert "embedding.size0" in otel_probes._invariants_sql()
    assert "length(embedding) >" not in otel_probes._invariants_sql()
    assert "value.size < 25" in otel_probes._RETENTION_SQL
    assert "split_intersecting_parts_ranges_into_layers_final = 0" in otel_probes._RETENTION_SQL
    assert fields["otel_census_scope"].default == "leader"
    # The invariant SQL still sits after the fast statement's last level so the
    # integrity tests' source slices keep covering both joins.
    text = OTEL.read_text()
    assert text.index("AS mutations_pending") < text.index("AS divergent_entities")


# A whole-table census is read by the process that holds the
# supervisor lease; every other process exports no point for those series.
def _lease(held: bool) -> Any:
    from server.workers import leader

    lease = leader.SupervisorLease("on" if held else "off")
    return lease


def test_census_probes_skip_where_the_lease_is_not_held(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings
    from server.workers import leader

    monkeypatch.setattr(settings, "otel_census_scope", "leader")
    calls: list[str] = []
    monkeypatch.setattr(
        otel_probes, "_probe_invariants", lambda: calls.append("inv") or {"orphan_vectors": 0}
    )
    monkeypatch.setattr(otel_probes, "_INVARIANTS_CACHE", {"at": 0.0, "value": None})

    def _code() -> dict[str, Any]:
        calls.append("code")
        return {"symbols": []}

    wrapped = otel_probes._census_only(_code)
    # A pure web replica / an isolated bench server: no lease object at all.
    monkeypatch.setattr(leader, "_CURRENT", None)
    assert otel_probes._probe_invariants_cached() == {}
    assert wrapped() is None
    # A sibling web worker: the lease exists but another process holds it.
    monkeypatch.setattr(leader, "_CURRENT", _lease(False))
    assert otel_probes._probe_invariants_cached() == {}
    assert wrapped() is None
    assert calls == []
    # The lease holder reads the census.
    monkeypatch.setattr(leader, "_CURRENT", _lease(True))
    assert otel_probes._probe_invariants_cached() == {"orphan_vectors": 0}
    assert wrapped() == {"symbols": []}
    assert calls == ["inv", "code"]


def test_census_scope_process_keeps_every_process_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings
    from server.workers import leader

    monkeypatch.setattr(settings, "otel_census_scope", "process")
    monkeypatch.setattr(leader, "_CURRENT", None)
    assert otel_probes._census_reads_here() is True
    assert otel_probes._census_only(lambda: {"k": 1})() == {"k": 1}


def test_state_gauge_probes_wrap_the_censuses_and_not_the_fast_probe() -> None:
    probes = otel_probes._state_gauge_probes()
    assert probes["ch_probe"] is otel_probes._probe_state  # per-replica counters: every process
    for name in (
        "code_probe",
        "retention_probe",
        "agent_labels_probe",
        "storage_probe",
        "agent_trusts_probe",
    ):
        assert getattr(probes[name], "__wrapped__", None) is not None, name


def _census_cache(monkeypatch: pytest.MonkeyPatch, probe: Any) -> Any:
    from server.settings import settings
    from server.workers import leader

    monkeypatch.setattr(settings, "otel_census_scope", "leader")
    monkeypatch.setattr(leader, "_CURRENT", _lease(True))
    return metrics_gauges._TTLCache(300.0, otel_probes._census_only(probe))


def _observed(cache: Any, key: str) -> list[float]:
    return [o.value for o in metrics_gauges._cached_series(cache, key)(None)]


def test_a_refresh_off_the_lease_clears_the_holders_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers import leader

    reading = {"agent_trust_used_ratio": [(0.9, {"loom.org": "acme", "loom.trust": "fleet"})]}
    cache = _census_cache(monkeypatch, lambda: reading)
    cache._refresh()
    assert _observed(cache, "agent_trust_used_ratio") == [0.9]
    # The lease moves to another process while a refresh is on its way: its None
    # answer clears the reading, which a later read on the lease must not serve.
    monkeypatch.setattr(leader, "_CURRENT", _lease(False))
    cache._refresh()
    assert cache._value is None


def test_a_handoff_stops_the_old_holders_series_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.workers import leader

    cache = _census_cache(monkeypatch, lambda: {"agent_trust_used_ratio": [(0.9, {})]})
    cache._refresh()
    assert _observed(cache, "agent_trust_used_ratio") == [0.9]
    monkeypatch.setattr(leader, "_CURRENT", _lease(False))
    # Within the TTL and before any refresh: nothing from the old reading.
    assert cache.get() is None
    assert _observed(cache, "agent_trust_used_ratio") == []
    assert not cache._refreshing


def test_a_refresh_skipped_off_the_lease_leaves_the_next_census_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers import leader

    calls: list[float] = []
    now = [1000.0]
    monkeypatch.setattr(metrics_gauges.time, "monotonic", lambda: now[0])

    def probe() -> Any:
        calls.append(now[0])
        return {"agent_trust_agents": [(3.0, {})]}

    cache = _census_cache(monkeypatch, probe)
    cache._refresh()  # the holder's census at t=1000
    # A refresh started on the lease runs after it is lost; no read sees that state.
    monkeypatch.setattr(leader, "_CURRENT", _lease(False))
    now[0] = 1301.0
    cache._refresh()
    # The lease comes back: the census is taken at once, not at t=1601.
    monkeypatch.setattr(leader, "_CURRENT", _lease(True))
    now[0] = 1320.0
    cache.get()
    _drain(cache)
    assert calls == [1000.0, 1320.0]
    assert _observed(cache, "agent_trust_agents") == [3.0]


def test_a_failed_census_waits_out_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float] = []
    now = [1000.0]
    monkeypatch.setattr(metrics_gauges.time, "monotonic", lambda: now[0])

    def probe() -> Any:
        calls.append(now[0])
        raise RuntimeError("ClickHouse away")

    cache = _census_cache(monkeypatch, probe)
    cache._refresh()
    now[0] = 1010.0
    cache.get()
    assert not cache._refreshing and calls == [1000.0]


def test_a_process_back_on_the_lease_takes_its_census_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.workers import leader

    calls: list[float] = []
    now = [1000.0]
    monkeypatch.setattr(metrics_gauges.time, "monotonic", lambda: now[0])

    def probe() -> Any:
        calls.append(now[0])
        return {"agent_trust_agents": [(3.0, {})]}

    cache = _census_cache(monkeypatch, probe)
    cache._refresh()  # the holder's census at t=1000
    monkeypatch.setattr(leader, "_CURRENT", _lease(False))
    now[0] = 1010.0
    assert cache.get() is None  # off the lease: nothing
    # Back on the lease within the 300 s TTL: the census is taken now, not at t=1300.
    monkeypatch.setattr(leader, "_CURRENT", _lease(True))
    now[0] = 1020.0
    cache.get()
    _drain(cache)
    assert calls == [1000.0, 1020.0]
    assert _observed(cache, "agent_trust_agents") == [3.0]
    # A census taken keeps its TTL.
    now[0] = 1060.0
    cache.get()
    assert not cache._refreshing and calls == [1000.0, 1020.0]


def _drain(cache: Any) -> None:
    import time

    deadline = time.time() + 5.0
    while time.time() < deadline:
        with cache._lock:
            if not cache._refreshing:
                return
        time.sleep(0.005)
    raise AssertionError("background refresh never completed")


def test_a_failed_census_keeps_its_last_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    answers: list[Any] = [{"agent_trust_agents": [(3.0, {})]}]

    def probe() -> Any:
        if not answers:
            raise RuntimeError("ClickHouse away")
        return answers.pop()

    cache = _census_cache(monkeypatch, probe)
    cache._refresh()
    cache._refresh()  # the probe fails: the holder's last reading stands
    assert _observed(cache, "agent_trust_agents") == [3.0]
