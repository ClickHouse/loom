"""A code vector table is compacted when its copies
outnumber its keys. Ratio math and threshold, dry run never OPTIMIZEs, the
table allowlist, the merge poll's two exits, and the bulk hook's row
threshold and error swallowing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.code.app.index import compaction as cp
from server.core.vectors import code_tables as vt
from server.settings import settings


class _CH:
    """Fake CH: system.parts answers from a list of (parts, rows) readings
    (the last one repeats), uniq answers `keys`, merges from a list."""

    def __init__(
        self,
        *,
        parts: list[tuple[int, int]],
        keys: int,
        merges: list[int] | None = None,
        fail_cluster: bool = False,
        fail_optimize: bool = False,
    ) -> None:
        self.parts = list(parts)
        self.keys = keys
        self.merges = list(merges or [0])
        self.fail_cluster = fail_cluster
        self.fail_optimize = fail_optimize
        self.executed: list[str] = []
        self.queries: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.queries.append(sql)
        if "system.parts" in sql:
            p, r = self.parts.pop(0) if len(self.parts) > 1 else self.parts[0]
            return {"parts": p, "rows": r}
        if "uniq(" in sql:
            return {"keys": self.keys}
        if "system.merges" in sql:
            if "clusterAllReplicas" in sql and self.fail_cluster:
                raise RuntimeError("no cluster default")
            n = self.merges.pop(0) if len(self.merges) > 1 else self.merges[0]
            return {"n": n}
        raise AssertionError(sql)

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.executed.append(sql)
        if self.fail_optimize:
            raise RuntimeError("boom")


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


@pytest.fixture(autouse=True)
def _no_mapping(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "code_vector_tables", "")
    vt.reset()
    yield
    vt.reset()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_ratio_is_physical_rows_over_keys() -> None:
    ch = _CH(parts=[(12, 536_000)], keys=257_000)
    res = _run(cp.copies_per_key(ch, "code_chunks_prefix"))
    assert res.table == "loom.code_chunks_prefix"
    assert (res.parts_before, res.rows_before, res.keys) == (12, 536_000, 257_000)
    assert res.ratio == round(536_000 / 257_000, 3)
    # the key read names only the key tuple, never a vector
    uniq_sql = next(q for q in ch.queries if "uniq(" in q)
    assert "org, namespace, embedding_model, path, chunk_id" in uniq_sql
    assert "embedding," not in uniq_sql


def test_empty_table_has_ratio_zero_and_skips_the_key_read() -> None:
    ch = _CH(parts=[(0, 0)], keys=0)
    res = _run(cp.maybe_compact(ch, "code_chunks_prefix"))
    assert res.ratio == 0.0 and not res.ran and ch.executed == []
    assert not any("uniq(" in q for q in ch.queries)


def test_below_threshold_does_not_optimize() -> None:
    ch = _CH(parts=[(3, 140)], keys=100)
    res = _run(cp.maybe_compact(ch, "code_chunks_prefix", min_ratio=1.5))
    assert not res.ran and ch.executed == [] and "<" in res.reason


def test_dry_run_never_issues_optimize() -> None:
    ch = _CH(parts=[(12, 400)], keys=100)
    res = _run(cp.maybe_compact(ch, "code_chunks", min_ratio=1.5, dry_run=True))
    assert res.ratio == 4.0 and not res.ran and ch.executed == []
    assert "dry run" in res.reason


def test_at_threshold_optimizes_and_waits_for_the_merge() -> None:
    clock = _Clock()
    ch = _CH(parts=[(12, 150), (12, 150), (2, 100)], keys=100, merges=[1, 0])
    res = _run(
        cp.maybe_compact(
            ch,
            "loom.code_chunks_prefix",
            min_ratio=1.5,
            poll_s=15,
            timeout_s=600,
            sleep=clock.sleep,
            clock=clock,
        )
    )
    assert ch.executed == ["OPTIMIZE TABLE loom.code_chunks_prefix FINAL"]
    assert res.ran and not res.timed_out and res.reason == "compacted"
    assert (res.parts_after, res.rows_after, res.ratio_after) == (2, 100, 1.0)
    assert res.waited_s == 30.0
    assert any("clusterAllReplicas('default', system.merges)" in q for q in ch.queries)


def test_allowlist_rejects_any_other_table() -> None:
    for bad in ("memory", "loom.memory", "code_chunks; DROP TABLE x", "", "other.code_chunks"):
        with pytest.raises(ValueError):
            cp.resolve_table(bad)
    ch = _CH(parts=[(1, 1)], keys=1)
    with pytest.raises(ValueError):
        _run(cp.maybe_compact(ch, "loom.memory"))
    assert ch.queries == [] and ch.executed == []


def test_allowlist_admits_a_mapped_vector_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "code_vector_tables",
        '{"static:potion": {"table": "loom.code_chunks_potion", "dim": 256}}',
    )
    vt.reset()
    assert cp.resolve_table("code_chunks_potion") == "loom.code_chunks_potion"
    assert cp.resolve_table("code_chunks") == "loom.code_chunks"


def test_poll_stops_when_no_merge_runs_and_parts_dropped() -> None:
    clock = _Clock()
    # merge still running at first poll even though parts dropped; done at the second
    ch = _CH(parts=[(4, 100), (2, 60)], keys=50, merges=[2, 0])
    parts, rows, waited, timed_out = _run(
        cp.wait_for_merge(
            ch,
            "loom.code_chunks_prefix",
            parts_before=12,
            rows_before=100,
            timeout_s=600,
            poll_s=15,
            sleep=clock.sleep,
            clock=clock,
        )
    )
    assert (parts, rows, waited, timed_out) == (2, 60, 30.0, False)


def test_poll_times_out_when_parts_never_drop() -> None:
    clock = _Clock()
    ch = _CH(parts=[(12, 100)], keys=50, merges=[0])
    parts, _rows, waited, timed_out = _run(
        cp.wait_for_merge(
            ch,
            "loom.code_chunks_prefix",
            parts_before=12,
            rows_before=100,
            timeout_s=60,
            poll_s=15,
            sleep=clock.sleep,
            clock=clock,
        )
    )
    assert timed_out and parts == 12 and waited == 60.0


def test_poll_falls_back_to_local_merges_without_a_cluster() -> None:
    clock = _Clock()
    ch = _CH(parts=[(1, 50)], keys=50, merges=[0], fail_cluster=True)
    _p, _r, _w, timed_out = _run(
        cp.wait_for_merge(
            ch,
            "loom.code_chunks",
            parts_before=3,
            rows_before=90,
            timeout_s=60,
            poll_s=15,
            sleep=clock.sleep,
            clock=clock,
        )
    )
    assert not timed_out
    assert any(q.startswith("SELECT count() AS n FROM system.merges") for q in ch.queries)


def test_optimize_failure_is_reported_not_raised() -> None:
    ch = _CH(parts=[(12, 300)], keys=100, fail_optimize=True)
    res = _run(cp.maybe_compact(ch, "code_chunks_prefix"))
    assert not res.ran and "boom" in res.error


def test_bulk_tables_never_include_code_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cp.bulk_tables("text-embedding-3-large") == ["loom.code_chunks_prefix"]
    monkeypatch.setattr(
        settings,
        "code_vector_tables",
        (
            '{"prefix:512:text-embedding-3-large": {"table": "loom.code_chunks_prefix", "dim": 512},'
            ' "static:potion": {"table": "loom.code_chunks_potion", "dim": 256}}'
        ),
    )
    vt.reset()
    assert cp.bulk_tables("text-embedding-3-large") == ["loom.code_chunks_prefix"]
    assert cp.bulk_tables("static:potion") == ["loom.code_chunks_prefix", "loom.code_chunks_potion"]


def test_bulk_hook_fires_only_at_or_above_the_row_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_compact_after_bulk_rows", 10_000)
    monkeypatch.setattr(settings, "code_compact_min_ratio", 1.5)
    clock = _Clock()
    kw = {"sleep": clock.sleep, "clock": clock, "poll_s": 15, "timeout_s": 60}

    ch = _CH(parts=[(12, 300)], keys=100)
    assert _run(cp.compact_after_bulk(ch, 9_999, "text-embedding-3-large", **kw)) == []
    assert ch.queries == [] and ch.executed == []

    ch = _CH(parts=[(12, 300), (12, 300), (2, 100)], keys=100, merges=[0])
    out = _run(cp.compact_after_bulk(ch, 10_000, "text-embedding-3-large", **kw))
    assert [r.table for r in out] == ["loom.code_chunks_prefix"] and out[0].ran
    assert ch.executed == ["OPTIMIZE TABLE loom.code_chunks_prefix FINAL"]


def test_bulk_hook_off_at_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_compact_after_bulk_rows", 0)
    ch = _CH(parts=[(12, 300)], keys=100)
    assert _run(cp.compact_after_bulk(ch, 1_000_000, "m")) == []
    assert ch.queries == []


def test_bulk_hook_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_compact_after_bulk_rows", 10)

    class _Broken:
        async def query_one(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("ch down")

        async def execute(self, *a: Any, **k: Any) -> None:
            raise AssertionError("never")

    assert _run(cp.compact_after_bulk(_Broken(), 100, "m")) == []


def test_cli_hook_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import code_maintenance

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("no client")

    monkeypatch.setattr(cp, "compact_after_bulk", _boom)
    _run(code_maintenance._compact_after_bulk(50_000, "m"))  # no exception


def test_cli_compact_is_registered_and_targets_resolve() -> None:
    import argparse

    from server.cli import code, code_maintenance

    parser = argparse.ArgumentParser()
    code.register_code_cli(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["code", "compact", "--dry-run"])
    assert args.func is code._cmd_compact and args.table == "code_chunks_prefix"
    assert args.dry_run and args.min_ratio is None
    assert code_maintenance.compact_targets("code_chunks_prefix") == ["loom.code_chunks_prefix"]
    assert code_maintenance.compact_targets("all") == [
        "loom.code_chunks_prefix",
        "loom.code_chunks",
    ]
    with pytest.raises(ValueError):
        code_maintenance.compact_targets("memory")
