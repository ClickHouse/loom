"""Completed commands keep their terminal proof through fixture cleanup."""

import json
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.gates.testsupport.cell_trigger import fixture


@pytest.fixture
def command_owner(monkeypatch, tmp_path):
    clock = SimpleNamespace(now=10.0)
    state = SimpleNamespace(
        outcome=0,
        stop_signal=signal.SIGTERM,
        stop_status=0,
        processes=[],
        closed=[],
        events=[],
        adopted=[300],
        cleanup_calls=[],
    )

    class Process:
        pid = 200

        def __init__(self, *args, **kwargs):
            self.returncode = None
            self.polls = 0
            self.reaped = False
            state.processes.append(self)

        def poll(self):
            self.polls += 1
            if state.outcome is not None:
                self.returncode = state.outcome
                self.reaped = True
            return self.returncode

        def wait(self):
            assert self.reaped
            return self.returncode

    class Owned:
        pid = 100

        def identity(self, pid):
            assert pid == 200
            return (self.pid, "birth", "S")

        def stop(self, process, note):
            assert process is None
            assert all(child.reaped for child in state.processes)
            state.cleanup_calls.append(list(state.adopted))
            if not getattr(state, "keep_adopted", False):
                state.adopted.clear()

        def children(self, pid):
            assert pid == self.pid
            state.cleanup_calls.append("kernel-census")
            return list(state.adopted)

    def note(event, **fields):
        state.events.append((event, fields))
        if event == "terminal" and getattr(state, "event_error", None):
            raise state.event_error

    def send(pidfd, sig):
        assert pidfd == 900
        if sig == state.stop_signal:
            state.outcome = state.stop_status

    original_stat, original_close = fixture.os.stat, fixture.os.close
    monkeypatch.setattr(fixture, "source", lambda _: None)
    monkeypatch.setattr(fixture.subprocess, "Popen", Process)
    monkeypatch.setattr(fixture.os, "pidfd_open", lambda pid: 900 if pid == 200 else None)
    monkeypatch.setattr(fixture.signal, "pidfd_send_signal", send)
    monkeypatch.setattr(
        fixture.os,
        "stat",
        lambda path, *args, **kwargs: (
            SimpleNamespace(st_uid=fixture.os.geteuid())
            if str(path) == "/proc/200"
            else original_stat(path, *args, **kwargs)
        ),
    )
    monkeypatch.setattr(
        fixture.os,
        "close",
        lambda fd: state.closed.append(fd) if fd == 900 else original_close(fd),
    )
    monkeypatch.setattr(
        fixture,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock.now,
            sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
        ),
    )
    owner = fixture.Owner({"root": str(tmp_path), "workdir": str(tmp_path)}, Owned(), note)
    owner.sampled = clock.now
    return owner, state


def assert_terminal_cleanup(owner, state, item):
    process = item["process"]
    assert process.reaped and process.polls >= 2
    assert item["terminal"] == json.loads(Path(item["terminal_receipt"]["path"]).read_text())
    assert item["terminal"]["returncode"] == process.returncode
    assert [event for event, _ in state.events].count("terminal") == 1
    assert state.cleanup_calls == [[300], "kernel-census"]
    assert state.closed == [900] and item["pidfd"] is None


def test_successful_router_command_remains_successful_after_cleanup(command_owner):
    owner, state = command_owner
    item = owner.command("router", ["/usr/bin/true", "migrate"], {})
    assert item["process"].reaped and item["terminal"]["returncode"] == 0
    assert item["signals"] == [] and owner.first is None
    owner.cleanup()
    assert_terminal_cleanup(owner, state, item)
    assert owner.first is None
    assert owner.failure_summary()["records"] == []


def test_failed_command_remains_owned_until_cleanup(command_owner):
    owner, state = command_owner
    state.outcome = 7
    with pytest.raises(fixture.Refused, match="native command refused"):
        owner.command("router", ["/usr/bin/true"], {})
    item = owner.leaders[0]
    assert not item.get("cleaned") and item["pidfd"] == 900
    owner.cleanup()
    assert_terminal_cleanup(owner, state, item)
    assert str(owner.first) == "unexpected fixture terminal outcome"


def test_pending_command_remains_owned_until_cleanup(command_owner):
    owner, state = command_owner
    state.outcome = None
    with pytest.raises(fixture.Refused, match="serial command deadline"):
        owner.command("router", ["/usr/bin/true"], {}, seconds=0)
    item = owner.leaders[0]
    assert not item.get("cleaned") and item["terminal"] is None
    assert item["pidfd"] == 900 and not item["process"].reaped
    owner.cleanup()
    assert_terminal_cleanup(owner, state, item)
    assert item["signals"] == [signal.SIGTERM] and owner.first is None


@pytest.mark.parametrize("failure", ["receipt", "event"])
def test_unverified_command_keeps_first_error_and_cleanup_ownership(
    command_owner, monkeypatch, failure
):
    owner, state = command_owner
    first = RuntimeError("terminal evidence refused")
    if failure == "receipt":
        original_write = fixture.write

        def write(path, value, **kwargs):
            if path.name.endswith("-terminal.json"):
                raise first
            return original_write(path, value, **kwargs)

        monkeypatch.setattr(fixture, "write", write)
    else:
        state.event_error = first
    with pytest.raises(RuntimeError) as caught:
        owner.command("router", ["/usr/bin/true"], {})
    item = owner.leaders[0]
    assert caught.value is first and owner.first is first
    assert not item.get("cleaned") and item["process"].reaped
    owner.cleanup()
    assert owner.first is first
    assert state.cleanup_calls == [[300], "kernel-census"] and state.closed == [900]
    assert any(str(error) == "unexpected fixture terminal outcome" for error, _ in owner.failures)


def test_successful_cleanup_command_retains_prior_failure(command_owner):
    owner, state = command_owner
    first = RuntimeError("prior refusal")
    owner.select(first)
    item = owner.command("inspect", ["/usr/bin/true"], {}, cleanup=True)
    owner.cleanup()
    assert_terminal_cleanup(owner, state, item)
    assert owner.first is first


@pytest.mark.parametrize(
    ("role", "status", "sent", "accepted"),
    [
        ("router", 0, [], False),
        ("proxy", 0, [], False),
        ("api", 0, [], False),
        ("router", 0, [signal.SIGTERM], True),
        ("router", -signal.SIGTERM, [signal.SIGTERM], True),
        ("proxy", 0, [signal.SIGTERM], True),
        ("api", 0, [signal.SIGTERM], True),
        ("api", -signal.SIGTERM, [signal.SIGTERM], True),
        ("router", 1, [signal.SIGTERM], False),
        ("proxy", -signal.SIGTERM, [signal.SIGTERM], False),
        ("api", 1, [signal.SIGTERM], False),
    ],
)
def test_service_terminal_outcomes_remain_strict(command_owner, role, status, sent, accepted):
    owner, state = command_owner
    item = owner.start(role, ["/usr/bin/true"], {}, persistent=True)
    state.outcome = status
    item["signals"] = sent
    owner.cleanup()
    assert_terminal_cleanup(owner, state, item)
    assert (owner.first is None) is accepted
    if not accepted:
        assert str(owner.first) == "unexpected fixture terminal outcome"


@pytest.mark.parametrize(
    "role,stop_signal", [("router", signal.SIGKILL), ("driver", signal.SIGTERM)]
)
def test_cleanup_escalation_still_refuses(command_owner, role, stop_signal):
    owner, state = command_owner
    state.outcome, state.stop_signal, state.stop_status = None, stop_signal, -stop_signal
    item = owner.start(role, ["/usr/bin/true"], {}, persistent=True)
    owner.cleanup()
    assert_terminal_cleanup(owner, state, item)
    assert str(owner.first) == "cleanup escalation"
    assert item["signals"][-1] == stop_signal


def test_completed_command_still_requires_empty_kernel_children(command_owner):
    owner, state = command_owner
    item = owner.command("router", ["/usr/bin/true"], {})
    state.keep_adopted = True
    with pytest.raises(fixture.Refused, match="kernel children remain"):
        owner.cleanup()
    assert state.cleanup_calls == [[300], "kernel-census"]
    assert state.adopted == [300] and state.closed == []
    assert item["pidfd"] == 900 and item["process"].reaped
