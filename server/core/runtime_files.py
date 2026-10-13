"""Files one server's web processes share, in a per-user runtime directory, and
how they are opened (`open_private`).

The workers of one `uvicorn --workers N` share a parent, so a file is named by
the parent's pid namespace, pid and start time: two servers on one host get two
files, a later server that reuses the pid (a restarted container) starts afresh,
and two containers sharing the directory never take each other's. Files of a
server provably gone from this pid namespace are removed the first time this
process names one; files of other namespaces are left alone.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import stat
import sys
from pathlib import Path

FILE_MODE = 0o600
RUN_DIR = "/run/loom"
_TMP_DIR = "/tmp"
# loom-<name>-<pid namespace>-<pid>-<start time>, then any suffix (".json", ".lock", ...).
_OWNED = re.compile(r"^loom-[a-z-]+-(\d+)-(\d+)-(\d+)(?:\..*)?$")
# Directories this process has pruned: once each is enough.
_pruned: set[str] = set()


def runtime_dir() -> str:
    """`LOOM_RUNTIME_DIR` when set: `scripts/serve.sh` resolves it once, before
    the workers start, so every one of them names the same files. Else
    `choose_runtime_dir()`. Either way a directory only this user can enter
    (a pinned one may since have turned read-only); raises OSError when not."""
    from server.settings import settings

    pinned = str(settings.runtime_dir).strip()
    if not pinned:
        return choose_runtime_dir()
    if not _private_dir(pinned):
        raise OSError(f"LOOM_RUNTIME_DIR={pinned} is not a directory only this user can enter")
    return pinned


def choose_runtime_dir() -> str:
    """`$XDG_RUNTIME_DIR/loom` when the variable is set, else `/run/loom`, else
    `/tmp/loom-<uid>`: the first that is, or can be made, a directory only this
    user can enter, so no other account can plant a file where the server's
    names point. Raises OSError when none is."""
    xdg = str(os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    candidates = [os.path.join(xdg, "loom")] if xdg else []
    candidates += [RUN_DIR, os.path.join(_TMP_DIR, f"loom-{os.geteuid()}")]
    for base in candidates:
        if _private_dir(base) and os.access(base, os.W_OK | os.X_OK):
            return base
    raise OSError(f"none of {', '.join(candidates)} is a directory only this user can enter")


def _private_dir(path: str) -> bool:
    """Whether `path` is, or is now, a directory of this user's that no other
    account can enter (a looser mode of its own is tightened)."""
    with contextlib.suppress(OSError):
        os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            return False
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.chmod(path, 0o700)
    except OSError:
        return False
    return True


def _started(pid: int) -> str | None:
    """The process's start time (field 22 of /proc/<pid>/stat); '' when no such
    process runs, None when /proc cannot say (unreadable)."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return ""
    except OSError:
        return None
    return fields.rsplit(")", 1)[1].split()[19]


def _pid_namespace() -> str:
    """This process's pid namespace (the inode of /proc/self/ns/pid); '' when unknown."""
    try:
        return re.sub(r"\D", "", os.readlink("/proc/self/ns/pid"))
    except OSError:
        return ""


def server_file(name: str) -> str:
    """`<runtime dir>/loom-<name>-<pid namespace>-<parent pid>-<parent start time>`."""
    base = runtime_dir()
    namespace = _pid_namespace()
    if namespace and base not in _pruned:
        _pruned.add(base)
        _prune(base, namespace)
    ppid = os.getppid()
    return os.path.join(base, f"loom-{name}-{namespace or 0}-{ppid}-{_started(ppid) or 0}")


def _prune(base: str, namespace: str) -> None:
    """Remove this user's files of servers that `namespace`'s /proc shows are gone."""
    with contextlib.suppress(OSError):
        for entry in os.scandir(base):
            owned = _OWNED.match(entry.name)
            if (
                owned is None
                or owned.group(1) != namespace
                or owned.group(2) == "0"
                or owned.group(3) == "0"
            ):
                continue  # another namespace's, or named without a pid and start to check
            started = _started(int(owned.group(2)))
            if started is None or started == owned.group(3):
                continue  # running, or unknown: keep it
            with contextlib.suppress(OSError):
                if entry.stat(follow_symlinks=False).st_uid != os.geteuid():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.unlink(entry.path)


def open_private(path: str, flags: int) -> int:
    """Open `path` never through a symlink, as a regular file owned by this
    process, created (with O_CREAT) or tightened to 0600. Raises OSError.
    O_NONBLOCK until it is known to be a file: opening a FIFO would wait for
    a writer."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, FILE_MODE)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{path}: not a regular file")
        if st.st_uid != os.geteuid():
            raise OSError(f"{path}: owned by uid {st.st_uid}, this process is {os.geteuid()}")
        os.set_blocking(fd, True)
        if stat.S_IMODE(st.st_mode) & 0o077:
            os.fchmod(fd, FILE_MODE)
    except BaseException:
        os.close(fd)
        raise
    return fd


if __name__ == "__main__":
    try:
        print(runtime_dir())
    except OSError as e:
        print(f"loom: {e}", file=sys.stderr)
        raise SystemExit(2) from None
