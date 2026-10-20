"""The agent-label gauge never carries the self-credit
and caps its namespace label; the storage housekeeping readings are gauges.

CH-free: the transforms are pure and every client is a stub."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

import pytest

from server.observability import metrics_gauges, otel_probes
from tests.unit.test_code_state_gauges import _Meter

# ----------------------------------------------------------- self-credit stays out


def test_agent_labels_transform_drops_every_server_sourced_kind() -> None:
    """A wider read (the pre-split `used_cited`, recall, evidence, contradicted)
    never reaches the series: the exclusion holds in Python as well as in the
    statement's `kinds` parameter."""
    rows = [
        ("o", "demo-full2", "used_cited", 232_350),
        ("o", "demo-full2", "recall", 1_371_321),
        ("o", "demo-full2", "evidence", 2_805),
        ("o", "demo-full2", "contradicted", 40),
        ("o", "demo-full2", "used", 3),
        ("o", "clickgap-private", "harm", 25),
        ("o", "bench-d2-acted", "acted", 1),
    ]
    series = otel_probes._agent_labels_series_from_rows(rows)["agent_labels_24h"]
    kinds = {a["loom.kind"] for _v, a in series}
    assert kinds == {"used", "harm", "acted"}
    by_ns = {(a["loom.namespace"], a["loom.kind"]): v for v, a in series}
    assert by_ns[("demo-full2", "used")] == 3.0
    assert by_ns[("clickgap-private", "harm")] == 25.0
    assert by_ns[("bench-d2-acted", "acted")] == 1.0
    assert not any(v > 1_000 for v, _a in series), "the 232k self-credit leaked"


def test_agent_labels_statement_parameterises_the_kinds_and_reads_one_day() -> None:
    from server.memory.store.usage import AGENT_LABEL_EVENT_TYPES

    sql = otel_probes._AGENT_LABELS_SQL
    assert "{kinds:Array(String)}" in sql and "INTERVAL 24 HOUR" in sql
    assert "used_cited" not in sql and "recall" not in sql
    assert set(AGENT_LABEL_EVENT_TYPES) == {"used", "acted", "harm"}


# ------------------------------------------------------------------ cardinality cap


def _bench_rows(n: int, *, org: str = "dev") -> list[tuple[str, str, str, int]]:
    # namespace i carries i `used` labels and one `harm`: totals are distinct
    return [(org, f"locomo-conv-{i:03d}", "used", i) for i in range(1, n + 1)] + [
        (org, f"locomo-conv-{i:03d}", "harm", 1) for i in range(1, n + 1)
    ]


def test_cap_keeps_the_top_namespaces_and_folds_the_rest_into_other() -> None:
    rows = _bench_rows(30) + [("clickhouse", "demo-full2", "used", 5)]
    series = otel_probes._agent_labels_series_from_rows(rows, max_namespaces=20)["agent_labels_24h"]
    named = {
        a["loom.namespace"]
        for _v, a in series
        if a["loom.namespace"] != otel_probes.OTHER_NAMESPACE
    }
    assert len(named) == 20
    # the top 20 by total: conv-030 (31) … conv-011 (12); demo (5) and
    # conv-001..010 (2..11) fold
    assert named == {f"locomo-conv-{i:03d}" for i in range(11, 31)}
    other = {
        (a["loom.org"], a["loom.kind"]): v
        for v, a in series
        if a["loom.namespace"] == otel_probes.OTHER_NAMESPACE
    }
    assert other == {
        ("dev", "used"): float(sum(range(1, 11))),
        ("dev", "harm"): 10.0,
        ("clickhouse", "used"): 5.0,
    }
    # the fleet total is exact: nothing lost in the fold
    assert sum(v for v, a in series if a["loom.kind"] == "used") == float(sum(range(1, 31)) + 5)
    assert len(series) == 20 * 2 + 3


def test_cap_is_off_at_zero_and_inert_under_the_limit() -> None:
    rows = _bench_rows(30)
    uncapped = otel_probes._agent_labels_series_from_rows(rows, max_namespaces=0)[
        "agent_labels_24h"
    ]
    assert len(uncapped) == 60 and not any(
        a["loom.namespace"] == otel_probes.OTHER_NAMESPACE for _v, a in uncapped
    )
    small = otel_probes._agent_labels_series_from_rows(_bench_rows(5), max_namespaces=20)[
        "agent_labels_24h"
    ]
    assert len(small) == 10 and not any(a["loom.namespace"] == "other" for _v, a in small)


def test_cap_order_is_deterministic_on_ties() -> None:
    rows = [("o", f"ns-{c}", "used", 7) for c in "zyxwv"]
    a = otel_probes._agent_labels_series_from_rows(rows, max_namespaces=2)["agent_labels_24h"]
    b = otel_probes._agent_labels_series_from_rows(list(reversed(rows)), max_namespaces=2)[
        "agent_labels_24h"
    ]
    assert a == b
    assert [x["loom.namespace"] for _v, x in a] == ["ns-v", "ns-w", otel_probes.OTHER_NAMESPACE]


def test_a_namespace_at_the_trigger_pace_is_counted_even_when_folded() -> None:
    """The phase-1 trigger (1,000 labels a month in one namespace, ~33 a day)
    is read from a count that needs no namespace label: a tenant out-ranked by
    200 busier bench namespaces is folded into `other` in the per-namespace
    series and still counted at pace for its org."""
    rows = _bench_rows(200) + [
        ("clickhouse", "prod", "acted", 40),
        ("clickhouse", "quiet", "used", 3),
    ]
    series = otel_probes._agent_labels_series_from_rows(rows, max_namespaces=20)
    named = {a["loom.namespace"] for _v, a in series["agent_labels_24h"]}
    assert "prod" not in named and otel_probes.OTHER_NAMESPACE in named
    assert otel_probes.WORTH_TRIGGER_LABELS_PER_DAY == pytest.approx(1000 / 30)
    # dev: conv-033 .. conv-200 carry >= 34 labels (i used + 1 harm >= 33.3)
    assert series["agent_labels_namespaces_at_pace"] == [
        (1.0, {"loom.org": "clickhouse"}),
        (float(sum(1 for i in range(1, 201) if i + 1 >= 1000 / 30)), {"loom.org": "dev"}),
    ]
    # an org whose namespaces are all under pace reads 0, not absent
    quiet = otel_probes._agent_labels_series_from_rows([("o", "n", "used", 5)])
    assert quiet["agent_labels_namespaces_at_pace"] == [(0.0, {"loom.org": "o"})]
    assert otel_probes._agent_labels_series_from_rows([]) == {
        "agent_labels_24h": [],
        "agent_labels_namespaces_at_pace": [],
    }


def test_probe_reads_the_cap_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "otel_agent_labels_max_namespaces", 2, raising=False)

    class _Res:
        result_rows = [("o", "a", "used", 9), ("o", "b", "used", 8), ("o", "c", "used", 7)]

    class _Sync:
        def query(self, sql: str, parameters: dict[str, Any]) -> _Res:
            assert parameters["kinds"] == ["used", "acted", "harm"]
            return _Res()

    monkeypatch.setattr(otel_probes, "_probe_client", lambda: _Sync())
    series = otel_probes._probe_agent_labels()["agent_labels_24h"]
    assert [a["loom.namespace"] for _v, a in series] == ["a", "b", otel_probes.OTHER_NAMESPACE]
    assert settings.otel_agent_labels_max_namespaces == 2


def test_default_cap_is_twenty() -> None:
    from server.settings import Settings

    assert Settings.model_fields["otel_agent_labels_max_namespaces"].default == 20


# ------------------------------------------------------------- storage gauges

NOW = dt.datetime(2026, 9, 15, 6, 45, 0, tzinfo=dt.UTC)


def test_storage_series_per_table_and_view() -> None:
    parts = [
        # table, parts, wide, patch, rows, marks, bytes
        ("memory_embeddings", 23, 5, 0, 936_852, 3_697, 12_563_000_000),
        ("memory_embeddings_q", 26, 3, 5, 669_531, 2_630, 8_053_000_000),
        ("memory_term_freq", 13, 0, 0, 5_656_237, 1_571, 143_000_000),
        ("session_summaries", 11, 0, 0, 99_752, 788, 705_000_000),
        ("empty_table", 0, 0, 0, 0, 0, 0),
    ]
    refreshes = [
        ("agent_cost_rollup_refresh", "Scheduled", NOW - dt.timedelta(seconds=45), ""),
        ("memory_exposure_breadth_hourly_refresh", "Scheduled", None, ""),
        ("broken_refresh", "Scheduled", NOW - dt.timedelta(hours=2), "Code: 60. Table missing"),
        ("waiting_refresh", "WaitingForDependencies", NOW.replace(tzinfo=None), ""),
    ]
    s = otel_probes._storage_series_from_rows(parts, refreshes, now=NOW)
    rpm = {a["loom.table"]: v for v, a in s["storage_rows_per_mark"]}
    assert rpm["memory_embeddings"] == pytest.approx(253.4, abs=0.1)
    assert rpm["memory_term_freq"] == pytest.approx(3600.4, abs=0.1)
    assert rpm["empty_table"] == 0.0  # no marks: 0, not a division error
    patch = {a["loom.table"]: v for v, a in s["storage_patch_parts"]}
    assert patch == {
        "memory_embeddings": 0.0,
        "memory_embeddings_q": 5.0,
        "memory_term_freq": 0.0,
        "session_summaries": 0.0,
        "empty_table": 0.0,
    }
    wide = {a["loom.table"]: v for v, a in s["storage_wide_parts"]}
    assert wide["session_summaries"] == 0.0 and wide["memory_embeddings"] == 5.0
    assert {a["loom.table"]: v for v, a in s["storage_bytes_on_disk"]}["memory_embeddings_q"] == (
        8_053_000_000.0
    )
    sched = {a["loom.view"]: v for v, a in s["storage_view_refresh_scheduled"]}
    assert sched == {
        "agent_cost_rollup_refresh": 1.0,
        "memory_exposure_breadth_hourly_refresh": 1.0,
        "broken_refresh": 0.0,
        "waiting_refresh": 0.0,
    }
    age = {a["loom.view"]: v for v, a in s["storage_view_refresh_age_s"]}
    assert age["agent_cost_rollup_refresh"] == 45.0
    assert age["broken_refresh"] == 7200.0
    assert age["waiting_refresh"] == 0.0  # a naive timestamp is read as UTC
    assert "memory_exposure_breadth_hourly_refresh" not in age  # never succeeded: no point


def test_storage_statements_read_metadata_only_over_a_closed_table_list() -> None:
    assert "FROM system.parts" in otel_probes._STORAGE_PARTS_SQL
    assert (
        "{tables:Array(String)}" in otel_probes._STORAGE_PARTS_SQL
        and "GROUP BY table" in otel_probes._STORAGE_PARTS_SQL
    )
    assert "loom.memory" not in otel_probes._STORAGE_PARTS_SQL  # no data table is scanned
    assert "FROM system.view_refreshes" in otel_probes._VIEW_REFRESH_SQL
    assert set(otel_probes.STORAGE_TABLES) >= {
        "memory_embeddings",
        "memory_evidence",
        "code_term_freq",
        "memory_term_freq",
        "session_summaries",
    }
    # the aliases never shadow a source column (the 26.8 analyzer rejects
    # `sum(rows) AS rows` inside another aggregate)
    for sql in (otel_probes._STORAGE_PARTS_SQL,):
        assert " AS rows" not in sql and " AS marks" not in sql


def test_storage_probe_keeps_the_parts_when_view_refreshes_is_refused(monkeypatch) -> None:
    class _Res:
        def __init__(self, rows: list[Any]) -> None:
            self.result_rows = rows

    class _Sync:
        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Res:
            if "system.view_refreshes" in sql:
                raise RuntimeError("Code: 497. Not enough privileges")
            assert parameters == {"tables": list(otel_probes.STORAGE_TABLES)}
            return _Res([("memory_embeddings", 23, 5, 0, 936_852, 3_697, 1)])

    monkeypatch.setattr(otel_probes, "_probe_client", lambda: _Sync())
    s = otel_probes._probe_storage()
    assert s["storage_patch_parts"] == [(0.0, {"loom.table": "memory_embeddings"})]
    assert "storage_view_refresh_scheduled" not in s


def test_storage_probe_reads_how_far_the_sli_rollup_trails(monkeypatch) -> None:
    class _Res:
        def __init__(self, rows: list[Any]) -> None:
            self.result_rows = rows

    class _Sync:
        def __init__(self, lag_rows: list[Any]) -> None:
            self.lag_rows = lag_rows

        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Res:
            if sql == otel_probes._SLI_MISSED_SQL:
                return _Res([(3,)])
            if "loom_telemetry.sli_minute" in sql:
                return _Res(self.lag_rows)
            return _Res([])

    monkeypatch.setattr(otel_probes, "_probe_client", lambda: _Sync([(3725.5,)]))
    series = otel_probes._probe_storage()
    assert series["sli_rollup_lag_s"] == [(3725.5, {})]
    assert series["sli_rollup_missed_rows"] == [(3.0, {})]
    # No sentinel yet (a rollup that never ran): no point, not a huge lag.
    monkeypatch.setattr(otel_probes, "_probe_client", lambda: _Sync([]))
    series = otel_probes._probe_storage()
    assert "sli_rollup_lag_s" not in series and "sli_rollup_missed_rows" not in series


def test_storage_gauges_registered_only_with_a_probe_and_attributed_by_table() -> None:
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict)
    assert not any(n.startswith("loom.ch.table.") for n in meter.gauges)

    series = {
        "storage_rows_per_mark": [(3600.4, {"loom.table": "memory_term_freq"})],
        "storage_view_refresh_scheduled": [(1.0, {"loom.view": "agent_cost_rollup_refresh"})],
    }
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict, storage_probe=lambda: series)
    for name in (
        "loom.ch.table.rows_per_mark",
        "loom.ch.table.active_parts",
        "loom.ch.table.wide_parts",
        "loom.ch.table.patch_parts",
        "loom.ch.table.bytes_on_disk",
        "loom.ch.view_refresh.scheduled",
        "loom.ch.view_refresh.age_s",
        "loom.sli.rollup_lag_s",
        "loom.sli.rollup_missed_rows",
    ):
        assert name in meter.gauges, name
    assert "index_granularity = 256" in meter.descriptions["loom.ch.table.rows_per_mark"]
    # stale-while-revalidate: empty before the first refresh lands, never 0
    obs: list[Any] = meter.gauges["loom.ch.table.rows_per_mark"](None)
    assert obs == []
    deadline = time.monotonic() + 15.0
    while not obs and time.monotonic() < deadline:
        time.sleep(0.02)
        obs = meter.gauges["loom.ch.table.rows_per_mark"](None)
    (o,) = obs
    assert o.value == 3600.4 and o.attributes == {"loom.table": "memory_term_freq"}
    # a series the probe did not return stays silent (no observation)
    assert meter.gauges["loom.ch.table.patch_parts"](None) == []


def test_at_pace_gauge_is_registered_beside_the_label_gauge() -> None:
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict, agent_labels_probe=dict)
    assert "loom.memory.agent_labels_namespaces_at_pace" in meter.gauges
    assert (
        "LOOM_OTEL_AGENT_LABELS_MAX_NAMESPACES"
        in meter.descriptions["loom.memory.agent_labels_24h"]
    )
    meter = _Meter()
    metrics_gauges.register_state_gauges(meter, ch_probe=dict)
    assert "loom.memory.agent_labels_namespaces_at_pace" not in meter.gauges


def test_state_gauge_probes_name_every_probe_once() -> None:
    probes = otel_probes._state_gauge_probes()
    assert set(probes) == {
        "ch_probe",
        "code_probe",
        "retention_probe",
        "agent_labels_probe",
        "storage_probe",
        "agent_trusts_probe",
    }
    # The censuses are wrapped to run where the lease is held.
    assert probes["storage_probe"].__wrapped__ is otel_probes._probe_storage
    assert probes["agent_labels_probe"].__wrapped__ is otel_probes._probe_agent_labels
    assert probes["agent_trusts_probe"].__wrapped__ is otel_probes._probe_agent_trusts
    # every key is a parameter of the registrar
    import inspect

    params = inspect.signature(metrics_gauges.register_state_gauges).parameters
    assert set(probes) <= set(params)


def test_ttl_cache_never_fetched_is_due_on_a_young_host(monkeypatch) -> None:
    """monotonic() below the TTL must not stop the
    first refresh (a fresh container; a 15-minute cache)."""
    calls: list[int] = []
    cache = metrics_gauges._TTLCache(900.0, lambda: calls.append(1) or {"k": 1})
    monkeypatch.setattr(metrics_gauges.time, "monotonic", lambda: 5.0)
    assert cache.get() is None  # first call starts the refresh
    deadline = time.time() + 5.0
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert calls == [1]
