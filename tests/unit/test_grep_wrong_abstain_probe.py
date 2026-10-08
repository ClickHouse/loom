"""code.grep wrong-abstain gate: the pure fold, the needle
batching, the scoped recount and its padding, the behind-schema ledger
fallback, the gauge series and its slow clock, and the bench's report."""

from __future__ import annotations

from typing import Any

import pytest

from server.observability import grep_replay_probe as gp


class _Res:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.result_rows = rows
        self.column_names: list[str] = []


class _CH:
    """Sync stub: the ledger statement answers `abstains`, every recount
    answers from `hits` ((pattern, prefix) -> chunks)."""

    def __init__(
        self,
        abstains: list[tuple[Any, ...]],
        hits: dict[tuple[str, str], int],
    ) -> None:
        self.abstains = abstains
        self.hits = hits
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Res:
        self.calls.append((sql, dict(parameters or {})))
        if "code_search_trace" in sql:
            return _Res(self.abstains)
        pairs = list(zip(parameters["pats"], parameters["prefixes"], strict=True))
        return _Res([([self.hits.get(pair, 0) for pair in pairs], 1000)])


def test_batches_are_distinct_ordered_pairs_capped_at_255() -> None:
    pairs = [(f"p{i}", "") for i in range(300)] + [("p0", ""), ("", ""), ("p1", ""), ("p0", "src/")]
    out = gp.batches(pairs)
    assert [len(b) for b in out] == [255, 46], "p0 under src/ is a different pair; '' is dropped"
    assert out[0][0] == ("p0", "") and out[1][-1] == ("p0", "src/")
    assert gp.batches([]) == []


def test_recount_is_one_scan_per_255_pairs_with_aligned_prefixes_and_pads_an_empty_namespace() -> (
    None
):
    ch = _CH(
        [],
        {
            ("Alias", ""): 1881,
            ("Alias", "src/Storages/StorageFactory.cpp"): 0,
            ("isSerializable", ""): 97,
        },
    )
    out = gp.recount(
        ch,
        "o",
        "n",
        [
            ("Alias", ""),
            ("Alias", "src/Storages/StorageFactory.cpp"),
            ("zzz", ""),
            ("isSerializable", ""),
        ],
    )
    assert out == {
        ("Alias", ""): 1881,
        ("Alias", "src/Storages/StorageFactory.cpp"): 0,
        ("zzz", ""): 0,
        ("isSerializable", ""): 97,
    }
    assert len(ch.calls) == 1
    assert ch.calls[0][1] == {
        "org": "o",
        "ns": "n",
        "pats": ["Alias", "Alias", "zzz", "isSerializable"],
        "prefixes": ["", "src/Storages/StorageFactory.cpp", "", ""],
    }
    ch2 = _CH([], {})
    gp.recount(ch2, "o", "n", [(f"p{i}", "") for i in range(300)])
    assert len(ch2.calls) == 2, "300 distinct pairs → two scans"

    class _Empty:
        def query(self, sql: str, parameters: dict[str, Any] | None = None) -> _Res:
            return _Res([])  # sumForEach over no rows: no row at all

    assert gp.recount(_Empty(), "o", "empty", [("a", ""), ("b", "x/")]) == {
        ("a", ""): 0,
        ("b", "x/"): 0,
    }


def test_fold_counts_wrong_calls_in_scope_per_consumer_and_keeps_a_clean_consumer_at_zero() -> None:
    abstains = [
        (
            "o",
            "n",
            "clickgap",
            "Alias",
            "src/Storages/StorageFactory.cpp",
            1,
        ),  # scoped: correct no-match
        ("o", "n", "clickgap", "Alias", "", 1),  # unscoped: wrong
        ("o", "n", "clickgap", "KeyCondition", "", 2),
        ("o", "n", "clickgap", "Cannot convert string  to type DateTime64(, )", "", 3),
        ("o", "n", "bench", "nothing_here", "", 4),
        ("o", "n2", "clickgap", "Alias", "", 1),
    ]
    corpus = {
        ("o", "n"): {
            ("Alias", "src/Storages/StorageFactory.cpp"): 0,
            ("Alias", ""): 1881,
            ("KeyCondition", ""): 360,
        },
        ("o", "n2"): {("Alias", ""): 0},
    }
    out = gp.fold(abstains, corpus)
    by = {(r["org"], r["namespace"], r["consumer"]): r for r in out}
    cg = by[("o", "n", "clickgap")]
    assert cg["abstain_calls"] == 7 and cg["wrong_calls"] == 3
    assert [(p["pattern"], p["path_prefix"]) for p in cg["wrong_patterns"]] == [
        ("Alias", ""),
        ("KeyCondition", ""),
    ]
    assert (
        by[("o", "n", "bench")]["wrong_calls"] == 0
        and by[("o", "n", "bench")]["abstain_calls"] == 4
    )
    assert by[("o", "n2", "clickgap")]["wrong_calls"] == 0  # recount said absent there
    s = gp.series(out)["code_grep_wrong_abstain_24h"]
    assert (3.0, {"loom.org": "o", "loom.namespace": "n", "loom.consumer": "clickgap"}) in s
    assert (0.0, {"loom.org": "o", "loom.namespace": "n", "loom.consumer": "bench"}) in s


def test_wrong_abstains_reads_the_ledger_once_and_recounts_once_per_namespace() -> None:
    ch = _CH(
        [
            ("o", "n", "clickgap", "Alias", "", 1),
            ("o", "n", "clickgap", "zzz", "", 1),
            ("o", "n2", "x", "Alias", "src/", 1),
        ],
        {("Alias", ""): 5, ("Alias", "src/"): 2},
    )
    out = gp.wrong_abstains(ch, hours=24)
    ledger = [c for c in ch.calls if "code_search_trace" in c[0]]
    recounts = [c for c in ch.calls if "multiSearchAllPositions" in c[0]]
    assert len(ledger) == 1 and ledger[0][1] == {"hours": 24} and "path_prefix" in ledger[0][0]
    assert sorted(c[1]["ns"] for c in recounts) == ["n", "n2"]
    assert {r["consumer"]: r["wrong_calls"] for r in out} == {"clickgap": 1, "x": 1}


def test_a_ledger_read_error_raises() -> None:
    class _Other:
        def query(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("Code: 497. Not enough privileges")

    with pytest.raises(RuntimeError):
        gp.ledger_abstains(_Other())


def test_probe_never_raises() -> None:
    class _Boom:
        def query(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("grant refused")

    assert gp.probe(_Boom()) == {}


def test_gauge_runs_on_its_own_clock_and_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import otel_probes
    from server.settings import settings

    calls: list[int] = []
    monkeypatch.setattr(
        gp,
        "probe",
        lambda client, hours=24: (
            calls.append(hours),
            {"code_grep_wrong_abstain_24h": [(1.0, {})]},
        )[1],
    )
    monkeypatch.setattr(otel_probes, "_GREP_REPLAY_CACHE", {"at": 0.0, "series": {}})
    monkeypatch.setattr(settings, "otel_code_grep_replay_cache_s", 3600, raising=False)
    assert otel_probes._grep_wrong_abstain_series(object(), 1000.0) == {
        "code_grep_wrong_abstain_24h": [(1.0, {})]
    }
    assert otel_probes._grep_wrong_abstain_series(object(), 1000.0 + 3599) == {
        "code_grep_wrong_abstain_24h": [(1.0, {})]
    }
    assert calls == [24], "inside the ttl the cached series is served"
    otel_probes._grep_wrong_abstain_series(object(), 1000.0 + 3600)
    assert calls == [24, 24]
    monkeypatch.setattr(settings, "otel_code_grep_replay_cache_s", 0, raising=False)
    assert otel_probes._grep_wrong_abstain_series(object(), 99999.0) == {}
    assert calls == [24, 24], "0 = off: no probe call"


def test_gauge_is_registered_with_the_probe_series(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import metrics_gauges

    names: dict[str, Any] = {}

    class _Meter:
        def create_observable_gauge(
            self, *, name: str, callbacks: Any, description: str = "", unit: str = ""
        ) -> None:
            names[name] = callbacks[0]

        def create_counter(self, *a: Any, **k: Any) -> Any:  # pragma: no cover - not used here
            return None

    attrs = {"loom.org": "o", "loom.namespace": "n", "loom.consumer": "c"}
    metrics_gauges.register_state_gauges(
        _Meter(), ch_probe=dict, code_probe=lambda: {"code_grep_wrong_abstain_24h": [(3.0, attrs)]}
    )
    cb = names["loom.code.grep.wrong_abstain_24h"]
    # the TTL cache produces in a background thread: the first read is empty
    import time

    obs: Any = []
    for _ in range(50):
        obs = cb(None)
        if obs:
            break
        time.sleep(0.02)
    assert [(o.value, dict(o.attributes)) for o in obs] == [(3.0, attrs)]


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "ts": "t",
        "org": "o",
        "namespace": "n",
        "consumer": "clickgap",
        "route": "grep_literal",
        "top_k": 200,
        "result_count": 0,
        "abstain": 1,
        "latency_ms": 30,
        "returned_paths": [],
        "mode": "matches",
        "chunks_matched": 0,
        "path_prefix": "",
    }
    return {**base, **kw}


def test_bench_report_folds_ledger_rows_in_scope_and_flags_replayed_wrong_abstains() -> None:
    from bench import code_grep_replay as b

    rows = [
        _row(
            pattern="Alias",
            path_prefix="src/Storages/StorageFactory.cpp",
            route="grep_literal+prefix",
        ),
        _row(pattern="Alias"),
        _row(pattern="Cannot convert", latency_ms=90),
        _row(
            pattern="toAST",
            result_count=3,
            abstain=0,
            latency_ms=100,
            returned_paths=["a.cpp", "b.cpp"],
            chunks_matched=9,
        ),
        _row(
            pattern="toAST",
            result_count=1,
            abstain=0,
            returned_paths=["a.cpp"],
            path_prefix="src/a",
            route="grep_literal+prefix",
        ),
    ]
    corpus = {
        ("o", "n"): {
            ("Alias", "src/Storages/StorageFactory.cpp"): 0,
            ("Alias", ""): 1881,
            ("Cannot convert", ""): 0,
            ("toAST", ""): 134,
            ("toAST", "src/a"): 1,
        }
    }
    replayed = {
        ("o", "n", "Alias", "", "matches", "200"): {
            "row": rows[1],
            "served": [0, 200, 200],
            "chunks": [0, 200, 200],
            "ms": [20.0, 110.0, 120.0],
            "errors": 0,
            "files": ["x"],
        },
        ("o", "n", "toAST", "", "matches", "200"): {
            "row": rows[3],
            "served": [3],
            "chunks": [9],
            "ms": [100.0],
            "errors": 0,
            "files": ["a.cpp", "b.cpp", "c.cpp"],
        },
        ("o", "n", "toAST", "src/a", "matches", "200"): {
            "row": rows[4],
            "served": [1],
            "chunks": [1],
            "ms": [50.0],
            "errors": 0,
            "files": ["a.cpp"],
        },
    }
    rep = b.report(rows, corpus, replayed)
    cons = rep["consumers"][0]
    assert cons["calls"] == 5 and cons["abstain_calls"] == 3 and cons["wrong_calls"] == 1
    assert cons["wrong_patterns"] == [
        {"pattern": "Alias", "path_prefix": "", "calls": 1, "corpus": 1881}
    ]
    by = {(d["pattern"], d["path_prefix"]): d for d in rep["calls"]}
    assert not by[("Alias", "src/Storages/StorageFactory.cpp")]["wrong_abstain"], (
        "correct scoped no-match"
    )
    assert by[("Alias", "")]["wrong_abstain"] and by[("Alias", "")]["wrong_now"]
    assert by[("Alias", "")]["served_now"] == [0, 200]
    assert not by[("Cannot convert", "")]["wrong_abstain"]
    assert (
        by[("toAST", "")]["files_identical"] is False and by[("toAST", "")]["files_covered"] is True
    )
    assert by[("toAST", "src/a")]["files_identical"] is True
    assert rep["replay"]["wrong_now"] == 1
    assert rep["replay"]["success_files_identical"] == [1, 2] and rep["replay"][
        "success_files_covered"
    ] == [2, 2]
    # the replayed call carries the ledger's scope and limit
    assert b._call_body(rows[0]) == {
        "org": "o",
        "namespace": "n",
        "pattern": "Alias",
        "mode": "matches",
        "consumer": "grep-replay",
        "path_prefix": "src/Storages/StorageFactory.cpp",
        "limit": 200,
    }
    # the FILE list the trace writer records: distinct paths, first-seen order, ≤ 50
    assert b.served_files(
        {"matches": [{"path": "b"}, {"path": "a"}, {"path": "b"}]}, "matches"
    ) == ["b", "a"]
    assert b.served_files({"files": [{"path": "f"}]}, "files") == ["f"]


def test_series_bounds_consumer_labels_per_namespace() -> None:
    """`consumer` is self-reported: a flood of distinct tags must not become a
    flood of gauge series. The worst SERIES_MAX_CONSUMERS keep their names,
    the rest sum under OTHER_CONSUMER, and no wrong call is lost."""
    abstains = [("o", "n", f"tag-{i:03d}", "Alias", "", i + 1) for i in range(200)]
    abstains.append(("o", "n2", "clickgap", "Alias", "", 2))
    out = gp.fold(abstains, {("o", "n"): {("Alias", ""): 5}, ("o", "n2"): {("Alias", ""): 5}})
    s = gp.series(out)["code_grep_wrong_abstain_24h"]
    labels = [a["loom.consumer"] for _, a in s if a["loom.namespace"] == "n"]
    assert len(labels) == gp.SERIES_MAX_CONSUMERS + 1
    assert labels[: gp.SERIES_MAX_CONSUMERS] == [f"tag-{i:03d}" for i in range(199, 191, -1)]
    assert labels[-1] == gp.OTHER_CONSUMER
    assert sum(v for v, a in s if a["loom.namespace"] == "n") == sum(range(1, 201))
    # another namespace keeps its own budget
    assert (2.0, {"loom.org": "o", "loom.namespace": "n2", "loom.consumer": "clickgap"}) in s
