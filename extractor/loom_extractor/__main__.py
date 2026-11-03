"""`python -m loom_extractor`: the service on LOOM_EXTRACTOR_HOST (127.0.0.1)
and LOOM_EXTRACTOR_PORT (8790), configured by `service.Config.from_env`."""

from __future__ import annotations

import os
import platform
from pathlib import Path

import uvicorn

from loom_extractor.child import NO_SOCKETS, no_dump, no_new_privileges
from loom_extractor.service import Config, create_app

# CAP_SYS_ADMIN and CAP_SYS_RESOURCE: either lifts the limit on new processes.
_LIFTING_CAPS = (21, 24)


def permitted_capabilities(status: str | None = None) -> int:
    """This process's permitted capability set, from /proc/self/status: what it
    or a child could raise into effect, ambient ones included."""
    text = Path("/proc/self/status").read_text() if status is None else status
    for line in text.splitlines():
        if line.startswith("CapPrm:"):
            return int(line.split()[1], 16)
    raise SystemExit("cannot read this process's capabilities from /proc/self/status")


def refuse_root() -> None:
    """The kernel does not hold root, or a process with CAP_SYS_ADMIN or
    CAP_SYS_RESOURCE, to a child's limit on new processes, so a child of such
    a service could start one that outlives its extraction."""
    if os.getuid() == 0:
        raise SystemExit("the extractor does not run as root; run it as an unprivileged user")
    caps = permitted_capabilities()
    if any(caps >> bit & 1 for bit in _LIFTING_CAPS):
        raise SystemExit("the extractor does not run with CAP_SYS_ADMIN or CAP_SYS_RESOURCE")


def refuse_unfiltered_machine() -> None:
    """Every child sets a socket filter for its architecture and refuses to run
    without one; a service that could start none should not start at all."""
    machine = platform.machine()
    if machine not in NO_SOCKETS:
        raise SystemExit(f"no socket filter for {machine}: the extractor runs on x86_64 or aarch64")


def main() -> None:
    refuse_root()
    refuse_unfiltered_machine()
    # Every child inherits it: no setuid binary or file capability gives one privileges.
    no_new_privileges()
    # Before the token is read: a process of the same user cannot then read this
    # process's environment through /proc.
    no_dump()
    app = create_app(Config.from_env(os.environ))
    uvicorn.run(
        app,
        host=os.environ.get("LOOM_EXTRACTOR_HOST", "127.0.0.1"),
        port=int(os.environ.get("LOOM_EXTRACTOR_PORT", "8790")),
    )


if __name__ == "__main__":
    main()
