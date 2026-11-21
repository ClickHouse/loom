"""Proof diagnostics retain the interrupted phase without changing the outcome."""

import asyncio
import json
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.unit.test_cell_trigger_ci_creator import keeper_config_case as keeper_config_case
from tests.unit.test_cell_trigger_ci_creator import protocol_reader as protocol_reader


@pytest.mark.parametrize("mode", ["refused", "cancelled", "diagnostic-io-error"])
async def test_provenance_records_the_active_phase_and_preserves_the_first_error(
    keeper_config_case, monkeypatch, tmp_path, mode
):
    native, _, _, _, _ = keeper_config_case
    folder = tmp_path / "serving-cell"
    folder.mkdir()
    native.write(folder / "launch.json", {"server": {"pid": 7}})
    expected = asyncio.CancelledError() if mode == "cancelled" else RuntimeError("private-proof")
    calls = []

    @contextmanager
    def workload(*args):
        calls.append("workload")
        try:
            yield SimpleNamespace()
        finally:
            calls.append("workload-close")

    @asynccontextmanager
    async def connections(context):
        calls.append("connect")
        try:
            yield SimpleNamespace(is_in_transaction=lambda: False), object()
        finally:
            calls.append("disconnect")

    async def begin(*args):
        calls.append("begin")
        return {"backend_pid": 7}

    async def census(*args):
        calls.append("census")
        return {}

    async def verify(*args, progress=None):
        calls.append("verify")
        if progress is not None:
            progress(2, 1, 0)
        raise expected

    monkeypatch.setattr(native, "codec_contract", lambda: object())
    monkeypatch.setattr(native, "Workload", workload)
    monkeypatch.setattr(native, "connections", connections)
    monkeypatch.setattr(native, "begin_observation", begin)
    monkeypatch.setattr(native, "ch_census", census)
    monkeypatch.setattr(native, "verify_pages", verify)
    if mode == "diagnostic-io-error":
        original_open = Path.open

        def open_path(path, *args, **kwargs):
            if path.name == "provenance-progress.jsonl":
                raise OSError("private-diagnostic-storage")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(BaseException) as caught:
        await native.independent_provenance(
            {"workdir": str(tmp_path)}, tmp_path / "provenance.json"
        )
    assert caught.value is expected
    assert calls == [
        "workload",
        "connect",
        "begin",
        "census",
        "verify",
        "disconnect",
        "workload-close",
    ]
    assert not (tmp_path / "provenance.json").exists()
    progress = tmp_path / "provenance-progress.jsonl"
    if mode == "diagnostic-io-error":
        assert not progress.exists()
    else:
        raw = progress.read_text()
        rows = [json.loads(line) for line in raw.splitlines()]
        assert rows[0] == [0, 1, 100, 45500]
        assert [row for row in rows if row[0] == 1][-1][1:4] == [2, 1, 0]
        assert all(type(n) is int and n >= 0 for row in rows for n in row)
        assert "private" not in raw and "pid" not in raw


@pytest.mark.parametrize("mode", ["returned", "refused", "cancelled"])
def test_owner_budget_is_recorded_before_cleanup_without_replacing_the_result(
    keeper_config_case, monkeypatch, mode
):
    native, instance, owner, _, _ = keeper_config_case
    clock = SimpleNamespace(now=6990.0)
    monkeypatch.setattr(
        native,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.now,
            perf_counter_ns=lambda: 17,
            process_time_ns=lambda: 11,
        ),
    )
    owner.started = 0.0
    instance.cli_settings = {"same": "private-settings"}
    expected = asyncio.CancelledError() if mode == "cancelled" else RuntimeError("private-refusal")
    calls = []

    def action(task, **kwargs):
        calls.append((task, kwargs))
        clock.now = 7021.0
        if mode != "returned":
            raise expected

    monkeypatch.setattr(instance, "action", action)
    if mode == "returned":
        assert instance.provenance() is None
    else:
        with pytest.raises(BaseException) as caught:
            instance.provenance()
        assert caught.value is expected
    assert calls == [("provenance", {"settings": instance.cli_settings})]
    raw = (instance.w / "provenance-budget.jsonl").read_text()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows == [
        [0, 1, 0, 6990000, 30000, 0, 0],
        [0, 1, 1, 7021000, 0, 31000, int(mode == "returned")],
    ]
    assert "private" not in raw


def test_progress_publication_is_separate_from_the_acceptance_result(tmp_path):
    from scripts.gates import cell_trigger_acceptance as creator

    folder = tmp_path / "private"
    source = folder / "fixture"
    source.mkdir(parents=True)
    child = [[0, 1, 100, 45500], [1, 2, 1, 0, 5, 3], [2, 2, 0, 0]]
    budget = [[0, 1, 0, 6990000, 30000, 0, 0], [0, 1, 1, 7021000, 0, 31000, 0]]
    for name, rows in [("provenance-progress", child), ("provenance-budget", budget)]:
        (source / (name + ".jsonl")).write_text("".join(json.dumps(row) + "\n" for row in rows))
    (source / "private.log").write_text("private-settings-token")
    destination = tmp_path / "published"
    creator.publish(
        folder,
        destination,
        RuntimeError("private-error"),
        phase="native",
        diagnostic_error_type=None,
    )
    result = json.loads((destination / "result.json").read_text())
    assert result["accepted"] is False and result["error_type"] == "RuntimeError"
    progress = json.loads((destination / "provenance-progress.json").read_text())
    assert progress == {"schema": 1, "rows": child, "budget": budget, "incomplete_tail": 0}
    assert {f.name for f in destination.iterdir()} == {"result.json", "provenance-progress.json"}
    assert "private-" not in "".join(f.read_text() for f in destination.iterdir())


@pytest.mark.parametrize(
    "fault,phase", [("", 1), ("native_hash", 3), ("ch_value", 5), ("audit_missing", 7)]
)
async def test_page_progress_follows_the_full_existing_hash_and_comparison_checks(
    keeper_config_case, protocol_reader, monkeypatch, fault, phase
):
    from tests.unit.test_cell_trigger_provenance_reuse import _exercise

    native, *_ = keeper_config_case
    original = native.verify_pages
    records = []

    async def observed(*args, **kwargs):
        return await original(*args, **kwargs, progress=lambda *row: records.append(row))

    monkeypatch.setattr(native, "verify_pages", observed)
    await _exercise(native, monkeypatch, 2, False, fault)
    assert records[0] == (1, 1, 0)
    assert records[-1] == ((1, 2, 2) if not fault else (phase, 1, 0))
    if not fault:
        assert records == [(number, 1, 0) for number in range(1, 8)] + [(1, 2, 2)]


@pytest.mark.parametrize("phase", [2, 4, 6])
async def test_page_cancellation_keeps_the_interrupted_query_phase(
    keeper_config_case, protocol_reader, monkeypatch, phase
):
    from tests.unit.test_cell_trigger_provenance_reuse import _exercise

    native, *_ = keeper_config_case
    original = native.verify_pages
    records = []
    expected = asyncio.CancelledError()

    async def observed(conn, ch, *args, **kwargs):
        fetch, query = conn.fetch, ch.query

        async def fetch_page(*call):
            if phase == 2:
                raise expected
            return await fetch(*call)

        async def query_page(sql, **call):
            if phase == (6 if ".audit_log " in sql else 4):
                raise expected
            return await query(sql, **call)

        conn.fetch, ch.query = fetch_page, query_page
        return await original(conn, ch, *args, **kwargs, progress=lambda *row: records.append(row))

    monkeypatch.setattr(native, "verify_pages", observed)
    with pytest.raises(asyncio.CancelledError) as caught:
        await _exercise(native, monkeypatch, 2, False)
    assert caught.value is expected
    assert records[-1] == (phase, 1, 0)


@pytest.mark.parametrize(
    "mode",
    [
        "string",
        "bool",
        "negative",
        "phase",
        "rows",
        "clock",
        "bytes",
        "records",
        "symlink",
        "read-error",
        "write-error",
        "truncated",
        "absent",
    ],
)
def test_progress_export_is_closed_bounded_and_never_replaces_acceptance(
    tmp_path, monkeypatch, mode
):
    from scripts.gates import cell_trigger_acceptance as creator

    folder = tmp_path / "private"
    source = folder / "fixture/provenance-progress.jsonl"
    source.parent.mkdir(parents=True)
    rows = [[0, 1, 100, 45500], [1, 2, 1, 0, 5, 3]]
    if mode == "string":
        rows[1][1] = "private-token"
    elif mode == "bool":
        rows[1][2] = True
    elif mode == "negative":
        rows[1][4] = -1
    elif mode == "phase":
        rows[1][1] = 26
    elif mode == "rows":
        rows[1][3] = 45501
    elif mode == "clock":
        rows[1][5] = 2**63
    elif mode == "records":
        rows += [rows[1]] * 3216
    contents = "".join(json.dumps(row) + "\n" for row in rows)
    if mode == "bytes":
        contents = " " * 823553
    elif mode == "truncated":
        contents += '[1,"private-unfinished'
    if mode == "symlink":
        target = folder / "private-source"
        target.write_text("private-token")
        source.symlink_to(target)
    elif mode != "absent":
        source.write_text(contents)
    if mode == "read-error":
        original_open = creator.os.open

        def open_path(path, *args, **kwargs):
            if path == source:
                raise OSError("private-read-error")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(creator.os, "open", open_path)
    if mode == "write-error":
        original_write = creator.write

        def write(path, *args, **kwargs):
            if path.name == "provenance-progress.json":
                raise OSError("private-write-error")
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(creator, "write", write)
    destination = tmp_path / "published"
    creator.publish(folder, destination, None, phase="native", diagnostic_error_type=None)
    result = json.loads((destination / "result.json").read_text())
    assert result["accepted"] is True and result["error_type"] == ""
    if mode in {"absent", "write-error"}:
        assert {p.name for p in destination.iterdir()} == {"result.json"}
    else:
        diagnostic = json.loads((destination / "provenance-progress.json").read_text())
        assert diagnostic == (
            {"schema": 1, "rows": rows, "budget": [], "incomplete_tail": 1}
            if mode == "truncated"
            else {"schema": 1, "invalid": 1}
        )
    assert "private-" not in "".join(p.read_text() for p in destination.iterdir())


def test_progress_stream_caps_records_and_keeps_its_numeric_drop_count(keeper_config_case):
    native, instance, *_ = keeper_config_case
    path = instance.w / "bounded-progress.jsonl"
    with native._ProofProgress(path) as progress:
        for _ in range(4000):
            progress.mark(2, 1, 0)
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    assert len(rows) == 3217 and len(raw) <= 823552
    assert rows[-1] == [2, 3216, 785, 0]
    assert all(len(line) + 1 <= 256 for line in raw.splitlines())
    assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("operation", ["write", "flush", "close"])
def test_progress_storage_errors_preserve_the_active_work_exception(
    keeper_config_case, monkeypatch, operation
):
    native, instance, *_ = keeper_config_case
    original_open = Path.open
    path = instance.w / "failed-progress.jsonl"
    opened = []

    class Stream:
        def __init__(self, actual):
            self.actual = actual

        def __getattr__(self, name):
            return getattr(self.actual, name)

        def write(self, value):
            if operation == "write":
                raise OSError("private-write")
            return self.actual.write(value)

        def flush(self):
            if operation == "flush":
                raise OSError("private-flush")
            return self.actual.flush()

        def close(self):
            self.actual.close()
            if operation == "close":
                raise OSError("private-close")

    def open_path(target, *args, **kwargs):
        stream = original_open(target, *args, **kwargs)
        if target == path:
            opened.append(stream)
            return Stream(stream)
        return stream

    monkeypatch.setattr(Path, "open", open_path)
    original = RuntimeError("private-proof")
    with pytest.raises(RuntimeError) as caught:
        with native._ProofProgress(path) as progress:
            progress.mark(2, 1, 0)
            raise original
    assert caught.value is original and opened[0].closed
