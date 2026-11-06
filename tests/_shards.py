"""Split a test run into parts that run on separate machines.

`LOOM_TEST_SHARD=k/n` keeps the k-th of n parts of whatever pytest collected and
deselects the rest (`tests/conftest.py` calls `keep`). A part holds whole files,
so a module-scoped fixture is built once, in one part. Files are dealt heaviest
first to the lightest part, weighted by their test count, so the parts end up
within about one file of each other. Every process that collects the same files
with the same test counts computes the same split, and each file lands in
exactly one part.
"""

from __future__ import annotations

import collections
import os

import pytest


def parse(spec: str) -> tuple[int, int]:
    """`"k/n"` as `(k, n)`, 1 <= k <= n; anything else is a ValueError."""
    index, sep, count = spec.partition("/")
    if not sep or not index.isdecimal() or not count.isdecimal():
        raise ValueError(f"LOOM_TEST_SHARD={spec!r} is not k/n")
    k, n = int(index), int(count)
    if not 1 <= k <= n:
        raise ValueError(f"LOOM_TEST_SHARD={spec!r}: k must be between 1 and n")
    return k, n


def assign(weights: dict[str, int], parts: int) -> dict[str, int]:
    """Each file's part, 1-based: heaviest first, to the lightest part so far
    (ties to the lower part, files of equal weight in name order)."""
    load = [0] * parts
    out: dict[str, int] = {}
    for name in sorted(weights, key=lambda f: (-weights[f], f)):
        part = load.index(min(load))
        load[part] += weights[name]
        out[name] = part + 1
    return out


def keep(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Deselect every collected file outside the requested part; no-op without one."""
    spec = os.environ.get("LOOM_TEST_SHARD", "")
    if not spec:
        return
    try:
        k, n = parse(spec)
    except ValueError as e:
        raise pytest.UsageError(str(e)) from e
    part = assign(collections.Counter(_file(item) for item in items), n)
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        (kept if part[_file(item)] == k else dropped).append(item)
    config.hook.pytest_deselected(items=dropped)
    items[:] = kept


def _file(item: pytest.Item) -> str:
    return item.nodeid.split("::", 1)[0]
