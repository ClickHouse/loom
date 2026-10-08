"""BlobStore.put: content-addressed, atomic, and safe for concurrent writers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from server.storage import blob_store
from server.storage.blob_store import BlobStore


def test_put_then_read_round_trips_and_leaves_no_temp(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha, rel = store.put("o", "ns", b"hello")
    assert rel == f"o/ns/{sha[:2]}/{sha}"
    assert store.read(rel) == b"hello"
    assert [p.name for p in (tmp_path / "o" / "ns" / sha[:2]).iterdir()] == [sha]


def test_two_writers_of_the_same_bytes_both_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same bytes uploaded through two processes resolve to one path. A
    second writer that lands between the first's write and its rename must
    not take the first's temp file away."""
    store = BlobStore(tmp_path)
    real_replace = os.replace
    interleaved: list[str] = []

    def _replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        if not interleaved:
            interleaved.append("second writer")
            store.put("o", "ns", b"same bytes")
        real_replace(src, dst)

    monkeypatch.setattr(blob_store.os, "replace", _replace)
    sha, rel = store.put("o", "ns", b"same bytes")
    assert interleaved and store.read(rel) == b"same bytes"
    assert [p.name for p in (tmp_path / "o" / "ns" / sha[:2]).iterdir()] == [sha]


def test_a_failed_write_removes_its_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = BlobStore(tmp_path)

    def _boom(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(blob_store.os, "replace", _boom)
    with pytest.raises(OSError):
        store.put("o", "ns", b"x")
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_a_dot_segment_never_leaves_the_root(tmp_path: Path) -> None:
    store = BlobStore(tmp_path / "root")
    _sha, rel = store.put("..", "..", b"x")
    assert rel.startswith("_/_/")
    assert store.read(rel) == b"x"
    assert not (tmp_path / _sha[:2]).exists()


def test_read_and_delete_refuse_paths_outside_the_root(tmp_path: Path) -> None:
    (tmp_path / "secret").write_bytes(b"s")
    store = BlobStore(tmp_path / "root")
    with pytest.raises(ValueError):
        store.read("../secret")
    with pytest.raises(ValueError):
        store.read("")
    assert store.delete("../secret") is False
    assert (tmp_path / "secret").exists()
