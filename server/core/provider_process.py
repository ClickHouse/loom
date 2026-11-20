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
# (clock ticks after boot) | Kubernetes namespace and pod name (POD_NAMESPACE,
# POD_NAME: the host name is the pod name cut to 63 characters; '' elsewhere)
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
                    os.environ.get("POD_NAMESPACE", ""),
                    os.environ.get("POD_NAME", ""),
                )
            )
        except (OSError, IndexError):
            value = ""
        _proof = (os.getpid(), value)
    return _proof[1]


def proves_stopped(proof: str) -> bool:
    """Whether the process `proof` names has stopped, judged from this one: same host
    name and kernel boot, and its pid is free, runs a different birth, or is a
    single-thread zombie of that birth (same pid namespace). Another namespace or API
    object proves nothing. Sharing a host name or container identity alone does
    not prove death; an uninspectable process is not proven stopped."""
    parts = proof.split("|")
    if len(parts) == 9:
        if parts[8] != LIFECYCLE:
            return False
        parts = parts[:8]
    if len(parts) not in (6, 7, 8):
        return False
    host, boot, container, pidns, pid, start = parts[:6]
    try:
        my_boot, my_pidns = _boot_and_pidns()
    except OSError:
        return False
    if host != socket.gethostname() or boot != my_boot:
        return False
    if pidns != my_pidns:
        return False  # namespace identity alone says nothing about old processes
    # Only positive evidence: absence, a different birth, or a fully exited group.
    # A pid this process may not inspect (hidepid, a permission error) proves nothing.
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False
    try:
        if _start_ticks(pid) != start:
            return True
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            stat = f.read().rsplit(")", 1)[1].split()
        # A zombie leader can still have live siblings. Bind all three fields to
        # one fresh record: only a single-thread zombie cannot resume this birth.
        return stat[19] == start and stat[0] == "Z" and stat[17] == "1"
    except (OSError, IndexError, ValueError):
        return False


LIFECYCLE = "cell-retirement-v1"


def certified(proof: str) -> str:
    """Preserve the exact captured identity and stamp the installed protocol."""
    parts = proof.split("|")
    if len(parts) == 9 and parts[8] == LIFECYCLE:
        parts = parts[:8]
    if len(parts) not in (6, 7, 8):
        raise ValueError("Unrecognized participant identity proof")
    parts += [""] * (8 - len(parts))
    if not all(parts[i] for i in (0, 1, 3, 4, 5)):
        raise ValueError("Incomplete participant identity proof")
    if not parts[4].isdecimal() or int(parts[4]) <= 0 or not parts[5].isdecimal():
        raise ValueError("Invalid participant process identity")
    return "|".join((*parts, LIFECYCLE))


def has_certificate(proof: str) -> bool:
    if len(proof.split("|")) != 9:
        return False
    try:
        return certified(proof) == proof
    except ValueError:
        return False


def participant_proof() -> str:
    """Advertise capability only in a composition with installed lifecycle guards."""
    from server.core.retirement import get_gate

    proof = host_proof()
    return certified(proof) if proof and get_gate().role else proof
