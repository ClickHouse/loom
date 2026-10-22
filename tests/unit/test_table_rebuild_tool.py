"""The operator rebuild tool: its pure parts (batching, the delta set, the gate,
the next step) and its runs against a fake ClickHouse (confirmation, the twin
as the lock, postconditions, signals over the exchange)."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from scripts.ops import table_rebuild as tool
from server.core.schema import table_rebuild as tr


def test_batches_cover_a_part_in_offset_ranges() -> None:
    assert tool.batches(10, 4) == [(0, 4), (4, 8), (8, 10)]
    assert tool.batches(4, 4) == [(0, 4)]
    assert tool.batches(0, 4) == [(0, 0)]


def test_the_delta_is_every_part_not_copied_yet() -> None:
    # a merge or an insert makes a new part name; a copied part that is gone is irrelevant
    now = {"all_1_1_0": 5, "all_1_3_1": 9, "all_4_4_0": 1}
    assert tool.new_parts(now, {"all_1_1_0", "all_2_2_0"}) == {"all_1_3_1": 9, "all_4_4_0": 1}


def test_the_sample_gate_skips_keys_written_after_the_pass() -> None:
    live = [(("a",), 5, 1), (("b",), 12, 2)]
    twin = [(("a",), 5, 1), (("b",), 7, 9)]
    assert tool.compare_sample(live, twin, in_flight_above=10) == []
    assert tool.compare_sample(live, twin, in_flight_above=12) == [("b",)]


def test_the_sample_gate_flags_missing_extra_and_changed_rows() -> None:
    live = [(("a",), 5, 1), (("b",), 6, 2)]
    assert tool.compare_sample(live, [(("a",), 5, 1)], 10) == [("b",)]
    assert tool.compare_sample(live, [*live, (("c",), 1, 1)], 10) == [("c",)]
    assert tool.compare_sample(live, [(("a",), 5, 3), (("b",), 6, 2)], 10) == [("a",)]


def test_a_key_twice_on_one_side_is_a_difference_in_what_merges() -> None:
    # the old table's partitions kept two rows of one key that the twin merged
    live = [(("a",), 5, 1), (("a",), 4, 7)]
    assert tool.compare_sample(live, [(("a",), 5, 1)], 10) == [("a",)]


def test_counts_agree_only_within_the_rows_written_since_the_pass() -> None:
    assert tool.counts_agree(100, 100, 0)
    assert tool.counts_agree(103, 100, 3)
    assert not tool.counts_agree(104, 100, 3)
    assert not tool.counts_agree(99, 100, 5)  # the twin holds a key the live table does not


# -- the tool against a fake ClickHouse --------------------------------------


OLD = tr.Layout("org, namespace, name", "")
NEW = tr.Layout("org, namespace, path, facet_kind, name, ordinal", "")
COLS = [("org", "String", "", "", ""), ("version", "UInt64", "", "", "")]


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self.result_rows = rows


class _CH:
    """The system-table reads and the statements the tool issues, over a dict
    of tables: name -> {layout, rows, engine, uuid}."""

    def __init__(self, **tables: dict[str, Any]) -> None:
        self.t = tables
        self.commands: list[str] = []
        self.fail_create = False
        self.uuids = iter(f"u{i}" for i in range(100))

    def query(self, sql: str, parameters: Any = None, settings: Any = None) -> _Result:
        p = parameters or {}
        name = str(p.get("t", ""))
        if "system.disks" in sql:
            return _Result([(1e15,)])
        if "FROM system.tables" in sql:
            row = self.t.get(name)
            if row is None:
                return _Result([])
            if "engine" in sql:
                return _Result([(row["engine"], row["uuid"], "2000-01-01 00:00:00")])
            return _Result([(row["layout"].sorting_key, row["layout"].partition_key)])
        if "system.columns" in sql:
            return _Result(COLS)
        if "hostName()" in sql:
            return _Result([("26.8", "ch-0")])
        if "system.replicas" in sql or "startsWith(name, 'patch')" in sql:
            return _Result([])
        if "system.parts" in sql:
            return _Result([(1024,)])
        if "system.mutations" in sql or "WHERE version >" in sql:
            return _Result([(0,)])
        if sql == "SELECT now()":
            return _Result([("2000-01-01 00:00:00",)])
        table = sql.split("FROM loom.", 1)[1].split()[0]
        rows = self.t[table]["rows"]
        if "GROUP BY _part" in sql:
            return _Result([(f"{table}_part", rows)] if rows else [])
        if "max(version)" in sql:
            return _Result([(rows,)])
        if "cityHash64(" in sql:  # the sample
            return _Result([])
        return _Result([(rows,)])

    def command(self, stmt: str, parameters: Any = None, settings: Any = None) -> None:
        s = " ".join(stmt.split())
        self.commands.append(s)
        words = s.replace("loom.", "").split()
        if s.startswith("CREATE TABLE"):
            if self.fail_create:
                raise RuntimeError("Code: 57. Table already exists")
            self.t[words[2]] = {
                "layout": NEW,
                "rows": 0,
                "engine": "ReplacingMergeTree",
                "uuid": next(self.uuids),
            }
        elif s.startswith("DROP TABLE"):
            self.t.pop(words[2])
        elif s.startswith("EXCHANGE TABLES"):
            a, b = words[2], words[4]
            self.t[a], self.t[b] = self.t[b], self.t[a]
        elif s.startswith("RENAME TABLE"):
            self.t[words[4]] = self.t.pop(words[2])
        elif s.startswith("INSERT INTO"):
            dst, src = words[2], s.split("FROM loom.", 1)[1].split()[0]
            self.t[dst]["rows"] = max(self.t[dst]["rows"], self.t[src]["rows"])


def _table(layout: tr.Layout, rows: int, engine: str = "ReplacingMergeTree") -> dict[str, Any]:
    return {"layout": layout, "rows": rows, "engine": engine, "uuid": "live"}


# The sort-key rebuild the baseline has folded in, kept as a declaring journal.
REBUILD_FIXTURE = str(Path(__file__).resolve().parent / "fixtures" / "rebuild_migrations")


def _run(ch: _CH, *flags: str) -> int:
    return tool.main(
        [
            "--table",
            "code_test_facets",
            "--settle-s",
            "0",
            "--migrations-dir",
            REBUILD_FIXTURE,
            *flags,
        ],
        ch=ch,
    )


def _writes(ch: _CH) -> list[str]:
    return [c for c in ch.commands if not c.startswith("SYSTEM")]


def test_without_yes_a_rebuild_prints_its_plan_and_changes_nothing(capsys: Any) -> None:
    ch = _CH(code_test_facets=_table(OLD, 5))
    assert _run(ch) == tool.UNCONFIRMED
    assert ch.commands == []
    out = capsys.readouterr().out
    assert "ClickHouse " in out and "pass --yes" in out and '"exit": 4' in out


def test_the_dry_run_prints_the_twin_ddl_and_the_next_step(capsys: Any) -> None:
    ch = _CH(code_test_facets=_table(OLD, 5))
    assert _run(ch, "--dry-run") == tool.OK and ch.commands == []
    out = capsys.readouterr().out
    assert "CREATE TABLE loom.code_test_facets_rebuild" in out and "--yes` runs it" in out


def test_a_confirmed_rebuild_copies_swaps_and_checks_the_result() -> None:
    ch = _CH(code_test_facets=_table(OLD, 5))
    assert _run(ch, "--yes") == tool.OK
    assert ch.t["code_test_facets"]["layout"] == NEW and ch.t["code_test_facets"]["rows"] == 5
    assert ch.t["code_test_facets_pre_rebuild"]["layout"] == OLD
    assert "code_test_facets_rebuild" not in ch.t
    kinds = [c.split()[0] for c in _writes(ch)]
    assert kinds[0] == "CREATE" and kinds.count("EXCHANGE") == 1 and "RENAME" in kinds


def test_a_twin_left_behind_is_a_lock_until_restart(capsys: Any) -> None:
    ch = _CH(code_test_facets=_table(OLD, 5), code_test_facets_rebuild=_table(NEW, 0))
    assert _run(ch, "--yes") == tool.REFUSED and ch.commands == []
    assert "another run's lock" in capsys.readouterr().out
    assert _run(ch, "--yes", "--restart") == tool.OK
    assert ch.commands[0] == "DROP TABLE loom.code_test_facets_rebuild SYNC"


def test_a_lost_create_race_refuses_before_any_copy() -> None:
    ch = _CH(code_test_facets=_table(OLD, 5))
    ch.fail_create = True
    assert _run(ch, "--yes") == tool.REFUSED
    assert not any(c.startswith(("INSERT", "EXCHANGE")) for c in ch.commands)


def test_a_twin_replaced_by_another_run_is_never_swapped() -> None:
    ch = _CH(code_test_facets=_table(OLD, 5))
    real = tool.Rebuild.verified

    def verified(job: tool.Rebuild) -> bool:
        ch.t["code_test_facets_rebuild"]["uuid"] = "someone-else"
        return real(job)

    tool.Rebuild.verified = verified  # type: ignore[method-assign]
    try:
        assert _run(ch, "--yes") == tool.REFUSED
    finally:
        tool.Rebuild.verified = real  # type: ignore[method-assign]
    assert not any(c.startswith("EXCHANGE") for c in ch.commands)


def test_preflight_refuses_an_engine_the_gate_cannot_check() -> None:
    ch = _CH(code_test_facets=_table(OLD, 5, engine="MergeTree"))
    assert _run(ch, "--yes") == tool.REFUSED and ch.commands == []


def test_rollback_and_drop_need_yes_and_check_their_result() -> None:
    ch = _CH(code_test_facets=_table(NEW, 5), code_test_facets_pre_rebuild=_table(OLD, 5))
    assert _run(ch, "--rollback") == tool.UNCONFIRMED and ch.commands == []
    assert _run(ch, "--rollback", "--yes") == tool.OK
    assert ch.t["code_test_facets"]["layout"] == OLD
    assert _run(ch, "--drop-pre-rebuild") == tool.UNCONFIRMED
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tool.OK
    assert "code_test_facets_pre_rebuild" not in ch.t


def _next(out: str) -> str:
    return str(json.loads(out.rsplit("summary ", 1)[1])["next"])


def test_a_successful_rebuild_names_its_next_step_and_a_rollback_names_none(
    capsys: Any,
) -> None:
    """The swap leaves the rollback copy to keep or drop, so a successful rebuild names
    both; a table already on the target layout says so; a successful rollback or drop
    leaves nothing to do."""
    ch = _CH(code_test_facets=_table(OLD, 5))
    assert _run(ch, "--yes") == tool.OK
    step = _next(capsys.readouterr().out)
    assert "--rollback --yes" in step and "--drop-pre-rebuild --yes" in step
    assert _run(ch, "--rollback", "--yes") == tool.OK
    assert _next(capsys.readouterr().out) == ""
    assert _run(ch, "--drop-pre-rebuild", "--yes") == tool.OK
    assert _next(capsys.readouterr().out) == ""
    ch = _CH(code_test_facets=_table(NEW, 5))
    assert _run(ch, "--yes") == tool.OK
    assert "nothing to do" in _next(capsys.readouterr().out)


def test_the_next_step_follows_what_the_three_names_hold() -> None:
    g = lambda live, twin, pre: tr.guidance("t", "run", live, twin, pre)  # noqa: E731
    assert "RENAME TABLE loom.t_rebuild TO loom.t_pre_rebuild" in g(True, False, None)
    both = g(True, True, None)
    assert "compare" in both and "RENAME" not in both and "--restart" not in both
    assert "`run --rollback --yes`" in g(True, None, False)
    assert "nothing to do" in g(True, None, None)
    assert "`run --drop-pre-rebuild --yes`" in g(False, None, True)
    assert "`run --restart --yes`" in g(False, True, None)
    assert "`run --restart --yes`" in g(False, False, None)
    assert "by hand" in g(None, True, False)
    assert "blocks a new run" in g(False, None, False)


def test_the_tool_reports_the_layouts_as_on_or_off_target(capsys: Any) -> None:
    ch = _CH(code_test_facets=_table(NEW, 5), code_test_facets_pre_rebuild=_table(OLD, 5))
    job = tool.Rebuild(
        ch, tool.parse(["--table", "code_test_facets", "--migrations-dir", REBUILD_FIXTURE])
    )
    assert "--rollback --yes" in job.report_state()


def test_the_disk_check_wants_room_for_a_second_copy() -> None:
    assert tool.disk_refusal(100, None) == "" and tool.disk_refusal(100, 111) == ""
    assert "needs 1.1x" in tool.disk_refusal(2**30, 2**30)


def test_a_sort_key_with_a_function_splits_at_the_top_level_only() -> None:
    assert tr.key_columns("org, cityHash64(a, b)") == ["org", "cityHash64(a, b)"]


def test_a_signal_during_the_exchange_waits_for_the_pair() -> None:
    done = []
    with pytest.raises(tr.Interrupted):
        with tr.signals_deferred():
            os.kill(os.getpid(), signal.SIGTERM)
            done.append("rename")
    assert done == ["rename"]


def test_a_copy_never_names_a_computed_column() -> None:
    class _Cols:
        def query(self, sql: str, parameters: Any = None, settings: Any = None) -> Any:
            rows = [
                ("k", "String", "", "", ""),
                ("lc", "String", "MATERIALIZED", "lower(k)", ""),
                ("a", "String", "ALIAS", "k", ""),
                ("d", "UInt8", "DEFAULT", "0", ""),
            ]
            return type("R", (), {"result_rows": rows})()

    assert tr.insertable_columns(_Cols(), "t") == ["k", "d"]
