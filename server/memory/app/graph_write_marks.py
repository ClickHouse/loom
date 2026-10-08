"""When a namespace's entity graph last moved, and which projections of it are in flight.

`facts.traverse` remembers a walk for a minute. A walk read before a graph write must not
be served after it, and a walk must not start while a projection its caller just wrote is
still landing. One process knows its own writes; a server runs several web workers (and a
workers container beside them), and a client's `memory.set` and its next walk rarely land
on the same one. So the marks are kept twice:

- in-process, always: a dict of (org, namespace) -> wall-clock ns of the last graph write;
- in a directory every process of the server shares, when enabled: per namespace, a
  `written` file whose mtime is the last graph write, and one `pending-<pid>-<n>` file per
  projection in flight. A read is a `stat` and a `scandir` of a small directory, no
  ClickHouse round trip.

The shared directory is best effort. When it is missing, cannot be created or a call on it
fails, the marks fall back to the process-local ones (logged once): the walk then reads its
own process's writes, as a single worker does. A pending file older than
`STALE_PENDING_S` (a process that died mid-projection) is ignored and removed.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import logging
import os
import stat
import time

log = logging.getLogger("server.memory.app.graph_write_marks")

STALE_PENDING_S = 30.0
_POLL_S = 0.025
_LOCAL_MAX = 4096
_WRITTEN = "written"
_PENDING = "pending-"
_DIR_MODE = 0o700
_FILE_MODE = 0o600


class GraphWriteMarks:
    """The marks of one process. `shared_dir` None = process-local only."""

    def __init__(self, shared_dir: str | None) -> None:
        self._local: dict[tuple[str, str], int] = {}
        self._dir = shared_dir or None
        self._seq = itertools.count()
        self._warned = False

    @property
    def shared_dir(self) -> str | None:
        return self._dir

    # -- shared directory -------------------------------------------------

    def _fail(self, what: str, e: OSError) -> None:
        if not self._warned:
            self._warned = True
            log.warning(
                "graph write marks: %s failed under %s (%s); walks fall back to this "
                "process's own writes",
                what,
                self._dir,
                e,
            )

    def _ns_dir(self, org: str, namespace: str, *, create: bool) -> str | None:
        if self._dir is None:
            return None
        digest = hashlib.sha256(f"{org}\0{namespace}".encode()).hexdigest()[:32]
        path = os.path.join(self._dir, digest)
        if create:
            os.makedirs(path, mode=_DIR_MODE, exist_ok=True)
        return path

    @staticmethod
    def _touch(path: str, at_ns: int) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, _FILE_MODE)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"{path}: not a regular file")
            os.utime(fd, ns=(at_ns, at_ns))
        finally:
            os.close(fd)

    # -- writes -----------------------------------------------------------

    def note_write(self, org: str, namespace: str, *, at_ns: int | None = None) -> None:
        """A graph write to (org, namespace) landed now (or at `at_ns`). Never raises."""
        if not org or not namespace:
            return
        at = time.time_ns() if at_ns is None else at_ns
        key = (org, namespace)
        if key not in self._local and len(self._local) >= _LOCAL_MAX:
            # Drop the oldest quarter; a dropped mark only means one walk is remembered
            # that could have been re-read.
            for k in sorted(self._local, key=self._local.__getitem__)[: _LOCAL_MAX // 4]:
                self._local.pop(k, None)
        self._local[key] = max(at, self._local.get(key, 0))
        try:
            d = self._ns_dir(org, namespace, create=True)
            if d is None:
                return
            path = os.path.join(d, _WRITTEN)
            try:
                if os.stat(path, follow_symlinks=False).st_mtime_ns >= at:
                    return  # another process marked a later write
            except FileNotFoundError:
                pass
            self._touch(path, at)
        except OSError as e:
            self._fail("marking a write", e)

    def begin_pending(self, org: str, namespace: str) -> str | None:
        """A projection of (org, namespace) is about to be written: returns the token
        `end_pending` takes, or None when nothing is shared."""
        if self._dir is None or not org or not namespace:
            return None
        try:
            d = self._ns_dir(org, namespace, create=True)
            if d is None:
                return None
            path = os.path.join(d, f"{_PENDING}{os.getpid()}-{next(self._seq)}")
            self._touch(path, time.time_ns())
            return path
        except OSError as e:
            self._fail("marking a projection in flight", e)
            return None

    def end_pending(self, token: str | None) -> None:
        if not token:
            return
        try:
            os.unlink(token)
        except FileNotFoundError:
            pass
        except OSError as e:
            self._fail("clearing a projection in flight", e)

    # -- reads ------------------------------------------------------------

    def written_at_ns(self, org: str, namespace: str) -> int | None:
        """Wall-clock ns of the last graph write to (org, namespace) by any process that
        shares the marks; None when none is recorded."""
        local = self._local.get((org, namespace))
        shared: int | None = None
        try:
            d = self._ns_dir(org, namespace, create=False)
            if d is not None:
                shared = os.stat(os.path.join(d, _WRITTEN), follow_symlinks=False).st_mtime_ns
        except FileNotFoundError:
            shared = None
        except OSError as e:
            self._fail("reading the last write", e)
        if local is None:
            return shared
        return local if shared is None else max(local, shared)

    def pending(self, org: str, namespace: str, *, now_ns: int | None = None) -> int:
        """Projections of (org, namespace) in flight in any process sharing the marks."""
        if self._dir is None:
            return 0
        now = time.time_ns() if now_ns is None else now_ns
        stale_before = now - int(STALE_PENDING_S * 1e9)
        n = 0
        try:
            d = self._ns_dir(org, namespace, create=False)
            if d is None:
                return 0
            with os.scandir(d) as it:
                for entry in it:
                    if not entry.name.startswith(_PENDING):
                        continue
                    try:
                        mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                    except FileNotFoundError:
                        continue  # finished while we looked
                    if mtime < stale_before:
                        self.end_pending(entry.path)  # its process died mid-projection
                        continue
                    n += 1
        except FileNotFoundError:
            return 0
        except OSError as e:
            self._fail("listing projections in flight", e)
            return 0
        return n

    async def wait_pending(self, org: str, namespace: str, *, timeout_s: float) -> int:
        """Wait (at most `timeout_s`) until no process has a projection of (org, namespace)
        in flight; returns how many were in flight at the start."""
        first = self.pending(org, namespace)
        if not first or timeout_s <= 0:
            return first
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())))
            if not self.pending(org, namespace):
                break
        return first

    def forget_local(self) -> None:
        """Tests."""
        self._local.clear()


def _default_dir() -> str:
    from server.core.ratelimit_shared import runtime_dir

    return os.path.join(runtime_dir(), f"loom-graph-marks-{os.getppid()}")


def _resolve_dir() -> str | None:
    from server.settings import settings

    mode = str(settings.graph_write_marks_shared)
    configured = str(settings.graph_write_marks_dir)
    workers = max(1, int(settings.web_workers))
    if mode == "off" or (mode == "auto" and not configured and workers <= 1):
        return None
    path = configured or _default_dir()
    try:
        os.makedirs(path, mode=_DIR_MODE, exist_ok=True)
    except OSError as e:
        log.warning(
            "graph write marks: cannot use %s (%s); walks read this process's own writes",
            path,
            e,
        )
        return None
    return path


_marks: GraphWriteMarks | None = None


def marks() -> GraphWriteMarks:
    """This process's marks, resolved from settings on first use."""
    global _marks
    if _marks is None:
        _marks = GraphWriteMarks(_resolve_dir())
    return _marks


def _reset_for_tests(instance: GraphWriteMarks | None = None) -> None:
    global _marks
    _marks = instance


__all__ = ["STALE_PENDING_S", "GraphWriteMarks", "marks"]
