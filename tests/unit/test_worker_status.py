"""Worker observations preserve terminal uncertainty and pytest outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from tests import _worker_status as w

pytest_plugins = ("pytester",)


class Process:
    pid = 123

    def __init__(self, *codes: int | None, error: Exception | None = None):
        self.codes = iter(codes)
        self.error = error
        self.calls = 0

    def poll(self):
        self.calls += 1
        if self.error:
            raise self.error
        return next(self.codes)


class Node:
    def __init__(self, process, *, popen=True, via=None):
        self.gateway = SimpleNamespace(
            id="gw0", spec=SimpleNamespace(popen=popen, via=via), _io=SimpleNamespace(popen=process)
        )
        self.workeroutput = {"exitstatus": 1}


@pytest.fixture
def collector(tmp_path):
    config = SimpleNamespace(
        rootpath=tmp_path,
        pluginmanager=SimpleNamespace(getplugin=lambda _: None),
        getoption=lambda name, default=None: {"randomly_seed": 1729, "numprocesses": 2}.get(
            name, default
        ),
        getini=lambda name: {"faulthandler_timeout": 300, "faulthandler_exit_on_timeout": False}[
            name
        ],
    )
    return w.WorkerStatus(config)


def records(collector):
    return [json.loads(line) for line in collector.path.read_text().splitlines()]


@pytest.mark.parametrize("code", [None, 0, 7, -9, -11])
def test_node_down_distinguishes_os_status_from_pytest_status(collector, code):
    process = Process(code)
    collector.pytest_testnodedown(Node(process), "Not properly terminated")
    record = records(collector)[0]
    assert record["stage"] == "node_down"
    assert record["pid"] == 123
    assert record["os_returncode"] == code
    assert record["os_signal"] == (-code if code is not None and code < 0 else None)
    assert record["os_state"] == ("running" if code is None else "exited")
    assert record["pytest_exitstatus"] == 1
    assert process.calls == 1


def test_status_first_observed_after_teardown_keeps_its_stage(collector):
    node = Node(Process(None, None, -9))
    collector.pytest_testnodedown(node, "channel lost")
    finish = collector.pytest_sessionfinish()
    next(finish)
    sentinel = object()
    with pytest.raises(StopIteration) as stopped:
        finish.send(sentinel)
    assert stopped.value.value is sentinel
    observations = records(collector)[:3]
    assert [r["stage"] for r in observations] == [
        "node_down",
        "before_teardown",
        "after_teardown",
    ]
    assert [r["os_returncode"] for r in observations] == [None, None, -9]


def test_existing_teardown_exception_is_preserved(collector):
    collector.pytest_configure_node(Node(Process(None, 0)))
    finish = collector.pytest_sessionfinish()
    next(finish)
    original = RuntimeError("existing teardown failure")
    with pytest.raises(RuntimeError) as raised:
        finish.throw(original)
    assert raised.value is original
    assert records(collector)[1]["stage"] == "after_teardown"


@pytest.mark.parametrize("popen,via", [(False, None), (True, "gateway")])
def test_nonlocal_gateway_is_unavailable_without_polling(collector, popen, via):
    process = Process(error=AssertionError("must not poll remote transport"))
    collector.pytest_testnodedown(Node(process, popen=popen, via=via), None)
    record = records(collector)[0]
    assert record["os_returncode"] is None
    assert record["os_state"] == "unavailable"
    assert process.calls == 0


def test_poll_failure_stays_unknown_without_losing_pytest_status(collector):
    collector.pytest_testnodedown(Node(Process(error=OSError("unavailable"))), None)
    record = records(collector)[0]
    assert record["os_returncode"] is None
    assert record["os_signal"] is None
    assert record["os_state"] == "OSError"
    assert record["pytest_exitstatus"] == 1


def test_artifact_write_failure_keeps_the_observation_in_the_log(collector, capsys):
    collector.path = collector.config.rootpath / "absent" / "status.jsonl"
    collector.pytest_testnodedown(Node(Process(-11)), None)
    record = json.loads(capsys.readouterr().err.removeprefix("[worker-status] "))
    assert record["os_signal"] == 11
    assert record["artifact_write_failed"] is True
    assert record["artifact_available"] is False


def test_records_and_retained_handles_are_bounded(collector, capsys):
    for _ in range(w.MAX_HANDLES + 3):
        collector.pytest_configure_node(Node(Process()))
    assert len(collector.handles) == w.MAX_HANDLES
    assert collector.dropped_handles == 3
    for _ in range(w.MAX_RECORDS + 3):
        collector.emit({"event": "synthetic"})
    assert len(records(collector)) == w.MAX_RECORDS
    assert records(collector)[-1]["event"] == "record_limit"
    assert collector.path.stat().st_size <= w.MAX_RECORDS * w.MAX_RECORD_BYTES
    assert len(capsys.readouterr().err.splitlines()) == w.MAX_RECORDS
    assert len(w._encode({"value": "x" * w.MAX_RECORD_BYTES}).encode()) <= w.MAX_RECORD_BYTES


def test_context_contains_only_allowlisted_environment(collector, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("PRIVATE_TOKEN", "must not appear")
    collector.pytest_sessionstart()
    context = records(collector)[0]
    assert context["environment"]["GITHUB_RUN_ID"] == "42"
    assert "PRIVATE_TOKEN" not in context["environment"]
    assert context["seed"] == 1729
    assert context["faulthandler_timeout"] == 300
    assert context["faulthandler_exit_on_timeout"] is False


def test_every_retained_handle_gets_post_teardown_evidence(collector):
    collector.pytest_sessionstart()
    for _ in range(w.MAX_HANDLES):
        node = Node(Process(None, None, None, 0))
        node.workeroutput["exitstatus"] = 2
        collector.pytest_testnodedown(node, None)
        collector.pytest_testnodedown(node, "keyboard-interrupt")
    finish = collector.pytest_sessionfinish()
    next(finish)
    with pytest.raises(StopIteration):
        next(finish)
    observed = records(collector)
    after = [r for r in observed if r.get("stage") == "after_teardown"]
    assert len(after) == w.MAX_HANDLES
    assert all(r["os_returncode"] == 0 for r in after)
    assert observed[-1] == {
        "event": "collection_end",
        "dropped_handles": 0,
        "sequence": 4 * w.MAX_HANDLES + 2,
        "artifact_available": True,
    }


def test_plugin_preserves_real_pytest_failures_and_junit(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    pytester.makepyfile(
        """
        def test_pass():
            assert 1 == 1
        def test_assertion():
            assert False, "original assertion"
        def test_exception():
            raise ValueError("original exception")
        """
    )
    outcomes = []
    for enabled in (False, True):
        report = pytester.path / f"result-{enabled}.xml"
        args = ["-p", "xdist.plugin", "-n", "1", f"--junitxml={report}"]
        if enabled:
            args += ["-p", "tests._worker_status"]
        result = pytester.runpytest_subprocess(*args)
        result.assert_outcomes(passed=1, failed=2)
        assert result.ret == pytest.ExitCode.TESTS_FAILED
        cases = ElementTree.parse(report).findall(".//testcase")
        outcomes.append(sorted((c.attrib["name"], c.find("failure") is not None) for c in cases))
        assert "original assertion" in result.stdout.str()
        assert "original exception" in result.stdout.str()
    assert outcomes[0] == outcomes[1]
    files = list((pytester.path / w.ARTIFACT_ROOT).glob("*/status.jsonl"))
    assert len(files) == 1  # The explicitly loaded plugin does not run inside workers.
    observed = [json.loads(line) for line in files[0].read_text().splitlines()]
    statuses = [r for r in observed if r["event"] == "worker_status"]
    assert [r["stage"] for r in statuses] == ["node_down", "before_teardown", "after_teardown"]
    assert statuses[-1]["os_returncode"] == 0
    assert statuses[-1]["pytest_exitstatus"] == 1
