"""Files one server's web processes share, in a per-user runtime directory.

The workers of one `uvicorn --workers N` share a parent, so a file is named by
the parent's pid and start time: two servers on one host get two files, and a
later server that reuses the pid (a restarted container) starts afresh. Files
of servers no longer running are removed the first time this process names one.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from pathlib import Path

RUN_DIR = "/run/loom"
_TMP_DIR = "/tmp"
# loom-<name>-<pid>-<start time>, then any suffix (".json", ".lock", ...).
_OWNED = re.compile(r"^loom-[a-z-]+-(\d+)-(\d+)(?:\..*)?$")
# Directories this process has pruned: once each is enough.
_pruned: set[str] = set()


def runtime_dir() -> str:
    """`$XDG_RUNTIME_DIR/loom` when the variable names a writable directory,
    else `/run/loom` when it exists or can be created, else `/tmp`. The first
    two are per-user or per-service tmpfs paths another local account cannot
    list; `/tmp` is the fallback for a container without one."""
    xdg = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    for base in ([os.path.join(xdg, "loom")] if xdg else []) + [RUN_DIR]:
        try:
            os.makedirs(base, mode=0o700, exist_ok=True)
            if os.access(base, os.W_OK | os.X_OK):
                return base
        except OSError:
            continue
    return _TMP_DIR


def _started(pid: int) -> str:
    """The process's start time (field 22 of /proc/<pid>/stat); '' when not running."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return ""
    return stat.rsplit(")", 1)[1].split()[19]


def server_file(name: str) -> str:
    """`<runtime dir>/loom-<name>-<parent pid>-<parent start time>`."""
    base = runtime_dir()
    if base not in _pruned:
        _pruned.add(base)
        _prune(base)
    ppid = os.getppid()
    return os.path.join(base, f"loom-{name}-{ppid}-{_started(ppid) or 0}")


def _prune(base: str) -> None:
    """Remove this user's server files whose server is no longer running
    (only where /proc says which processes are)."""
    if not os.path.isdir("/proc"):
        return
    with contextlib.suppress(OSError):
        for entry in os.scandir(base):
            owned = _OWNED.match(entry.name)
            if owned is None or _started(int(owned.group(1))) == owned.group(2):
                continue
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_uid != os.geteuid():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.unlink(entry.path)
