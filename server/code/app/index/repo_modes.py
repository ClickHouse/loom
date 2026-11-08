"""Modes for what Loom writes under the code repos root (`var/code_repos`).

The host operator and the containers (uid 999) can share that directory through a
group the operator names (`code_repos_shared_gid`): a mirror or checkout directory that
belongs to that group is 2770, so either side can fetch into and check out what the
other made, and a lock file is 0660 when the directory that holds it belongs to the
group. Any other one stays the owner's alone (0700): sharing is the operator's
decision, never read from a mode a directory inherited. Inside them git's files give
the group what they give the owner (its objects stay read-only). Every other user is
shut out, so a restricted entry stays unreadable to them."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from server.settings import settings

# What the server's user and its group may do; others get nothing.
REPO_UMASK = 0o007
# A mirror or checkout directory in the shared group, and outside it.
SHARED_DIR_MODE = 0o2770
PRIVATE_DIR_MODE = 0o700
# A lock file beside a mirror in the shared group, and outside it.
SHARED_LOCK_MODE = 0o660
PRIVATE_LOCK_MODE = 0o600


@contextmanager
def repo_umask() -> Iterator[None]:
    """The process umask is `REPO_UMASK` inside the block and the previous one after
    it, when `code_repos_shared_gid` names a group to share with; unset, the block
    keeps the caller's umask. Files that must stay the owner's alone set their mode
    explicitly."""
    if settings.code_repos_shared_gid is None:
        yield
        return
    previous = os.umask(REPO_UMASK)
    try:
        yield
    finally:
        os.umask(previous)


def _shared(gid: int) -> bool:
    """Whether `gid` is the group the operator shares the root with (never when unset)."""
    return gid == settings.code_repos_shared_gid


def repo_dir_mode(gid: int) -> int:
    """The mode of a mirror or checkout directory whose group is `gid`: `SHARED_DIR_MODE`
    when that is `code_repos_shared_gid` (the group those bits open it to), with the
    setgid bit so new entries keep that group even where an exact chmod once cleared
    it, and `PRIVATE_DIR_MODE` otherwise."""
    return SHARED_DIR_MODE if _shared(gid) else PRIVATE_DIR_MODE


def lock_file_mode(path: str) -> int:
    """The mode of a lock file created at `path`: `SHARED_LOCK_MODE` when the directory
    that holds it belongs to `code_repos_shared_gid` (as `repo_dir_mode` decides for a
    directory), else `PRIVATE_LOCK_MODE`."""
    gid = os.stat(os.path.dirname(os.path.abspath(path))).st_gid
    return SHARED_LOCK_MODE if _shared(gid) else PRIVATE_LOCK_MODE


def set_repo_dir_mode(path: str) -> None:
    """`repo_dir_mode` on the directory `path`. Sync: callers run it in a thread."""
    os.chmod(path, repo_dir_mode(os.stat(path).st_gid))
