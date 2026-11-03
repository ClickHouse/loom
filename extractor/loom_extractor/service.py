"""The reference extractor's HTTP service, version 1 of the wire contract
(docs/reference/extractor-contract.md):

- `GET /v1/capabilities`: the formats this extractor parses and the options
  each takes.
- `POST /v1/extract?format=<f>&name=<file name>[&encoding=..][&sql_dialect=..]`:
  the original's bytes in the body, the artifact JSON back. A source it cannot
  extract is a 422 with `{"error": {"code", "message"}}`.
- `GET /healthz`: liveness, without the token.

Every `/v1` call needs `Authorization: Bearer <LOOM_EXTRACTOR_TOKEN>`. Each
extraction runs in a child process (`child`) under its own memory and CPU
limits, killed at the wall-clock timeout or when its output passes the
artifact bound; at most `concurrency` (1 by default) read their source and run
at once, and the rest wait. The service and every child are non-dumpable, so
a child cannot read the token through /proc; they set no-new-privileges, so
no exec gives a child a privilege; a child can start no process, so none
outlives its extraction; and a child can make no socket (seccomp), so one a
hostile source takes over reaches no address while it runs alone (concurrency 1,
the default; see `Config.concurrency`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from loom_extractor import child
from loom_extractor.source import PARSED_FORMATS

logger = logging.getLogger("loom_extractor")

CONTRACT_VERSION = 1
NAME = "loom-reference"
VERSION = "1"  # the service; each artifact names its own parser and parser_version
_FORMAT_OPTIONS = {fmt: ["encoding"] for fmt in PARSED_FORMATS} | {
    "sql": ["encoding", "sql_dialect"]
}
_READ_CHUNK = 64 * 1024
_REAP_S = 5.0  # how long a stopped child's pipes may stay open
_STDERR_TAIL = 4096
# The directory holding `loom_extractor`, for a child started with no environment,
# and the module the child runs.
_PACKAGE_ROOT = str(Path(child.__file__).resolve().parent.parent)
_CHILD_MODULE = "loom_extractor.child"


@dataclass(frozen=True)
class Config:
    token: str
    timeout_s: float = 120.0
    memory_mb: int = 1024
    # One at a time by default: a child is readable by a process of its user until
    # it has made itself non-dumpable, so a second child running beside it could
    # read its source then. Scale out with more service replicas instead.
    concurrency: int = 1
    max_bytes: int = 64 * 1024 * 1024
    max_artifact_bytes: int = 64 * 1024 * 1024

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Config:
        """The service's settings from `LOOM_EXTRACTOR_*`. A token is required:
        the service refuses to start without one of at least 16 characters, or
        with a limit that is not above 0."""
        token = env.get("LOOM_EXTRACTOR_TOKEN", "")
        if len(token) < 16:
            raise SystemExit("LOOM_EXTRACTOR_TOKEN must be set, at least 16 characters")
        d = cls(token=token)
        config = cls(
            token=token,
            timeout_s=float(env.get("LOOM_EXTRACTOR_TIMEOUT_S", d.timeout_s)),
            memory_mb=int(env.get("LOOM_EXTRACTOR_MEMORY_MB", d.memory_mb)),
            concurrency=int(env.get("LOOM_EXTRACTOR_CONCURRENCY", d.concurrency)),
            max_bytes=int(env.get("LOOM_EXTRACTOR_MAX_BYTES", d.max_bytes)),
            max_artifact_bytes=int(
                env.get("LOOM_EXTRACTOR_MAX_ARTIFACT_BYTES", d.max_artifact_bytes)
            ),
        )
        for name in ("timeout_s", "memory_mb", "concurrency", "max_bytes", "max_artifact_bytes"):
            value = getattr(config, name)
            if not math.isfinite(value) or value <= 0:  # 0 slots would leave every call waiting
                raise SystemExit(f"LOOM_EXTRACTOR_{name.upper()} must be a number above 0")
        return config


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class _Refused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


async def _read_capped(
    stream: asyncio.StreamReader, cap: int, stop: Callable[[], None]
) -> bytes | None:
    """The stream to its end, or None once it passes `cap`: the child is stopped
    then and the rest read and dropped, so the pipe closes."""
    out: bytearray | None = bytearray()
    while chunk := await stream.read(_READ_CHUNK):
        if out is None:
            continue
        out += chunk
        if len(out) > cap:
            out = None
            stop()
    return None if out is None else bytes(out)


async def _tail(stream: asyncio.StreamReader) -> bytes:
    tail = b""
    while chunk := await stream.read(_READ_CHUNK):
        tail = (tail + chunk)[-_STDERR_TAIL:]
    return tail


async def run_child(config: Config, data: bytes, request: dict[str, object]) -> bytes:
    """The artifact JSON of one extraction, or _Refused. The child runs in `/`
    with no environment beyond what Python needs to find this package, no
    directory of its own on `sys.path` (`-P`), and only its limits on its
    command line; the request and the bytes go on stdin."""
    limits = {
        "memory_bytes": config.memory_mb * 1024 * 1024,
        "cpu_s": max(1, int(config.timeout_s)),
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        "-s",
        "-P",
        "-m",
        _CHILD_MODULE,
        json.dumps(limits),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/",
        env={"PYTHONPATH": _PACKAGE_ROOT, "PATH": os.defpath, "LC_ALL": "C.UTF-8"},
        start_new_session=True,
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    assert stdin is not None and stdout is not None and stderr is not None

    async def feed() -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(json.dumps(request).encode() + b"\n")
            stdin.write(data)
            await stdin.drain()
            stdin.close()

    def stop() -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)

    async def run() -> tuple[bytes | None, bytes, int]:
        _, out, err = await asyncio.gather(
            feed(), _read_capped(stdout, config.max_artifact_bytes, stop), _tail(stderr)
        )
        return out, err, await proc.wait()

    try:
        out, err, code = await asyncio.wait_for(run(), config.timeout_s)
    except TimeoutError as e:
        raise _Refused("timeout", f"the extraction took over {config.timeout_s:g} s") from e
    finally:
        # On a timeout or a cancel the child is still running: it is stopped, and what
        # it left in the pipes read so they close and `wait` returns, within a bound.
        if proc.returncode is None:
            stop()
            try:
                async with asyncio.timeout(_REAP_S):
                    await asyncio.gather(stdout.read(), stderr.read())
                    await proc.wait()
            except TimeoutError:
                logger.error("extraction %s: its pipes stayed open after it was stopped", proc.pid)
    if out is None:
        raise _Refused("resource_limit", f"the artifact is over {config.max_artifact_bytes} bytes")
    if code == 0:
        return out
    if code == child.EXIT_REFUSED:
        refused = json.loads(out)["error"]
        raise _Refused(refused["code"], refused["message"])
    if code < 0:  # a signal: the CPU limit's SIGXCPU, or the kernel's at the memory limit
        raise _Refused("resource_limit", f"the extraction was stopped by signal {-code}")
    # Not the parser (a parser's exception is the child's own `corrupt`): a fault
    # before it ran, such as a refused prctl or a missing import, worth a retry.
    logger.error("extraction exited %s: %s", code, err.decode(errors="replace"))
    raise RuntimeError(f"the extraction exited {code}")


async def _disconnected(request: Request) -> None:
    """Returns when the caller disconnects. Called once the body is read, so
    every message left is the disconnect."""
    while (await request.receive())["type"] != "http.disconnect":
        pass


def create_app(config: Config) -> Starlette:
    slots = asyncio.Semaphore(config.concurrency)
    bearer = f"Bearer {config.token}".encode()

    def authorized(request: Request) -> bool:
        return hmac.compare_digest(request.headers.get("authorization", "").encode(), bearer)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def capabilities(request: Request) -> Response:
        if not authorized(request):
            return _error(401, "unauthorized", "a valid bearer token is required")
        return JSONResponse(
            {
                "contract_version": CONTRACT_VERSION,
                "extractor": {"name": NAME, "version": VERSION},
                "max_bytes": config.max_bytes,
                "formats": [
                    {"format": fmt, "options": opts} for fmt, opts in _FORMAT_OPTIONS.items()
                ],
            }
        )

    async def extract(request: Request) -> Response:
        if not authorized(request):
            return _error(401, "unauthorized", "a valid bearer token is required")
        params = dict(request.query_params)
        fmt, name = params.pop("format", ""), params.pop("name", "")
        if fmt not in _FORMAT_OPTIONS:
            return _error(422, "format_unavailable", f"this extractor does not parse {fmt!r}")
        unknown = sorted(set(params) - set(_FORMAT_OPTIONS[fmt]))
        if unknown:
            return _error(422, "invalid_option", f"{fmt} takes no option {unknown[0]!r}")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > config.max_bytes:
            return _error(422, "too_large", f"the source is over {config.max_bytes} bytes")
        # The body is read under the slot, so `concurrency` bounds the originals held
        # in memory as well as the children.
        async with slots:
            body = bytearray()
            async for chunk in request.stream():
                body += chunk
                if len(body) > config.max_bytes:
                    return _error(422, "too_large", f"the source is over {config.max_bytes} bytes")
            extraction = asyncio.ensure_future(
                run_child(config, bytes(body), {"format": fmt, "name": name, "options": params})
            )
            gone = asyncio.ensure_future(_disconnected(request))
            try:
                await asyncio.wait({extraction, gone}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                # The caller left, or this handler was cancelled: the child is stopped
                # before the slot is given back, so no second child runs beside it.
                gone.cancel()
                if not extraction.done():
                    extraction.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await extraction
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise asyncio.CancelledError  # the handler's own cancel goes on
            if extraction.cancelled():
                return _error(499, "client_gone", "the caller disconnected")
            try:
                out = extraction.result()
            except _Refused as e:
                return _error(422, e.code, str(e))
            except RuntimeError as e:
                return _error(500, "extractor_error", str(e))
        return Response(out, media_type="application/json")

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/v1/capabilities", capabilities),
            Route("/v1/extract", extract, methods=["POST"]),
        ]
    )
