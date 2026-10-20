"""The one sync ClickHouse client every gauge probe reads through."""

from __future__ import annotations

import sys
import threading
from typing import Any

from server.core.ch_names import database, telemetry_database
from server.observability import otel_exporters as _exporters


def _probe_query_tag(fn_name: str) -> str:
    """`otel.probe.<name>` for a `_probe_<name>` gauge body, else `otel.probe`."""
    if fn_name.startswith("_probe_"):
        return f"otel.probe.{fn_name.removeprefix('_probe_')}"
    return "otel.probe"


# One sync client for every gauge probe. A fresh `_make_ch_client_for_otel()` per call would
# open a TLS connection pool to ClickHouse roughly twice a minute in each process and never
# close it, exhausting file descriptors (`[Errno 24] Too many open files`) within
# hours. The cache below hands the probes one client; a probe that raises drops it so the
# next call rebuilds. The factory is re-read on every call so a test that monkeypatches
# `otel_exporters._make_ch_client_for_otel` gets its stub, not a client another test cached.
_PROBE_CLIENT: dict[str, Any] = {"client": None, "factory": None}
_PROBE_LOCK = threading.Lock()
# One statement at a time through the shared session. The
# driver rejects concurrent queries on one client ("Attempt to execute
# concurrent queries within the same session"), and the three gauge caches
# (state 30 s, code 60 s, retention 900 s) refresh on their own threads. At a
# 60 s export every cache lapses on the same tick, so without the lock the
# code-plane probe would lose every race and its gauge series would never
# export. Holding the lock for the statement queues the probes instead.
_PROBE_QUERY_LOCK = threading.Lock()


class _ProbeClient:
    """Forwards `query` / `command` to the cached sync client; a failure
    closes and forgets that client so the next probe starts clean."""

    def _target(self) -> Any:
        factory = _exporters._make_ch_client_for_otel
        with _PROBE_LOCK:
            if _PROBE_CLIENT["client"] is None or _PROBE_CLIENT["factory"] is not factory:
                stale = _PROBE_CLIENT["client"]
                _PROBE_CLIENT["client"] = factory()
                _PROBE_CLIENT["factory"] = factory
            else:
                stale = None
        if stale is not None:
            _close_quietly(stale)
        return _PROBE_CLIENT["client"]

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        # Name the gauge body that issued the statement (the frame above
        # query/command), unless the caller set its own log_comment.
        per_query = dict(kwargs.get("settings") or {})
        per_query.setdefault("log_comment", _probe_query_tag(sys._getframe(2).f_code.co_name))
        kwargs["settings"] = per_query
        # This client's own database is ClickStack's: a probe that filters system
        # tables by Loom's databases names them as parameters, bound here.
        sql = args[0] if args and isinstance(args[0], str) else ""
        if "{loom_db:String}" in sql or "{telemetry_db:String}" in sql:
            kwargs["parameters"] = {
                "loom_db": database(),
                "telemetry_db": telemetry_database(),
                **dict(kwargs.get("parameters") or {}),
            }
        with _PROBE_QUERY_LOCK:
            client = self._target()
            try:
                return getattr(client, method)(*args, **kwargs)
            except Exception:
                _probe_client_reset()
                raise

    def query(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("query", *args, **kwargs)

    def command(self, *args: Any, **kwargs: Any) -> Any:
        return self._call("command", *args, **kwargs)

    def close(self) -> None:  # a probe body that closes is still correct
        _probe_client_reset()


def _close_quietly(client: Any) -> None:
    try:
        client.close()
    except Exception:  # noqa: BLE001 — closing a broken client must not raise
        pass


def _probe_client() -> _ProbeClient:
    """The shared sync client every gauge probe reads through."""
    return _ProbeClient()


def _probe_client_reset() -> None:
    """Close and forget the cached probe client (after a failure, or in tests)."""
    with _PROBE_LOCK:
        stale = _PROBE_CLIENT["client"]
        _PROBE_CLIENT["client"] = None
        _PROBE_CLIENT["factory"] = None
    if stale is not None:
        _close_quietly(stale)
