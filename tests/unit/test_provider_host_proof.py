"""A later process on the same host proves a provider participant stopped
(server/core/provider_process.py `proves_stopped`), and retires it
(server/insights/app/provider_barrier.py `retire_dead_here`)."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from server.core import provider_process as pp
from server.insights.app import provider_barrier as barrier

pytestmark = pytest.mark.skipif(not pp.host_proof(), reason="needs /proc")


_KEYS = ("host", "boot", "container", "pidns", "pid", "start", "namespace", "pod")


def _with(**part: str) -> str:
    fields = dict(zip(_KEYS, pp.host_proof().split("|"), strict=True)) | part
    return "|".join(fields[k] for k in _KEYS)


def _dead_pid() -> str:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return str(child.pid)


def test_a_running_process_is_never_proven_stopped() -> None:
    assert not pp.proves_stopped(pp.host_proof())
    assert not pp.proves_stopped(_with(pid=str(os.getppid()), start=pp._start_ticks(os.getppid())))


def test_a_pid_that_exited_or_was_reused_is_proven_stopped() -> None:
    assert pp.proves_stopped(_with(pid=_dead_pid()))
    assert pp.proves_stopped(_with(start="1"))  # the pid now runs a later process


@pytest.mark.parametrize("error", [PermissionError, OSError, IndexError])
def test_a_process_that_cannot_be_inspected_is_never_proven_stopped(
    monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    def _unreadable(pid: int | str) -> str:
        if str(pid) == str(os.getppid()):
            raise error("no access")
        return real(pid)

    real = pp._start_ticks
    monkeypatch.setattr(pp, "_start_ticks", _unreadable)
    assert not pp.proves_stopped(_with(pid=str(os.getppid()), start="1"))


def test_a_pid_this_process_may_not_signal_still_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    def _eperm(pid: int, sig: int) -> None:
        raise PermissionError

    monkeypatch.setattr(pp.os, "kill", _eperm)
    monkeypatch.setattr(pp, "_start_ticks", lambda pid: (_ for _ in ()).throw(PermissionError()))
    assert not pp.proves_stopped(_with(pid=_dead_pid()))


def test_a_container_restarted_into_a_new_pid_namespace_is_proven_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pp, "_container", lambda: "c" * 64)
    assert pp.proves_stopped(_with(container="c" * 64, pidns="1"))


@pytest.mark.parametrize("theirs", ["d" * 64, ""])
def test_another_container_with_this_host_name_is_never_proven_stopped(
    monkeypatch: pytest.MonkeyPatch, theirs: str
) -> None:
    """Host networking, or two containers of one pod, share a host name and boot:
    a pid namespace of another container proves nothing about its processes."""
    monkeypatch.setattr(pp, "_container", lambda: "c" * 64)
    assert not pp.proves_stopped(_with(container=theirs, pidns="1"))
    monkeypatch.setattr(pp, "_container", lambda: "")
    assert not pp.proves_stopped(_with(container="", pidns="1"))


def test_the_container_id_is_read_from_the_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import builtins

    docker = "907 896 259:1 /var/lib/docker/containers/" + "a" * 64 + "/hostname /etc/hostname rw\n"
    kube = (
        "1 2 0:1 /var/lib/kubelet/pods/0b6f2c1e-1111-2222-3333-444455556666/containers/"
        "loom/0f1e /dev/termination-log rw\n"
    )
    real_open = builtins.open
    for mounts, want in (
        (docker, "a" * 64),
        (kube, "0b6f2c1e-1111-2222-3333-444455556666/loom"),
        ("", ""),
    ):
        f = tmp_path / "mountinfo"
        f.write_text(mounts)
        monkeypatch.setattr(
            builtins,
            "open",
            lambda p, *a, _f=f, **k: real_open(_f if p == "/proc/self/mountinfo" else p, *a, **k),
        )
        assert pp._container() == want


@pytest.mark.parametrize("part", [{"host": "elsewhere"}, {"boot": "another-boot"}])
def test_another_host_or_boot_proves_nothing(part: dict[str, str]) -> None:
    assert not pp.proves_stopped(_with(pid=_dead_pid(), **part))


@pytest.mark.parametrize("proof", ["", "a|b", "h|b|p|1|2", "pre-heartbeat build"])
def test_a_row_without_a_proof_proves_nothing(proof: str) -> None:
    assert not pp.proves_stopped(proof)


def _row(participant: UUID, proof: str, **over: Any) -> dict[str, Any]:
    return {
        "participant_id": participant,
        "transition_id": UUID(int=0),
        "state": "running",
        "version": 5,
        "stale_after_s": 60,
        "expired": False,
        "host_proof": proof,
        **over,
    }


@pytest.mark.asyncio
async def test_this_host_retires_its_dead_participants_once_their_queries_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    me, dead, busy, paused, remote = (uuid4() for _ in range(5))
    monkeypatch.setattr(barrier, "process_identity", lambda: me)
    gone = _with(pid=_dead_pid())
    rows = [
        _row(me, pp.host_proof()),
        _row(dead, gone),
        _row(busy, gone),
        _row(paused, _with(pid=str(os.getppid()), start=pp._start_ticks(os.getppid()))),
        _row(remote, _with(host="elsewhere", pid=_dead_pid())),
    ]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    monkeypatch.setattr(
        barrier, "running_queries", AsyncMock(side_effect=lambda p: int(str(p) == str(busy)))
    )
    record = AsyncMock()
    monkeypatch.setattr(barrier, "record_participant", record)
    monkeypatch.setattr(barrier, "_last_here", -float("inf"))
    assert await barrier.retire_dead_here(now=100.0) == 1
    record.assert_awaited_once_with(dead, UUID(int=0), "retired", after=5)
    assert await barrier.retire_dead_here(now=100.0 + barrier.HERE_S / 2) == 0  # throttled


# -- a pod Kubernetes deleted ------------------------------------------------

_POD_UID = "0b6f2c1e-1111-2222-3333-444455556666"


def _pod_proof(
    namespace: str = "loom", container: str = f"{_POD_UID}/loom", pod: str = "loom-7d9-abc"
) -> str:
    # The host name is the pod name cut to 63 characters; the lookup uses the pod name.
    return "|".join((pod[:63], "boot", container, "4026", "7", "99", namespace, pod))


@pytest.fixture
def kube(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    """A service-account mount, the API's address and a stand-in API: it answers
    the (status, body) pairs queued in `answers` and keeps what it saw in `seen`."""
    from types import SimpleNamespace

    import httpx

    (tmp_path / "token").write_text("tok")
    (tmp_path / "ca.crt").write_text("")
    monkeypatch.setattr(pp, "_SA", str(tmp_path))
    monkeypatch.setenv("POD_NAMESPACE", "loom")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    api = SimpleNamespace(answers=[], seen=[])

    def handler(request: httpx.Request) -> httpx.Response:
        api.seen.append(request)
        status, body = api.answers.pop(0)
        return httpx.Response(status, json=body)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), timeout=kw.get("timeout")),
    )
    return api


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,uid,phase,gone",
    [
        (404, "", "", True),
        (200, "another-uid", "Running", True),
        (200, _POD_UID, "Failed", True),  # evicted: the object stays, its containers stopped
        (200, _POD_UID, "Succeeded", True),
        (200, _POD_UID, "Running", False),
        (200, _POD_UID, "Pending", False),
        (403, "", "", False),
    ],
)
async def test_a_pod_is_gone_only_when_the_api_says_so(kube, status, uid, phase, gone) -> None:
    kube.answers.append((status, {"metadata": {"uid": uid}, "status": {"phase": phase}}))
    assert await pp.pod_gone(_pod_proof()) is gone
    assert kube.seen[0].url.path == "/api/v1/namespaces/loom/pods/loom-7d9-abc"
    assert kube.seen[0].headers["authorization"] == "Bearer tok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof",
    [
        _pod_proof(namespace="other"),
        _pod_proof(container="a" * 64),
        _pod_proof(pod=""),
        "h|b|c|p|1|2",
        "h|b|" + _POD_UID + "/loom|p|1|2|loom",  # a 7-part proof (no pod name) proves nothing
        "",
    ],
)
async def test_a_pod_elsewhere_or_outside_kubernetes_is_never_gone(kube, proof) -> None:
    assert await pp.pod_gone(proof) is False
    assert kube.seen == []  # nothing to ask: another namespace, a Docker container, an old row


@pytest.mark.asyncio
async def test_the_leader_retires_an_expired_participant_whose_pod_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    me, lost, busy, kept = (uuid4() for _ in range(4))
    monkeypatch.setattr(barrier, "process_identity", lambda: me)
    rows = [
        _row(me, pp.host_proof()),
        _row(lost, _pod_proof(), expired=True, version=9),
        _row(busy, _pod_proof(), expired=True),
        _row(kept, _pod_proof(), expired=True),
    ]
    monkeypatch.setattr(barrier, "participants", AsyncMock(return_value=rows))
    monkeypatch.setattr(barrier, "prune_retired", AsyncMock())
    monkeypatch.setattr(
        barrier, "running_queries", AsyncMock(side_effect=lambda p: int(str(p) == str(busy)))
    )
    asked: list[str] = []

    async def gone(proof: str) -> bool:
        asked.append(proof)
        return len(asked) == 1  # the first asked (lost) is gone, kept's pod still exists

    monkeypatch.setattr(barrier, "pod_gone", gone)
    record = AsyncMock()
    monkeypatch.setattr(barrier, "record_participant", record)
    monkeypatch.setattr(barrier, "_STALE", None)
    await barrier.sweep_stopped(now=5000.0)
    record.assert_awaited_once_with(lost, UUID(int=0), "retired", after=9)
    assert len(asked) == 2  # a participant with a query running is never asked about
    assert [p["participant_id"] for p in barrier.stale_participants()] == [str(busy), str(kept)]


@pytest.mark.asyncio
async def test_a_long_pod_name_is_looked_up_whole_not_by_its_cut_host_name(kube) -> None:
    long = "loom-memory-platform-production-workers-7d9c8b6f5d-x4kqz-abcdefgh"
    kube.answers.append((200, {"metadata": {"uid": _POD_UID}, "status": {"phase": "Running"}}))
    assert await pp.pod_gone(_pod_proof(pod=long)) is False
    assert kube.seen[0].url.path == f"/api/v1/namespaces/loom/pods/{long}"
