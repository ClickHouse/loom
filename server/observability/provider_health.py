"""Whether the upstream providers are actually answering, not just configured.

A configured provider can be dead (every call a 429 `insufficient_quota`), and
"is a real provider configured" cannot see that. This module answers "is it
working" from the outcomes of real calls.

Design constraints:

* **No probing from the health endpoint.** Health is polled constantly, and an
  embedding call per poll would cost real money and rate limit. So this records
  the outcome of calls the server was making anyway.
* **A stale failure must not peg the flag forever.** Degraded means "the most
  recent thing we know is a failure": a later success clears it, no timeout
  required. That way recovery is reported as promptly as breakage.
* **Never raise.** This is observability on the request path; a bug here must not
  turn a working call into a failed one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from server.core.errors import ModelCallRefused, error_kind

_lock = threading.Lock()
# Set while a candidate configuration is probed: its outcome says nothing about
# the providers the server is running on.
_unrecorded: ContextVar[bool] = ContextVar("provider_health_unrecorded", default=False)
# provider -> {"ok_at": float, "err_at": float, "err": str}
_state: dict[str, dict[str, Any]] = {}


def _touch(provider: str) -> dict[str, Any]:
    return _state.setdefault(provider, {"ok_at": 0.0, "err_at": 0.0, "err": "", "failing": False})


def record_success(provider: str) -> None:
    """A call to `provider` returned normally."""
    if _unrecorded.get():
        return
    try:
        with _lock:
            s = _touch(provider)
            s["ok_at"] = time.time()
            s["failing"] = False
    except Exception:  # noqa: BLE001 — telemetry must not break a working call
        pass


def _shown(err: BaseException) -> str:
    """An error as `/v1/health` (no token) shows it: a Loom error's code or another
    error's class, and the HTTP status of a provider's refusal. Its text, a Loom
    error's message included, can name a private gateway's address: logged by the
    caller, not kept here."""
    status = getattr(getattr(err, "response", None), "status_code", None)
    kind = error_kind(err)
    return f"{kind} (HTTP {status})" if isinstance(status, int) else kind


def record_failure(provider: str, err: BaseException | str) -> None:
    """A call to `provider` failed. Keeps a bounded description, not a traceback."""
    if _unrecorded.get():
        return
    try:
        text = err if isinstance(err, str) else _shown(err)
        with _lock:
            s = _touch(provider)
            s["err_at"] = time.time()
            s["err"] = text[:200]
            s["failing"] = True
    except Exception:  # noqa: BLE001 — telemetry must not break the failing call's own error path
        pass


@contextmanager
def recording(provider: str) -> Iterator[None]:
    """Record the outcome of the provider call made inside the block: a
    normal exit is a success, an `Exception` a failure (re-raised unchanged).
    A cancellation (the caller's deadline, a client gone) is not the
    provider's failure and records nothing, nor is the gateway's refusal of
    one identity (`ModelCallRefused`), which says nothing of the provider.
    Every provider client wraps its call in this, so a configured provider
    that keeps failing reads degraded."""
    try:
        yield
    except ModelCallRefused:
        raise
    except Exception as exc:
        record_failure(provider, exc)
        raise
    record_success(provider)


@contextmanager
def unrecorded() -> Iterator[None]:
    """Calls made inside the block leave the health state untouched."""
    token = _unrecorded.set(True)
    try:
        yield
    finally:
        _unrecorded.reset(token)


def reset() -> None:
    """For tests."""
    with _lock:
        _state.clear()


def status() -> dict[str, Any]:
    """Per-provider view, plus a single `degraded` verdict.

    A provider is degraded when its last known outcome was a failure. Deciding by
    the last outcome rather than a TTL means a recovery clears the flag on the
    next successful call, and a provider that has simply been idle is not
    reported as broken.
    """
    try:
        now = time.time()
        with _lock:
            snapshot = {k: dict(v) for k, v in _state.items()}
        out: dict[str, Any] = {}
        degraded = False
        for provider, s in snapshot.items():
            # The last recorded outcome, in lock order: two records in one clock
            # tick would compare equal as timestamps.
            failing = bool(s["failing"])
            degraded = degraded or failing
            entry: dict[str, Any] = {"failing": failing}
            if s["err_at"]:
                entry["last_error"] = s["err"]
                entry["last_error_age_s"] = round(now - s["err_at"], 1)
            if s["ok_at"]:
                entry["last_ok_age_s"] = round(now - s["ok_at"], 1)
            out[provider] = entry
        return {"degraded": degraded, "providers": out}
    except Exception:  # noqa: BLE001 — health reads as not degraded rather than failing
        return {"degraded": False, "providers": {}}
