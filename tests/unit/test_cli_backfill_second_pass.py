"""A namespace's first full backfill implies the edges-only
second pass — indexing order otherwise leaves every call
to a later file unresolved with zero candidates. The pass is recorded as owed
before the first batch, so a run interrupted before it finishes leaves it to the
next run."""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

import pytest

from server.cli import code_backfill
from server.cli.code_backfill import edge_pass_owed
from tests.unit.code_fixtures import repo_config


def _owed(**kw: Any) -> str:
    base: dict[str, Any] = {
        "explicit": False,
        "declined": False,
        "paths_file": False,
        "done_upto": "",
        "recorded": "",
        "side_by_side": False,
    }
    return edge_pass_owed(**{**base, **kw})


def test_first_full_backfill_implies_the_second_pass() -> None:
    assert _owed() == "first-backfill"


def test_a_resumed_run_or_a_repair_does_not_imply_it() -> None:
    assert not _owed(done_upto="412")
    assert not _owed(paths_file=True)


def test_a_resumed_run_runs_the_pass_an_earlier_run_owed() -> None:
    assert _owed(done_upto="412", recorded="first-backfill") == "first-backfill"


def test_the_flags_decide_when_given() -> None:
    assert _owed(explicit=True, paths_file=True, done_upto="9") == "requested"
    assert not _owed(declined=True, recorded="first-backfill")


def test_batches_written_side_by_side_owe_the_pass_whatever_the_flags_say() -> None:
    assert _owed(side_by_side=True, paths_file=True) == "workers"
    assert _owed(side_by_side=True, declined=True, done_upto="412") == "workers"
    # and a later run, with one process or the pass declined, still owes it
    assert _owed(declined=True, done_upto="412", recorded="workers") == "workers"


class _Interrupted(Exception):
    """The process went away in the middle of a pass."""


class _Store:
    """The namespace's state rows, kept across the runs of one test."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    def __call__(self) -> _Store:
        return self  # `CodeRepo()` in `_backfill`

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return self.state.get(key, "")

    async def set_state(self, org: str, ns: str, key: str, value: str, **_k: Any) -> None:
        self.state[key] = value


def _harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, paths: list[str]
) -> tuple[_Store, dict[str, Any]]:
    """`_backfill` on a stubbed store and checkout; `plan` says which batch or pass
    is interrupted, and records the batches and edge passes that ran."""
    from server.code.app.index import bootstrap, denylist
    from server.code.store import index

    store = _Store()
    plan: dict[str, Any] = {
        "fail_batch": None,
        "fail_edges": False,
        "edge_errors": 0,
        "unwritten": {},  # a batch's first path -> the rows its failed write left out
        "edge_unwritten": {},
        "batches": [],
        "edges": 0,
    }
    cfg = repo_config(org="acme", namespace="code-x", git_dir=str(tmp_path))

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    async def _head(*_a: object) -> str:
        return "f" * 40

    async def _paths(*_a: object) -> list[str]:
        return list(paths)

    async def _batch(_repo: Any, _org: str, _ns: str, chunk: list[str], **k: Any) -> str:
        if chunk[0] == plan["fail_batch"]:
            raise _Interrupted(chunk[0])
        plan["batches"].append(chunk[0])
        code_backfill.count_unwritten(k["counters"], plan["unwritten"].get(chunk[0], {}))
        return ""

    async def _edges(*_a: object, **_k: object) -> dict[str, int]:
        if plan["fail_edges"]:
            raise _Interrupted("edge pass")
        plan["edges"] += 1
        counters = {"files": len(paths), "edges": 0, "errors": plan["edge_errors"]}
        code_backfill.count_unwritten(counters, plan["edge_unwritten"])
        return counters

    monkeypatch.setattr(index, "CodeRepo", store)
    monkeypatch.setattr(code_backfill, "_repo_cfg", lambda _a: cfg)
    monkeypatch.setattr(code_backfill, "connect_code_plane", _noop)
    monkeypatch.setattr(code_backfill, "_backfill_head", _head)
    monkeypatch.setattr(code_backfill, "_backfill_paths", _paths)
    monkeypatch.setattr(code_backfill, "_held_batch", _batch)
    monkeypatch.setattr(code_backfill, "_second_edge_pass", _edges)
    monkeypatch.setattr(code_backfill, "_close_index", _noop)
    monkeypatch.setattr(denylist, "load_denylist", _noop)
    monkeypatch.setattr(bootstrap, "record_backfill_stamps", _noop)
    monkeypatch.setattr("sys.argv", ["loom", "code", "backfill", "--namespace", "code-x"])
    return store, plan


def _backfill(**kw: Any) -> int:
    base: dict[str, Any] = {
        "org": "acme",
        "namespace": "code-x",
        "phase": "index",
        "embed": False,
        "paths_file": "",
        "limit": 0,
        "concurrency": 2,
        "workers": 1,
        "edges_second_pass": False,
        "no_edges_second_pass": False,
        "dialect_folder_pass": False,
        "embedding_model_suffix": "",
    }
    base.update(kw)
    return asyncio.run(code_backfill._backfill(argparse.Namespace(**base)))


_PATHS = [f"f{i}.py" for i in range(450)]


def test_a_first_backfill_interrupted_in_its_index_phase_gets_its_edge_pass_on_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    store, plan = _harness(monkeypatch, tmp_path, _PATHS)
    plan["fail_batch"] = "f200.py"
    with pytest.raises(_Interrupted):
        _backfill()
    assert store.state["backfill_cursor:index"] == "200"
    plan["fail_batch"] = None
    assert _backfill() == 0  # resumed at 200
    assert plan["batches"] == ["f0.py", "f200.py", "f400.py"]
    assert plan["edges"] == 1
    assert store.state["backfill_edges_owed:index"] == ""
    assert store.state["git_head"] == "f" * 40


def test_a_rerun_after_an_interrupted_edge_pass_runs_the_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    store, plan = _harness(monkeypatch, tmp_path, _PATHS)
    plan["fail_edges"] = True
    with pytest.raises(_Interrupted):
        _backfill()
    assert store.state["backfill_cursor:index"] == str(len(_PATHS))
    assert "git_head" not in store.state
    plan["fail_edges"] = False
    assert _backfill() == 0  # no batch left, the pass still owed
    assert plan["edges"] == 1
    assert store.state["git_head"] == "f" * 40


def test_declining_the_pass_clears_what_an_earlier_run_owed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    store, plan = _harness(monkeypatch, tmp_path, _PATHS)
    plan["fail_batch"] = "f200.py"
    with pytest.raises(_Interrupted):
        _backfill()
    plan["fail_batch"] = None
    assert _backfill(no_edges_second_pass=True) == 0
    assert plan["edges"] == 0
    assert store.state["backfill_edges_owed:index"] == ""


def test_an_edge_pass_with_a_failed_file_stays_owed_until_a_rerun_finishes_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    store, plan = _harness(monkeypatch, tmp_path, _PATHS)
    plan["edge_errors"] = 1  # one file's edge ingest failed
    _backfill()
    assert plan["edges"] == 1
    assert store.state["backfill_edges_owed:index"] == "first-backfill"
    plan["edge_errors"] = 0
    assert _backfill() == 0  # no batch left, the pass still owed
    assert plan["edges"] == 2
    assert store.state["backfill_edges_owed:index"] == ""


_REPORT = f"backfill at {'f' * 12}: failed writes left up to "
_RERUN = "re-run to write them (rows already in the index are kept): "
_COMMAND = "loom code backfill --namespace code-x"


def test_a_failed_index_write_stops_the_run_at_its_batch_and_names_the_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failed write raised inside a stage that logs it, so no file counts as an
    error; the rows it dropped are the only record. The next batch's write would fail
    too, and that batch never starts."""
    store, plan = _harness(monkeypatch, tmp_path, _PATHS)
    plan["unwritten"] = {
        "f200.py": {"code_edges": 5, "code_files": 2},
        "f400.py": {"code_edges": 9},
    }
    assert _backfill() == 1
    assert plan["batches"] == ["f0.py", "f200.py"] and plan["edges"] == 0
    assert capsys.readouterr().err.splitlines() == [
        "backfill stopped at 200/450: a write failed",
        _REPORT + "7 rows out of the index (index phase: code_edges 5, code_files 2)",
        _RERUN + _COMMAND,
    ]
    # the run ends as a held one does: no watermark, the cursor before that batch
    assert "git_head" not in store.state
    assert store.state["backfill_cursor:index"] == "200"
    plan["unwritten"] = {}
    assert _backfill() == 0
    assert plan["batches"] == ["f0.py", "f200.py", "f200.py", "f400.py"] and plan["edges"] == 1
    assert store.state["git_head"] == "f" * 40
    assert capsys.readouterr().err == ""


def test_a_failed_edge_pass_write_stops_the_run_and_leaves_the_pass_owed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    store, plan = _harness(monkeypatch, tmp_path, _PATHS)
    plan["edge_unwritten"] = {"code_edges": 3}
    assert _backfill() == 1
    assert capsys.readouterr().err.splitlines() == [
        _REPORT + "3 rows out of the index (second edge pass: code_edges 3)",
        _RERUN + _COMMAND,
    ]
    # no watermark; the index phase is written, so the re-run starts at the pass
    assert "git_head" not in store.state
    assert store.state["backfill_cursor:index"] == "450"
    assert store.state["backfill_edges_owed:index"] == "first-backfill"
    plan["edge_unwritten"] = {}
    assert _backfill() == 0
    assert plan["batches"] == ["f0.py", "f200.py", "f400.py"] and plan["edges"] == 2
    assert store.state["backfill_edges_owed:index"] == ""
    assert store.state["git_head"] == "f" * 40


def test_rows_a_failed_write_left_out_fail_the_recorded_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run's `loom.worker_status` row when no file failed and a write did."""
    recorded: dict[str, Any] = {}

    async def _record(*_a: object, **k: Any) -> None:
        recorded.update(k)

    async def _noop(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(code_backfill, "_record_manual_pass", _record)
    monkeypatch.setattr(code_backfill, "_compact_after_bulk", _noop)
    monkeypatch.setattr(code_backfill, "_republish_counts", _noop)
    counters = code_backfill.index_counters() | {"files": 200}
    code_backfill.count_unwritten(counters, {"code_edges": 5, "code_files": 2})
    asyncio.run(
        code_backfill._close_index(None, counters, "acme", "code-x", "m", 0.0, "f" * 40, "backfill")
    )
    assert (recorded["done"], recorded["errors"]) == (200, 7)
    assert code_backfill.manual_pass_status(recorded["done"], recorded["errors"]) == "partial"
