"""`server.core.shared_document`: the JSON document one server's web processes share."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from server.core import shared_document
from server.core.shared_document import SharedDocument


def _doc(path: Path) -> SharedDocument[dict[str, Any]]:
    return SharedDocument(str(path), lambda data: dict(data["v"]))


def _encode(state: dict[str, Any]) -> dict[str, Any]:
    return {"v": state}


def test_poll_returns_a_document_once_per_version(tmp_path: Path) -> None:
    a, b = _doc(tmp_path / "d.json"), _doc(tmp_path / "d.json")
    assert b.poll() is None  # no file yet
    seen = []
    a.update(lambda latest: seen.append(latest) or {"x": 1}, _encode)
    assert seen == [None]
    assert b.poll() == {"x": 1}
    assert b.poll() is None  # unchanged
    assert a.poll() is None  # its own edit is already adopted


def test_an_edit_starts_from_the_latest_version(tmp_path: Path) -> None:
    a, b = _doc(tmp_path / "d.json"), _doc(tmp_path / "d.json")
    a.update(lambda _: {"x": 1}, _encode)
    b.poll()
    # re-read under the lock even though b had seen it
    assert b.update(lambda latest: {**(latest or {}), "y": 2}, _encode) == {"x": 1, "y": 2}
    assert a.update(lambda latest: latest or {}, _encode) == {"x": 1, "y": 2}


def test_a_corrupt_document_is_warned_once_and_replaced_by_the_next_edit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "d.json"
    path.write_text("{not json")
    os.chmod(path, 0o600)
    doc = _doc(path)
    with caplog.at_level(logging.WARNING):
        assert doc.poll() is None
        assert doc.poll() is None
    assert sum("is corrupt" in r.getMessage() for r in caplog.records) == 1
    assert doc.update(lambda latest: {"x": 1} if latest is None else latest, _encode) == {"x": 1}
    assert json.loads(path.read_text()) == {"v": {"x": 1}}


def test_an_edit_refuses_a_document_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "d.json"
    doc = _doc(path)
    doc.update(lambda _: {"x": 1}, _encode)
    before = path.read_bytes()
    real = shared_document.open_private

    def denied(p: str, flags: int) -> int:
        if p == str(path):
            raise PermissionError(errno.EACCES, "denied", p)
        return real(p, flags)

    monkeypatch.setattr(shared_document, "open_private", denied)
    os.utime(path, ns=(1, 1))  # a new version, so the poll reads
    assert doc.poll() is None  # reads keep the cached state
    with pytest.raises(PermissionError):
        doc.update(lambda _: {"x": 2}, _encode)
    assert path.read_bytes() == before


def test_a_symlinked_document_is_refused(tmp_path: Path) -> None:
    planted = tmp_path / "planted.json"
    planted.write_text(json.dumps({"v": {"x": 9}}))
    (tmp_path / "d.json").symlink_to(planted)
    doc = _doc(tmp_path / "d.json")
    assert doc.poll() is None
    with pytest.raises(OSError):
        doc.update(lambda _: {}, _encode)


def test_a_symlinked_lock_is_refused(tmp_path: Path) -> None:
    (tmp_path / "d.json.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        _doc(tmp_path / "d.json").update(lambda _: {}, _encode)
    assert not (tmp_path / "elsewhere").exists()


def test_files_are_private_to_the_user(tmp_path: Path) -> None:
    _doc(tmp_path / "d.json").update(lambda _: {}, _encode)
    for name in ("d.json", "d.json.lock"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_a_failed_publish_changes_nothing_and_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "d.json"
    doc = _doc(path)
    doc.update(lambda _: {"x": 1}, _encode)
    before = path.read_bytes()

    def no_space(*_a: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shared_document.os, "replace", no_space)
    with pytest.raises(OSError):
        doc.update(lambda _: {"x": 2}, _encode)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["d.json", "d.json.lock"]


def test_a_lasting_read_failure_warns_once_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "d.json"
    _doc(path).update(lambda _: {"x": 1}, _encode)
    reader = _doc(path)
    real = shared_document.open_private

    def denied(p: str, flags: int) -> int:
        if p == str(path):
            raise PermissionError(errno.EACCES, "denied", p)
        return real(p, flags)

    monkeypatch.setattr(shared_document, "open_private", denied)
    with caplog.at_level(logging.WARNING):
        assert reader.poll() is None
        assert reader.poll() is None
    assert sum("unreadable" in r.getMessage() for r in caplog.records) == 1
    monkeypatch.undo()
    assert reader.poll() == {"x": 1}
