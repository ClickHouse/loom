"""A parent receives bounded phase evidence while a captured finalizer is blocked."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import selectors
import shutil
import subprocess
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "integration" / "_restore_phase_witness.py"
PREFIX = b"RESTORE_PHASE "


def _command(tmp_path: Path) -> tuple[list[str], dict[str, str]]:
    shutil.copyfile(PLUGIN, tmp_path / "phase_witness.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
        "PYTHONPATH": str(tmp_path),
    }
    return [sys.executable, "-m", "pytest", "-p", "phase_witness", "-q", "--tb=no"], env


def _before_teardown_returns(process: subprocess.Popen[bytes]) -> list[dict]:
    assert process.stdout is not None
    records, buffered = [], b""
    deadline = time.monotonic() + 10
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            ready = selector.select(max(0, deadline - time.monotonic()))
            assert ready, "phase evidence stayed captured while finalization was blocked"
            part = os.read(process.stdout.fileno(), 65536)
            assert part, "the child returned before its finalizer was released"
            buffered += part
            assert len(buffered) < 262144
            while b"\n" in buffered:
                line, buffered = buffered.split(b"\n", 1)
                if line.startswith(PREFIX):
                    record = json.loads(line[len(PREFIX) :])
                    records.append(record)
                    if (record["phase"], record["event"]) == (2, 0):
                        return records


@pytest.mark.parametrize("module", ["test_cell_restore", "test_cell_restore_reconcile"])
@pytest.mark.parametrize("capture", ["ordinary", "capfd"])
def test_failure_evidence_reaches_parent_before_blocked_teardown(tmp_path, module, capture):
    read_fd, release_fd = os.pipe()
    command, env = _command(tmp_path)
    dependency = "capfd" if capture == "capfd" else ""
    source = (
        "import os\nimport pytest\n"
        "@pytest.fixture\n"
        f"def blocked({dependency}):\n"
        "    yield\n"
        f"    os.read({read_fd}, 1)\n"
        "def test_failure(blocked):\n"
        "    raise AssertionError('private failure text')\n"
    )
    (tmp_path / (module + ".py")).write_text(source)
    process = subprocess.Popen(
        command,
        cwd=tmp_path,
        env=env,
        pass_fds=(read_fd,),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    os.close(read_fd)
    try:
        records = _before_teardown_returns(process)
        assert process.poll() is None
        assert [(r["phase"], r["event"]) for r in records] == [
            (0, 0),
            (0, 1),
            (1, 0),
            (1, 2),
            (2, 0),
        ]
        error = records[3]
        assert error["type"] == 1 and error["file"] == module + ".py" and error["line"] == 8
        node = hashlib.sha256((module + ".py::test_failure").encode()).hexdigest()
        assert all(r["node"] == node for r in records)
        assert [r["seq"] for r in records] == list(range(1, 6))
        assert all(r["monotonic_ns"] > 0 for r in records)
        assert [r["monotonic_ns"] for r in records] == sorted(r["monotonic_ns"] for r in records)
        assert all(
            set(r) == {"v", "seq", "node", "phase", "event", "monotonic_ns", "type", "file", "line"}
            for r in records
        )
        assert "private failure text" not in json.dumps(records)
        os.write(release_fd, b"1")
        tail, _ = process.communicate(timeout=10)
        assert process.returncode == 1
        completed = [
            json.loads(line[len(PREFIX) :]) for line in tail.splitlines() if line.startswith(PREFIX)
        ]
        assert [(r["phase"], r["event"]) for r in completed] == [(2, 1)]
    finally:
        os.close(release_fd)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def test_unselected_module_has_no_phase_output(tmp_path):
    command, env = _command(tmp_path)
    (tmp_path / "test_other.py").write_text("def test_other():\n    assert True\n")
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert PREFIX not in result.stdout and PREFIX not in result.stderr


def test_overflow_is_visible_once_without_changing_outcomes(tmp_path):
    command, env = _command(tmp_path)
    (tmp_path / "conftest.py").write_text("import phase_witness\nphase_witness._MAX_RECORDS = 2\n")
    (tmp_path / "test_cell_restore.py").write_text(
        "def test_first():\n    assert True\ndef test_second():\n    assert True\n"
    )
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, timeout=10)
    assert result.returncode == 0
    records = [
        json.loads(line[len(PREFIX) :])
        for line in result.stdout.splitlines()
        if line.startswith(PREFIX)
    ]
    assert len(records) == 3 and [r["seq"] for r in records] == [1, 2, 3]
    assert [r["event"] for r in records].count(3) == 1


def test_recorder_failure_preserves_original_phase_error():
    spec = importlib.util.spec_from_file_location("restore_phase_control", PLUGIN)
    assert spec is not None and spec.loader is not None
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    def broken_sink(_line):
        raise RuntimeError("diagnostic sink failed")

    reporter = SimpleNamespace(write_line=broken_sink)
    capture = SimpleNamespace(global_and_fixture_disabled=nullcontext)
    manager = SimpleNamespace(
        getplugin=lambda name: reporter if name == "terminalreporter" else capture
    )
    item = SimpleNamespace(
        path=PLUGIN.with_name("test_cell_restore.py"),
        nodeid="test_cell_restore.py::test_failure",
        config=SimpleNamespace(stash=pytest.Stash(), rootpath=PLUGIN.parent, pluginmanager=manager),
    )
    phase = plugin._phase(item, 1)
    assert next(phase) is None
    original = ValueError("primary phase error")
    with pytest.raises(ValueError) as caught:
        phase.throw(original)
    assert caught.value is original
