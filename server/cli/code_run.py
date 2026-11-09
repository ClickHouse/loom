"""How a `loom code` subcommand runs: the namespace it addresses, and the
root span, stderr logging and client disconnect around it."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Coroutine, Iterator
from typing import Any

from server.cli._proc import TERMINATED_EXIT, Terminated, cancel_on_sigterm
from server.code.app.index.repo_modes import repo_umask

log = logging.getLogger("loom.cli.code")


def _identity(args: argparse.Namespace) -> tuple[str, str]:
    """(org, namespace) the command addresses: the flags, else the scalar settings.
    Neither = exit with the missing name; a code namespace is never guessed."""
    from server.settings import settings

    org = args.org or settings.code_org
    ns = args.namespace or settings.code_namespace
    if not org or not ns:
        missing = " and ".join(
            f
            for f, v in (("--org (LOOM_CODE_ORG)", org), ("--namespace (LOOM_CODE_NAMESPACE)", ns))
            if not v
        )
        raise SystemExit(f"no code namespace addressed: pass {missing}")
    return org, ns


def _repo_cfg(args: argparse.Namespace) -> Any:
    """Per-repo config for the addressed namespace (multi-repo aware).

    An explicitly addressed namespace that is not configured is an error,
    never a fallback to the first entry: when LOOM_CODE_REPOS_JSON fails to
    parse, configured_repos() degrades to the scalar entry, and falling back
    would index one branch's tree under another branch's namespace."""
    from server.code.domain.index.repos import (
        RegistryUnread,
        config_errors_for,
        configured_repos,
        page_namespaces,
        repo_for_namespace,
    )

    org, ns = _identity(args)
    try:
        cfg = repo_for_namespace(org, ns)
    except RegistryUnread:
        raise SystemExit("the self-service repository registry could not be read; retry") from None
    if cfg is not None:
        return cfg
    if (org, ns) in page_namespaces():
        raise SystemExit(
            f"{org}/{ns} was added from the Repositories page and removed: an operator "
            "entry for this name, if any, is skipped, so list it under another namespace"
        )
    repos = configured_repos()
    if not repos:
        raise SystemExit(
            f"namespace {org}/{ns} is not a configured code repo entry and none is "
            "configured: set LOOM_CODE_REPO (with LOOM_CODE_ORG / LOOM_CODE_NAMESPACE) or "
            "LOOM_CODE_REPOS_JSON, or add the repository from the Repositories page."
        )
    if getattr(args, "namespace", None) and ns != repos[0].namespace:
        errors = config_errors_for(ns)
        why = (
            "; ".join(errors) + "."
            if errors
            else "Check LOOM_CODE_REPOS_JSON — a JSON parse failure degrades it to the "
            "scalar entry; quote the value with single quotes in .env."
        )
        raise SystemExit(
            f"namespace {org}/{ns} is not a configured code repo entry "
            f"(configured: {', '.join(r.namespace for r in repos)}). {why}"
        )
    return repos[0]


async def _then_disconnect(coro: asyncio.Future[int] | Coroutine[Any, Any, int]) -> int:
    """Await one subcommand, then disconnect the shared client it connected."""
    from server import boot
    from server.core.background import register_buffer_drainer
    from server.core.ch import get_client
    from server.insights.app.provider_command import model_command

    register_buffer_drainer("boot-writes", boot.drain_writes)
    started = False
    try:
        # A repository added from the Repositories page lives in the registry, not in
        # LOOM_CODE_REPOS_JSON: read it before the subcommand resolves its namespace.
        from server.code.app.index.repo_registry import refresh_db_repos
        from server.code.domain.index.repos import RegistryUnread

        await get_client().connect()
        async with model_command():
            # Never raises; while it has never succeeded, `configured_repos` refuses.
            await refresh_db_repos(force=True)
            started = True
            try:
                return int(await coro)
            except RegistryUnread:
                raise SystemExit(
                    "the self-service repository registry could not be read; retry"
                ) from None
            finally:
                # Some native commands close their own client. Reopen it for
                # buffered-write drain/retirement before leaving enrollment.
                await get_client().connect()
    finally:
        if not started and asyncio.iscoroutine(coro):
            coro.close()
        await get_client().disconnect()


def _run(name: str, coro: asyncio.Future[int] | Coroutine[Any, Any, int]) -> int:
    """Run one `loom code <name>` subcommand inside a root span `cli.code.<name>`.

    Scheduled runs (scip-full, scip-delta, backfill, repack) are observed
    through this: stderr logging with timestamps and levels (stdout is
    untouched; their logs and whoever greps them see the same lines), OTel
    spans + logs to the backend the server writes, and one root span per
    invocation carrying exit code, duration and whatever counters the
    subcommand `_note()`s. Metrics are NOT started in the CLI: the state
    gauges would be exported a second time with the same values.
    """
    _setup_cli_logging()
    started = time.monotonic()
    rc = 1
    otel_mod: Any = None
    try:
        from server.observability import otel as _otel

        _otel.init_otel(None, enable_metrics=False)
        otel_mod = _otel
    except Exception as e:  # noqa: BLE001 — telemetry never blocks an operator
        log.debug("cli: otel init skipped: %s", e)
    # Provider spans (embed.provider, llm.code_describe) name the command as
    # their loom.operation; a CLI run has no request to take it from.
    try:
        from server.observability import genai as _genai

        _genai.set_identity(operation=f"cli.code.{name}")
    except Exception:  # noqa: BLE001 — span attribution is optional; the command runs without it
        pass
    span_cm = _cli_span(f"cli.code.{name}") if otel_mod is not None else None
    span = span_cm.__enter__() if span_cm is not None else None
    log.info("cli.code.%s: start", name)
    try:
        # What a `loom code` command (a host cron job) writes under the code repos root,
        # the containers must be able to write too.
        with repo_umask():
            rc = asyncio.run(cancel_on_sigterm(_then_disconnect(coro)))
        return rc
    except BaseException as e:
        rc = 130 if isinstance(e, KeyboardInterrupt) else 1
        if isinstance(e, Terminated):
            rc = TERMINATED_EXIT
        _stamp(span, "loom.cli.error", f"{type(e).__name__}: {e}"[:300])
        if isinstance(e, Terminated):
            log.warning("cli.code.%s: terminated", name)
            raise SystemExit(rc) from None
        if not isinstance(e, KeyboardInterrupt | SystemExit):  # an exit says its own why
            log.exception("cli.code.%s: failed", name)
        raise
    finally:
        _close_run_span(name, span_cm, span, otel_mod, rc=rc, started=started)


def _close_run_span(
    name: str, span_cm: Any, span: Any, otel_mod: Any, *, rc: int, started: float
) -> None:
    """Stamp the root span with the exit code and duration, close it, and
    flush telemetry before the process exits. Telemetry never changes `rc`."""
    duration_ms = int((time.monotonic() - started) * 1000)
    _stamp(span, "loom.cli.exit_code", rc)
    _stamp(span, "loom.cli.status", "ok" if rc == 0 else "error")
    _stamp(span, "loom.cli.duration_ms", duration_ms)
    if rc != 0 and span is not None:
        try:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, f"exit {rc}"))
        except Exception:  # noqa: BLE001 — telemetry never changes the exit code
            pass
    log.info("cli.code.%s: exit %d after %d ms", name, rc, duration_ms)
    if span_cm is not None:
        try:
            span_cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 — telemetry never changes the exit code
            pass
    if otel_mod is not None:
        try:
            otel_mod.shutdown()  # flush spans + logs before the process exits
        except Exception:  # noqa: BLE001 — an unflushed span never changes the exit code
            pass


def _cli_span(name: str) -> Any:
    """Root span for a CLI invocation (a no-op span when OTel is off)."""
    import contextlib

    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("loom.cli")

        @contextlib.contextmanager
        def _cm() -> Iterator[Any]:
            with tracer.start_as_current_span(name) as span:
                _stamp(span, "loom.operation", name)
                _stamp(span, "langfuse.trace.name", name)
                yield span

        return _cm()
    except Exception:  # noqa: BLE001 — without a tracer the command runs unspanned
        return contextlib.nullcontext()


def _stamp(span: Any, key: str, value: Any) -> None:
    if span is None:
        return
    try:
        span.set_attribute(key, value)
    except Exception:  # noqa: BLE001 — a span attribute never fails the command
        pass


def _note(**counters: Any) -> None:
    """Attach subcommand counters (files, symbols, edges, ...) to the CLI span.

    Called at the end of a subcommand with whatever it already prints; each
    lands as `loom.cli.<key>` on the `cli.code.<name>` root span.
    """
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        for k, v in counters.items():
            if isinstance(v, bool | int | float):
                span.set_attribute(f"loom.cli.{k}", v)
            elif isinstance(v, str) and v:
                span.set_attribute(f"loom.cli.{k}", v[:200])
    except Exception:  # noqa: BLE001 — a span counter never fails the command
        pass


_LOGGING_CONFIGURED = False


def _setup_cli_logging() -> None:
    """Timestamps + levels on stderr for every logger, once.

    Deliberately not `server.core.logging_config.configure_logging()`: that one
    writes to STDOUT, and a scheduled run appends stdout to its log file and
    operators grep those lines (`RESULT:`, `applied:`, `scip_commit ->`).
    Logging goes to stderr so the stdout contract is untouched; a `2>&1`
    lands both in the same file.
    """
    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return
    _LOGGING_CONFIGURED = True
    try:
        from server.core.logging_config import _TextFormatter
        from server.settings import settings

        root = logging.getLogger()
        if not any(
            isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) is sys.stderr
            for h in root.handlers
        ):
            handler = logging.StreamHandler(stream=sys.stderr)
            handler.setFormatter(_TextFormatter(_TextFormatter.DEFAULT_FMT))
            root.addHandler(handler)
        level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
        root.setLevel(min(level, logging.INFO))
        for noisy in ("httpx", "httpcore", "clickhouse_connect", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    except Exception:  # noqa: BLE001 — logging falls back to basicConfig, never blocks
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
