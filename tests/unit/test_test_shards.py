"""A test run's split into parts (`tests/_shards.py`)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests import _shards


@pytest.mark.parametrize(("spec", "want"), [("1/1", (1, 1)), ("2/4", (2, 4)), ("4/4", (4, 4))])
def test_a_shard_spec_reads_as_index_and_count(spec: str, want: tuple[int, int]) -> None:
    assert _shards.parse(spec) == want


@pytest.mark.parametrize("spec", ["0/4", "5/4", "4", "a/b", "1/", "/4", "-1/4", "1/0", "²/4"])
def test_a_malformed_or_out_of_range_spec_is_refused(spec: str) -> None:
    with pytest.raises(ValueError, match="LOOM_TEST_SHARD"):
        _shards.parse(spec)


def test_every_file_lands_in_exactly_one_part_and_the_parts_are_within_one_file() -> None:
    weights = {f"tests/integration/test_{i:03d}.py": (i * 37) % 50 + 1 for i in range(200)}
    part = _shards.assign(weights, 4)
    assert part.keys() == weights.keys()
    assert set(part.values()) == {1, 2, 3, 4}
    load = [sum(w for f, w in weights.items() if part[f] == k) for k in (1, 2, 3, 4)]
    assert max(load) - min(load) <= max(weights.values())


def test_the_split_depends_only_on_the_files_and_their_weights() -> None:
    weights = {"b.py": 5, "a.py": 5, "c.py": 9, "d.py": 1}
    reordered = dict(reversed(list(weights.items())))
    assert (
        _shards.assign(weights, 2)
        == _shards.assign(reordered, 2)
        == {
            "c.py": 1,
            "a.py": 2,
            "b.py": 2,
            "d.py": 1,
        }
    )


class _Config:
    def __init__(self) -> None:
        self.deselected: list[Any] = []
        self.hook = SimpleNamespace(pytest_deselected=lambda items: self.deselected.extend(items))


def _items(*nodeids: str) -> list[Any]:
    return [SimpleNamespace(nodeid=n) for n in nodeids]


def test_keep_deselects_every_file_outside_the_part(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _items("a.py::t1", "a.py::t2", "a.py::t3", "b.py::t1", "c.py::t1", "c.py::t2")
    config = _Config()
    monkeypatch.setenv("LOOM_TEST_SHARD", "2/2")
    kept = list(items)
    _shards.keep(config, kept)  # type: ignore[arg-type]
    # a.py (3 tests) goes to part 1, c.py (2) to part 2, then b.py (1) to part 2.
    assert [i.nodeid for i in kept] == ["b.py::t1", "c.py::t1", "c.py::t2"]
    assert [i.nodeid for i in config.deselected] == ["a.py::t1", "a.py::t2", "a.py::t3"]


def test_keep_leaves_the_run_whole_without_a_shard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_TEST_SHARD", raising=False)
    items = _items("a.py::t1", "b.py::t1")
    config = _Config()
    _shards.keep(config, items)  # type: ignore[arg-type]
    assert [i.nodeid for i in items] == ["a.py::t1", "b.py::t1"]
    assert config.deselected == []


def test_keep_refuses_a_bad_shard_as_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_TEST_SHARD", "5/4")
    with pytest.raises(pytest.UsageError, match="LOOM_TEST_SHARD"):
        _shards.keep(_Config(), _items("a.py::t1"))  # type: ignore[arg-type]


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_the_parts_keep_every_collected_test_exactly_once(
    monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    nodeids = [f"tests/test_{f}.py::t{t}" for f in range(9) for t in range(f % 4 + 1)]
    kept: list[str] = []
    for k in range(1, n + 1):
        monkeypatch.setenv("LOOM_TEST_SHARD", f"{k}/{n}")
        items, config = _items(*nodeids), _Config()
        _shards.keep(config, items)  # type: ignore[arg-type]
        assert sorted(i.nodeid for i in items + config.deselected) == sorted(nodeids)
        kept += [i.nodeid for i in items]
    assert sorted(kept) == sorted(nodeids)
