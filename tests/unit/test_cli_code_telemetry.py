"""`loom code <sub>`: one root span per invocation, stderr logging, stdout untouched.

The span makes scheduled scip-full/scip-delta/backfill runs observable without
tailing a log file. `init_otel` is suppressed under pytest by design, so these
tests route the CLI tracer to a private in-memory provider and check the span
shape, the exit-code/status stamping, and that stdout (which run logs and
operators grep) is unchanged byte for byte.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.cli import code, code_run, code_scip

pytestmark = pytest.mark.usefixtures("stub_model_command")


@pytest.fixture(autouse=True)
def offline_provider_connection(monkeypatch):
    """Telemetry tests do not connect to provider configuration or a real DB."""
    ch = SimpleNamespace(connected=False, connect=AsyncMock(), disconnect=AsyncMock())
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", AsyncMock())


@pytest.fixture
def cli_spans(monkeypatch: pytest.MonkeyPatch) -> Any:
    from opentelemetry import trace as _trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    real = _trace.get_tracer

    def _get_tracer(name: str, *a: Any, **kw: Any) -> Any:
        return provider.get_tracer(name) if name == "loom.cli" else real(name, *a, **kw)

    monkeypatch.setattr(_trace, "get_tracer", _get_tracer)
    # `init_otel` is a no-op under pytest (returns None); `shutdown` must be
    # harmless with nothing initialized.
    return exporter


@pytest.fixture(autouse=True)
def fresh_cli_logging(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`_setup_cli_logging` runs once per process, binds its handler to the
    `sys.stderr` of that moment and lowers the root and noisy-library levels.
    Every `_run` here trips it, so each test re-arms the latch, starts from
    an empty root handler list and gets the levels back afterwards."""
    monkeypatch.setattr(code_run, "_LOGGING_CONFIGURED", False)
    monkeypatch.setattr(logging.getLogger(), "handlers", [])
    names = ("", "httpx", "httpcore", "clickhouse_connect", "urllib3")
    levels = {n: logging.getLogger(n).level for n in names}
    yield
    for n, level in levels.items():
        logging.getLogger(n).setLevel(level)


def test_run_wraps_subcommand_in_a_root_span(
    cli_spans: Any, capsys: Any, fresh_cli_logging: None
) -> None:
    async def _sub() -> int:
        print("backfill: 3 files at abcdef123456")  # the stdout contract
        code_run._note(files=3, symbols=10, edges=4, errors=0, head="abcdef123456")
        return 0

    assert code_run._run("backfill", _sub()) == 0

    out, err = capsys.readouterr()
    assert out == "backfill: 3 files at abcdef123456\n"  # nothing added to stdout
    assert "cli.code.backfill: start" in err and "exit 0" in err  # logging on stderr

    (span,) = cli_spans.get_finished_spans()
    assert span.name == "cli.code.backfill" and span.parent is None
    a = dict(span.attributes or {})
    assert a["loom.operation"] == "cli.code.backfill"
    assert a["loom.cli.status"] == "ok" and a["loom.cli.exit_code"] == 0
    assert a["loom.cli.files"] == 3 and a["loom.cli.symbols"] == 10 and a["loom.cli.edges"] == 4
    assert a["loom.cli.head"] == "abcdef123456"
    assert isinstance(a["loom.cli.duration_ms"], int)


def test_nonzero_exit_marks_span_error(cli_spans: Any) -> None:
    from opentelemetry.trace import StatusCode

    async def _sub() -> int:
        return 2

    assert code_run._run("scip-full", _sub()) == 2
    (span,) = cli_spans.get_finished_spans()
    a = dict(span.attributes or {})
    assert a["loom.cli.status"] == "error" and a["loom.cli.exit_code"] == 2
    assert span.status.status_code == StatusCode.ERROR


def test_exception_is_stamped_and_re_raised(cli_spans: Any) -> None:
    async def _sub() -> int:
        raise RuntimeError("harness exit 3")

    with pytest.raises(RuntimeError):
        code_run._run("scip-delta", _sub())
    (span,) = cli_spans.get_finished_spans()
    a = dict(span.attributes or {})
    assert a["loom.cli.error"].startswith("RuntimeError: harness exit 3")
    assert a["loom.cli.exit_code"] == 1


def test_cli_logging_goes_to_stderr_once(fresh_cli_logging: None) -> None:
    code_run._setup_cli_logging()
    code_run._setup_cli_logging()
    root = logging.getLogger()
    stderr_handlers = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stderr
    ]
    assert len(stderr_handlers) == 1
    assert root.level <= logging.INFO


def test_every_code_subcommand_is_named_for_its_span() -> None:
    """Each `_cmd_*` must go through `_run("<sub>", ...)` — a subcommand added
    with a bare `asyncio.run` would silently lose its span and logging."""
    import inspect
    import re

    src = inspect.getsource(code)
    cmds = re.findall(r"def (_cmd_\w+)\(", src)
    assert len(cmds) >= 13
    for cmd in cmds:
        body = src.split(f"def {cmd}(", 1)[1].split("\ndef ", 1)[0]
        assert re.search(r'_run\("[a-z-]+", _\w+\(args\)\)', body), cmd


@pytest.mark.asyncio
async def test_scip_delta_subcommand_prints_result_line(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A shell snippet printed `RESULT: <result>`; the subcommand
    that replaces it must keep that line and turn errors into exit 1."""
    from server.code.workers import scip_delta as w
    from server.core import ch

    class _Client:
        connected = False

        async def connect(self) -> None: ...
        async def disconnect(self) -> None: ...

    monkeypatch.setattr(ch, "get_client", lambda: _Client())

    async def _pass() -> w.CodeScipDeltaResult:
        return w.CodeScipDeltaResult(repos_checked=1, deltas_applied=1)

    monkeypatch.setattr(w, "run_pass", _pass)
    import argparse

    assert await code_scip._scip_delta(argparse.Namespace()) == 0
    out, _ = capsys.readouterr()
    assert out.startswith("RESULT: CodeScipDeltaResult(repos_checked=1, deltas_applied=1")

    async def _bad() -> w.CodeScipDeltaResult:
        return w.CodeScipDeltaResult(repos_checked=1, errors=["ns: RuntimeError: boom"])

    monkeypatch.setattr(w, "run_pass", _bad)
    assert await code_scip._scip_delta(argparse.Namespace()) == 1
    out, err = capsys.readouterr()
    assert out.startswith("RESULT: ") and "boom" in err
