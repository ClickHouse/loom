"""Default uvicorn worker count for the image (`scripts/serve.sh` prints it when
LOOM_WEB_WORKERS is unset): one per CPU this process may run on, bounded by the
memory it may use, and never more than `MAX_WORKERS`.

CPUs: the scheduler affinity (a cpuset; `nproc` would read OMP_NUM_THREADS),
lowered by the tightest CPU quota on this process's own cgroup path, v2
`cpu.max` or v1 `cpu.cfs_quota_us`, at every level up to the mount root. The
path comes from /proc/self/cgroup, so a container in the host's cgroup
namespace still finds its limit. Memory: the tightest of `memory.max` /
`memory.limit_in_bytes` along the same path and MemTotal, at
`WORKER_MEMORY_BYTES` per process. Never below one.
"""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable
from pathlib import Path

# Memory budget per web process: a busy one with a real embedder and an
# indexed code namespace holds several hundred MB.
WORKER_MEMORY_BYTES = 1024 * 1024 * 1024
# Each process opens its own ClickHouse pool and calibrates its embedder at boot;
# past this, more processes add load on a shared host rather than throughput.
MAX_WORKERS = 8
_UNLIMITED = 1 << 60
CGROUP_ROOT = Path("/sys/fs/cgroup")


def _read(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def _cgroup_dirs(proc_cgroup: str, root: Path, controller: str) -> list[Path]:
    """The process's cgroup directory for `controller` and each ancestor up to the
    mount root, most specific first ("" = the v2 unified hierarchy)."""
    out: list[Path] = []
    for line in proc_cgroup.splitlines():
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        _, ctrls, rel = parts
        if controller == "":
            if ctrls != "":
                continue
            base = root
        else:
            if controller not in ctrls.split(","):
                continue
            base = root / ctrls
            if not base.is_dir():
                base = root / controller
        p = base / rel.lstrip("/")
        while True:
            out.append(p)
            if p == base:
                break
            p = p.parent
    return out


def _min_limit(dirs: list[Path], read: Callable[[Path], float | None]) -> float:
    vals = [v for v in (read(d) for d in dirs) if v is not None]
    return min(vals) if vals else math.inf


def _v2_cpu(d: Path) -> float | None:
    raw = _read(d / "cpu.max")
    if not raw:
        return None
    quota, _, period = raw.partition(" ")
    if quota == "max" or not period:
        return None
    return int(quota) / int(period)


def _v1_cpu(d: Path) -> float | None:
    quota, period = _read(d / "cpu.cfs_quota_us"), _read(d / "cpu.cfs_period_us")
    if not quota or not period or int(quota) <= 0:
        return None
    return int(quota) / int(period)


def _v2_mem(d: Path) -> float | None:
    raw = _read(d / "memory.max")
    return None if not raw or raw == "max" else float(raw)


def _v1_mem(d: Path) -> float | None:
    raw = _read(d / "memory.limit_in_bytes")
    return None if not raw or int(raw) >= _UNLIMITED else float(raw)


def _mem_total(meminfo: str) -> float:
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            return float(line.split()[1]) * 1024
    return math.inf


def available_cpus() -> int:
    """CPUs this process may run on (Linux affinity; elsewhere the CPU count)."""
    if sys.platform == "linux":
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def cpu_limit(
    *, cpus: int | None = None, proc_cgroup: str | None = None, root: Path = CGROUP_ROOT
) -> int:
    """CPUs this process may keep busy: the affinity, lowered by the CPU quota."""
    if cpus is None:
        cpus = available_cpus()
    if proc_cgroup is None:
        proc_cgroup = _read(Path("/proc/self/cgroup")) or ""
    quota = min(
        _min_limit(_cgroup_dirs(proc_cgroup, root, ""), _v2_cpu),
        _min_limit(_cgroup_dirs(proc_cgroup, root, "cpu"), _v1_cpu),
    )
    return max(1, cpus if quota == math.inf else min(cpus, math.ceil(quota)))


def default_workers(
    *,
    cpus: int | None = None,
    proc_cgroup: str | None = None,
    meminfo: str | None = None,
    root: Path = CGROUP_ROOT,
) -> int:
    if proc_cgroup is None:
        proc_cgroup = _read(Path("/proc/self/cgroup")) or ""
    if meminfo is None:
        meminfo = _read(Path("/proc/meminfo")) or ""
    mem = min(
        _min_limit(_cgroup_dirs(proc_cgroup, root, ""), _v2_mem),
        _min_limit(_cgroup_dirs(proc_cgroup, root, "memory"), _v1_mem),
        _mem_total(meminfo),
    )
    n = float(min(cpu_limit(cpus=cpus, proc_cgroup=proc_cgroup, root=root), MAX_WORKERS))
    if mem != math.inf:
        n = min(n, mem // WORKER_MEMORY_BYTES)
    return max(1, int(n))


if __name__ == "__main__":
    print(default_workers())
