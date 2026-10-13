"""The image's default web worker count (`server.core.web_workers`)."""

from __future__ import annotations

from pathlib import Path

from server.core.web_workers import WORKER_MEMORY_BYTES, default_workers

GB = 1024**3
BIG_MEM = "MemTotal:       134217728 kB\n"  # 128 GiB


def _w(root: Path, rel: str, name: str, value: str) -> None:
    d = root / rel.lstrip("/")
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(value + "\n")


def test_unlimited_container_gets_one_per_cpu(tmp_path: Path) -> None:
    _w(tmp_path, "/", "cpu.max", "max 100000")
    assert default_workers(cpus=14, proc_cgroup="0::/\n", meminfo=BIG_MEM, root=tmp_path) == 14


def test_cpu_quota_rounds_up(tmp_path: Path) -> None:
    _w(tmp_path, "/", "cpu.max", "250000 100000")
    assert default_workers(cpus=32, proc_cgroup="0::/\n", meminfo=BIG_MEM, root=tmp_path) == 3


def test_memory_limit_bounds_the_count(tmp_path: Path) -> None:
    _w(tmp_path, "/", "memory.max", str(512 * 1024 * 1024))
    assert default_workers(cpus=14, proc_cgroup="0::/\n", meminfo=BIG_MEM, root=tmp_path) == 1
    _w(tmp_path, "/", "memory.max", str(3 * WORKER_MEMORY_BYTES + 1))
    assert default_workers(cpus=14, proc_cgroup="0::/\n", meminfo=BIG_MEM, root=tmp_path) == 3


def test_host_memory_bounds_the_count(tmp_path: Path) -> None:
    meminfo = f"MemTotal:       {2 * GB // 1024} kB\n"
    assert default_workers(cpus=32, proc_cgroup="0::/\n", meminfo=meminfo, root=tmp_path) == 4


def test_never_below_one(tmp_path: Path) -> None:
    _w(tmp_path, "/", "memory.max", str(64 * 1024 * 1024))
    assert default_workers(cpus=4, proc_cgroup="0::/\n", meminfo=BIG_MEM, root=tmp_path) == 1


def test_host_cgroup_namespace_reads_the_process_path(tmp_path: Path) -> None:
    # --cgroupns=host: the mount root is unlimited, the limit sits on the scope.
    _w(tmp_path, "/", "cpu.max", "max 100000")
    _w(tmp_path, "/system.slice/docker-abc.scope", "cpu.max", "100000 100000")
    cg = "0::/system.slice/docker-abc.scope\n"
    assert default_workers(cpus=14, proc_cgroup=cg, meminfo=BIG_MEM, root=tmp_path) == 1


def test_an_ancestor_limit_binds(tmp_path: Path) -> None:
    _w(tmp_path, "/kubepods/pod1", "cpu.max", "200000 100000")
    _w(tmp_path, "/kubepods/pod1/ctr", "cpu.max", "max 100000")
    cg = "0::/kubepods/pod1/ctr\n"
    assert default_workers(cpus=16, proc_cgroup=cg, meminfo=BIG_MEM, root=tmp_path) == 2


def test_cgroup_v1_quota_and_memory(tmp_path: Path) -> None:
    cg = "12:memory:/docker/abc\n4:cpu,cpuacct:/docker/abc\n1:name=systemd:/docker/abc\n"
    _w(tmp_path, "/cpu,cpuacct/docker/abc", "cpu.cfs_quota_us", "200000")
    _w(tmp_path, "/cpu,cpuacct/docker/abc", "cpu.cfs_period_us", "100000")
    _w(tmp_path, "/memory", "memory.limit_in_bytes", str(9223372036854771712))
    assert default_workers(cpus=8, proc_cgroup=cg, meminfo=BIG_MEM, root=tmp_path) == 2
    _w(tmp_path, "/memory/docker/abc", "memory.limit_in_bytes", str(WORKER_MEMORY_BYTES))
    assert default_workers(cpus=8, proc_cgroup=cg, meminfo=BIG_MEM, root=tmp_path) == 1


def test_cgroup_v1_namespaced_mount_reads_the_root(tmp_path: Path) -> None:
    # The controller mount is the container's own group; /proc still names /docker/<id>.
    cg = "4:cpu,cpuacct:/docker/abc\n"
    _w(tmp_path, "/cpu,cpuacct", "cpu.cfs_quota_us", "100000")
    _w(tmp_path, "/cpu,cpuacct", "cpu.cfs_period_us", "100000")
    assert default_workers(cpus=8, proc_cgroup=cg, meminfo=BIG_MEM, root=tmp_path) == 1


def test_cgroup_v1_unlimited_quota(tmp_path: Path) -> None:
    cg = "4:cpu,cpuacct:/\n"
    _w(tmp_path, "/cpu,cpuacct", "cpu.cfs_quota_us", "-1")
    _w(tmp_path, "/cpu,cpuacct", "cpu.cfs_period_us", "100000")
    assert default_workers(cpus=6, proc_cgroup=cg, meminfo=BIG_MEM, root=tmp_path) == 6


def test_affinity_not_omp_threads(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMP_NUM_THREADS", "64")
    monkeypatch.setattr("os.sched_getaffinity", lambda _pid: {0})
    assert default_workers(proc_cgroup="0::/\n", meminfo=BIG_MEM, root=tmp_path) == 1
