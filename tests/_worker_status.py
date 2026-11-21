"""Retain bounded OS worker status observations beside pytest's own results."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

    from xdist.workermanage import WorkerController

MAX_HANDLES = 64
# An interrupted worker can notify node-down twice; reserve both teardown records too.
MAX_RECORDS = 4 * MAX_HANDLES + 3
MAX_RECORD_BYTES = 2048
ARTIFACT_ROOT = Path(".pytest_cache/worker-status")


def _versions() -> dict[str, str]:
    versions = {}
    for package in ("pytest", "pytest-xdist", "execnet"):
        try:
            versions[package] = version(package)
        except (PackageNotFoundError, OSError):
            versions[package] = "unknown"
    return versions


class WorkerStatus:
    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.handles: dict[WorkerController, Any] = {}
        self.records = 0
        self.path: Path | None = None
        self.dropped_handles = 0
        try:
            root = config.rootpath / ARTIFACT_ROOT
            root.mkdir(parents=True, exist_ok=True)
            self.path = Path(tempfile.mkdtemp(prefix="session-", dir=root)) / "status.jsonl"
        except OSError:
            pass  # The job log still carries observations when artifact creation fails.

    def emit(self, record: dict[str, Any]) -> None:
        if self.records >= MAX_RECORDS:
            return
        if self.records == MAX_RECORDS - 1:
            record = {"event": "record_limit", "limit": MAX_RECORDS}
        self.records += 1
        record = {**record, "sequence": self.records, "artifact_available": self.path is not None}
        line = _encode(record)
        if self.path is not None:
            try:
                with self.path.open("a") as output:
                    output.write(line)
            except OSError:
                self.path = None
                record.update(artifact_available=False, artifact_write_failed=True)
                line = _encode(record)
        try:
            reporter = self.config.pluginmanager.getplugin("terminalreporter")
            if reporter is not None:
                reporter.write_line("[worker-status] " + line.rstrip())
            else:
                sys.stderr.write("[worker-status] " + line)
        except (OSError, ValueError):
            pass  # Diagnostics must preserve the original pytest result if output closes.

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self) -> None:
        self.emit(
            {
                "event": "context",
                "python": sys.version.split()[0],
                "platform": sys.platform,
                "versions": _versions(),
                "environment": {
                    key: os.environ[key][:128]
                    for key in (
                        "GITHUB_RUN_ID",
                        "GITHUB_RUN_ATTEMPT",
                        "GITHUB_JOB",
                        "GITHUB_SHA",
                        "LOOM_TEST_SHARD",
                    )
                    if key in os.environ
                },
                "seed": self.config.getoption("randomly_seed", default=None),
                "workers": self.config.getoption("numprocesses", default=None),
                "faulthandler_timeout": self.config.getini("faulthandler_timeout"),
                "faulthandler_exit_on_timeout": self.config.getini("faulthandler_exit_on_timeout"),
            }
        )

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: WorkerController) -> None:
        if node in self.handles:
            return
        if len(self.handles) >= MAX_HANDLES:
            self.dropped_handles += 1
            return
        spec = node.gateway.spec
        # An ssh/via gateway's local process status is not the remote worker's status.
        process = None
        if spec.popen and not spec.via:
            process = getattr(getattr(node.gateway, "_io", None), "popen", None)
        self.handles[node] = process

    def observe(self, node: WorkerController, stage: str, error: object | None = None) -> None:
        process = self.handles[node]
        returncode = None
        state = "unavailable" if process is None else "unknown"
        try:
            if process is not None:
                returncode = process.poll()
                state = "running" if returncode is None else "exited"
        except Exception as e:  # noqa: BLE001 — diagnostic failure must not replace pytest's outcome
            state = type(e).__name__[:64]
        output = getattr(node, "workeroutput", {})
        self.emit(
            {
                "event": "worker_status",
                "stage": stage,
                "worker": node.gateway.id[:64],
                "pid": getattr(process, "pid", None),
                "os_state": state,
                "os_returncode": returncode,
                "os_signal": -returncode if returncode is not None and returncode < 0 else None,
                "pytest_exitstatus": output.get("exitstatus"),
                "channel_error_type": type(error).__name__[:64] if error is not None else None,
            }
        )

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: WorkerController, error: object | None) -> None:
        if node not in self.handles:
            self.pytest_configure_node(node)
        if node in self.handles:
            self.observe(node, "node_down", error)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self) -> Generator[None, object, object]:
        for node in self.handles:
            self.observe(node, "before_teardown")
        try:
            return (yield)
        finally:
            for node in self.handles:
                self.observe(node, "after_teardown")
            self.emit({"event": "collection_end", "dropped_handles": self.dropped_handles})


def pytest_configure(config: pytest.Config) -> None:
    if not hasattr(config, "workerinput"):
        config.pluginmanager.register(WorkerStatus(config), "worker-terminal-status")


def _encode(record: dict[str, Any]) -> str:
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
    if len(line.encode()) > MAX_RECORD_BYTES:
        return '{"event":"record_too_large"}\n'
    return line
