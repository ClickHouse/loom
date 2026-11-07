"""Whether this process should be sent traffic: `GET /v1/health/ready`.

A registry of named checks. A module that owns a piece of warm-up state
defines its check (a `readiness_check` function beside that state), and the
lifespan registers them (`server.boot.register_readiness_checks`), so the
health route imports no subsystem. A check returns a `CheckResult`, sync or async; it should
read in-memory state, and one that does I/O sets `ttl_s` so its answer is
cached and a probe every second costs nothing. A check that raises or takes
longer than `readiness_check_timeout_s` fails.

`required=False` checks are reported but never make the process unready.

Draining is part of readiness: on SIGTERM the process answers 503 here while
it keeps serving for `LOOM_SHUTDOWN_READY_DELAY_S`, so the load balancer
takes it out of rotation before its port closes.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from server.core.errors import shown_unauthenticated
from server.settings import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str = ""


def result(ok: bool, why_not: str) -> CheckResult:
    """A check's answer, carrying `why_not` only when it failed."""
    return CheckResult(ok, "" if ok else why_not)


Check = Callable[[], CheckResult | Awaitable[CheckResult]]


@dataclass
class _Entry:
    name: str
    check: Check
    required: bool
    ttl_s: float
    at: float = 0.0
    last: CheckResult | None = None
    lock: asyncio.Lock | None = None


_CHECKS: dict[str, _Entry] = {}
_draining = False


def register(name: str, check: Check, *, required: bool = True, ttl_s: float = 0.0) -> None:
    """Add (or replace) the check `name`."""
    _CHECKS[name] = _Entry(name=name, check=check, required=required, ttl_s=ttl_s)


def names() -> list[str]:
    return sorted(_CHECKS)


def begin_drain() -> None:
    """From now on this process answers not-ready (it is shutting down)."""
    global _draining
    _draining = True


def draining() -> bool:
    return _draining


async def _run(entry: _Entry) -> CheckResult:
    try:
        out = entry.check()
        if inspect.isawaitable(out):
            out = await asyncio.wait_for(out, timeout=settings.readiness_check_timeout_s)
        return out
    except TimeoutError:
        return CheckResult(False, f"no answer within {settings.readiness_check_timeout_s:g} s")
    except Exception as e:  # noqa: BLE001 — a failing check is an unready answer, not a 500
        # `/v1/health/ready` answers without a token: the reason is the error's kind.
        return CheckResult(False, shown_unauthenticated(log, f"readiness check {entry.name}", e))


async def _cached(entry: _Entry) -> CheckResult:
    if entry.ttl_s <= 0:
        return await _run(entry)
    if entry.lock is None:
        entry.lock = asyncio.Lock()
    async with entry.lock:  # concurrent probes share one evaluation
        if entry.last is None or time.monotonic() - entry.at >= entry.ttl_s:
            entry.last = await _run(entry)
            entry.at = time.monotonic()
        return entry.last


@dataclass(frozen=True)
class Readiness:
    checks: dict[str, CheckResult]
    required: frozenset[str]

    @property
    def failing(self) -> list[str]:
        return [n for n, r in self.checks.items() if not r.ok and n in self.required]

    @property
    def ready(self) -> bool:
        return not self.failing

    def as_dict(self) -> dict[str, object]:
        checks: dict[str, dict[str, object]] = {}
        for name, r in self.checks.items():
            checks[name] = {"ok": r.ok}
            if r.reason:
                checks[name]["reason"] = r.reason
            if name not in self.required:
                checks[name]["required"] = False
        return {"ready": self.ready, "failing": self.failing, "checks": checks}


async def evaluate() -> Readiness:
    """Every registered check, concurrently, plus `draining`."""
    entries = dict(_CHECKS)
    results = await asyncio.gather(*(_cached(e) for e in entries.values()))
    checks = {"draining": result(not _draining, "shutting down")}
    checks.update(zip(entries, results, strict=True))
    required = frozenset({"draining", *(n for n, e in entries.items() if e.required)})
    return Readiness(checks=dict(sorted(checks.items())), required=required)


def _reset_for_tests() -> None:
    global _draining
    _CHECKS.clear()
    _draining = False
