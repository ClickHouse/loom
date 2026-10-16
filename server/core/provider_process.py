"""This process's provider-participant id and the tag its ClickHouse queries carry.

Every ClickHouse client the process opens names the id in its HTTP User-Agent,
so `system.processes` says whether a participant that stopped heartbeating
still has a query running.
"""

import os
import re
import socket
from uuid import UUID, uuid4

_identity: tuple[int, UUID] | None = None


def process_identity() -> UUID:
    """One id per process; a forked child draws its own."""
    global _identity
    if _identity is None or _identity[0] != os.getpid():
        _identity = (os.getpid(), uuid4())
    return _identity[1]


def participant_tag(identity: UUID | str) -> str:
    """What `system.processes.http_user_agent` contains for that participant's queries."""
    return f"loom-participant/{UUID(str(identity)).hex}"


def client_name() -> str:
    """The `client_name` every ClickHouse client of this process is opened with."""
    return participant_tag(process_identity())


# host name | kernel boot id | container | pid namespace | pid | process start
# (clock ticks after boot)
_proof: tuple[int, str] | None = None
# Docker mounts /etc/hostname from its container's directory; a Kubernetes container
# mounts its termination log from the pod's, under the container's name.
_CONTAINER = (
    re.compile(r"/containers/([0-9a-f]{64})/hostname "),
    re.compile(r"/pods/([0-9a-f-]{36})/containers/([^/ ]+)/"),
)


def _start_ticks(pid: int | str) -> str:
    with open(f"/proc/{pid}/stat", encoding="ascii") as f:
        return f.read().rsplit(")", 1)[1].split()[19]


def _boot_and_pidns() -> tuple[str, str]:
    with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
        boot = f.read().strip()
    return boot, str(os.stat("/proc/self/ns/pid").st_ino)


def _container() -> str:
    """This container's id, stable across its restarts; '' outside one, or where
    the mounts do not say."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            mounts = f.read()
    except OSError:
        return ""
    for pattern in _CONTAINER:
        found = pattern.search(mounts)
        if found:
            return "/".join(found.groups())
    return ""


def host_proof() -> str:
    """What lets a later process on this host prove this one stopped (`proves_stopped`);
    '' where /proc does not say."""
    global _proof
    if _proof is None or _proof[0] != os.getpid():
        try:
            boot, pidns = _boot_and_pidns()
            value = "|".join(
                (
                    socket.gethostname(),
                    boot,
                    _container(),
                    pidns,
                    str(os.getpid()),
                    _start_ticks("self"),
                )
            )
        except (OSError, IndexError):
            value = ""
        _proof = (os.getpid(), value)
    return _proof[1]


def proves_stopped(proof: str) -> bool:
    """Whether the process `proof` names has stopped, judged from this one: same host
    name and kernel boot, and either its pid is free or runs another process than
    the one that started then (same pid namespace), or it ran in this container
    before the container restarted into a new pid namespace (which kills every
    process of the old one). Two containers that share a host name never prove
    each other stopped; a process that cannot be inspected is not proven stopped."""
    parts = proof.split("|")
    if len(parts) != 6:
        return False
    host, boot, container, pidns, pid, start = parts
    try:
        my_boot, my_pidns = _boot_and_pidns()
    except OSError:
        return False
    if host != socket.gethostname() or boot != my_boot:
        return False
    if pidns != my_pidns:
        return bool(container) and container == _container()
    # Only positive evidence: no such process, or another one under its pid.
    # A pid this process may not inspect (hidepid, a permission error) proves nothing.
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False
    try:
        return _start_ticks(pid) != start
    except (OSError, IndexError):
        return False
