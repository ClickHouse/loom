"""`server.core.shared_document`: the JSON document one server's web processes share."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from server.core import shared_document
from server.core.errors import OverloadedError, OverrideBusyError, OverrideNotSharedError
from server.core.shared_document import SharedDocument


def _encode(state: dict[str, Any]) -> dict[str, Any]:
    return {"v": state}


def _doc(path: Path) -> SharedDocument[dict[str, Any]]:
    return SharedDocument(str(path), lambda data: dict(data["v"]), _encode)


# The state an update starts from when there is no document (or it is corrupt).
NONE: dict[str, Any] = {"none": True}


@pytest.fixture(autouse=True)
def _poll_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared_document, "POLL_INTERVAL_S", 0.0)


def test_poll_returns_a_document_once_per_version(tmp_path: Path) -> None:
    a, b = _doc(tmp_path / "d.json"), _doc(tmp_path / "d.json")
    assert b.poll() is None  # no file yet
    seen = []
    a.update(lambda latest: seen.append(latest) or {"x": 1}, NONE)
    assert seen == [NONE]
    assert b.poll() == {"x": 1}
    assert b.poll() is None  # unchanged
    assert a.poll() is None  # its own edit is already adopted


def test_a_version_that_looks_unchanged_to_stat_is_still_adopted(tmp_path: Path) -> None:
    """Same inode, size and mtime (an inode reused within a timestamp tick): the
    generation tells the versions apart."""
    path = tmp_path / "d.json"
    a, b = _doc(path), _doc(path)
    a.update(lambda _: {"x": 1}, NONE)
    assert b.poll() == {"x": 1}
    before = os.stat(path)
    raw = json.loads(path.read_text())
    raw["generation"] ^= 1  # another generation of the same length
    raw["document"] = {"v": {"x": 2}}
    with open(path, "r+") as fh:  # in place, the same length
        fh.write(json.dumps(raw))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(path)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    assert b.poll() == {"x": 2}


def test_a_poll_reads_at_most_once_an_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    monkeypatch.setattr(shared_document.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(shared_document, "POLL_INTERVAL_S", 1.0)
    path = tmp_path / "d.json"
    a, b = _doc(path), _doc(path)
    assert b.poll() is None
    a.update(lambda _: {"x": 1}, NONE)
    assert b.poll() is None  # inside the interval: not read
    now[0] += 1.0
    assert b.poll() == {"x": 1}


def test_an_edit_starts_from_the_latest_version(tmp_path: Path) -> None:
    a, b = _doc(tmp_path / "d.json"), _doc(tmp_path / "d.json")
    a.update(lambda _: {"x": 1}, NONE)
    b.poll()
    # re-read under the lock even though b had seen it
    assert b.update(lambda latest: {**latest, "y": 2}, NONE) == {"x": 1, "y": 2}
    assert a.update(lambda latest: latest, NONE) == {"x": 1, "y": 2}


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
    assert doc.update(lambda latest: {"x": 1} if latest is NONE else latest, NONE) == {"x": 1}
    assert json.loads(path.read_text())["document"] == {"v": {"x": 1}}


def test_an_edit_refuses_a_document_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "d.json"
    doc = _doc(path)
    doc.update(lambda _: {"x": 1}, NONE)
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
        doc.update(lambda _: {"x": 2}, NONE)
    assert path.read_bytes() == before


def test_a_symlinked_document_is_refused(tmp_path: Path) -> None:
    planted = tmp_path / "planted.json"
    planted.write_text(json.dumps({"generation": 1, "document": {"v": {"x": 9}}}))
    (tmp_path / "d.json").symlink_to(planted)
    doc = _doc(tmp_path / "d.json")
    assert doc.poll() is None
    with pytest.raises(OSError):
        doc.update(lambda _: {}, NONE)


def test_a_symlinked_lock_is_refused(tmp_path: Path) -> None:
    (tmp_path / "d.json.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        _doc(tmp_path / "d.json").update(lambda _: {}, NONE)
    assert not (tmp_path / "elsewhere").exists()


def test_files_are_private_to_the_user(tmp_path: Path) -> None:
    _doc(tmp_path / "d.json").update(lambda _: {}, NONE)
    for name in ("d.json", "d.json.lock"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_a_failed_publish_changes_nothing_and_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "d.json"
    doc = _doc(path)
    doc.update(lambda _: {"x": 1}, NONE)
    before = path.read_bytes()

    def no_space(*_a: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shared_document.os, "replace", no_space)
    with pytest.raises(OSError):
        doc.update(lambda _: {"x": 2}, NONE)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["d.json", "d.json.lock"]


def test_a_lasting_read_failure_warns_once_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "d.json"
    _doc(path).update(lambda _: {"x": 1}, NONE)
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


def test_a_document_of_the_wrong_shape_is_corrupt_and_replaced(tmp_path: Path) -> None:
    path = tmp_path / "d.json"
    # decoding calls .items() on a list
    path.write_text(json.dumps({"generation": 1, "document": {"v": []}}))
    os.chmod(path, 0o600)
    doc = SharedDocument(str(path), lambda data: dict(data["v"].items()), _encode)
    assert doc.poll() is None
    assert doc.update(lambda latest: {"x": 1} if latest is NONE else latest, NONE) == {"x": 1}


def test_a_read_that_fails_after_opening_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "d.json"
    writer, reader = _doc(path), _doc(path)
    writer.update(lambda _: {"x": 30}, NONE)
    assert reader.poll() == {"x": 30}
    writer.update(lambda _: {"x": 1}, NONE)
    real = json.load
    failures = [OSError(errno.EIO, "injected read error")]

    def load(fh: Any) -> Any:
        if failures:
            raise failures.pop()
        return real(fh)

    monkeypatch.setattr(shared_document.json, "load", load)
    assert reader.poll() is None
    assert reader.poll() == {"x": 1}  # the same version, read again


def test_a_failed_update_leaves_a_siblings_newer_version_to_adopt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "d.json"
    a, b = _doc(path), _doc(path)
    a.update(lambda _: {"x": 30}, NONE)
    b.update(lambda _: {"x": 1}, NONE)  # a has not seen this yet

    def no_space(*_a: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shared_document.os, "replace", no_space)
    with pytest.raises(OSError):
        a.update(lambda latest: {**latest, "y": 2}, NONE)  # reads b's version, then fails
    monkeypatch.undo()
    assert a.poll() == {"x": 1}


def test_an_update_gives_up_at_once_on_a_lock_a_sibling_holds(tmp_path: Path) -> None:
    path = str(tmp_path / "doc.json")
    holder = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    doc = SharedDocument(path, lambda d: d, lambda d: d)
    try:
        with pytest.raises(OSError, match="held by another process"):
            doc.update(lambda d: {**d, "a": 1}, {})
    finally:
        os.close(holder)
    assert doc.update(lambda d: {**d, "a": 1}, {}) == {"a": 1}


def test_a_busy_lock_is_a_retry_and_an_unwritable_document_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = str(tmp_path / "doc.json")
    doc = SharedDocument(path, lambda d: d, lambda d: d)
    holder = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(OverrideBusyError):
            shared_document.share_override(doc, lambda d: d, {}, what="budget override")
    finally:
        os.close(holder)
    monkeypatch.setattr(shared_document, "_publish", _refuse_publish)
    with pytest.raises(OverrideNotSharedError):
        shared_document.share_override(doc, lambda d: d, {}, what="budget override")


def _refuse_publish(path: str, data: object) -> object:
    raise OSError(28, "No space left on device")


def test_an_override_route_waits_out_a_siblings_lock_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tries: list[int] = []

    def update(busy_for: int) -> str:
        tries.append(1)
        if len(tries) <= busy_for:
            raise OverrideBusyError("a sibling holds the lock")
        return "set"

    async def _run() -> None:
        ticks = 0

        async def other_requests() -> None:
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0)

        served = asyncio.create_task(other_requests())
        assert await shared_document.waiting_out_siblings(update, 3) == "set"
        assert len(tries) == 4
        served.cancel()
        assert ticks > 0  # the loop served others while the route waited
        monkeypatch.setattr(shared_document, "LOCK_WAIT_S", 0.02)
        tries.clear()
        with pytest.raises(OverrideBusyError):
            await shared_document.waiting_out_siblings(update, 10**6)

    asyncio.run(_run())


def test_only_a_busy_override_is_waited_out() -> None:
    """A pool that is out of connections answers OVERLOADED at once."""

    def update() -> None:
        raise OverloadedError("every connection is busy")

    with pytest.raises(OverloadedError):
        asyncio.run(shared_document.waiting_out_siblings(update))


def test_a_path_that_will_not_resolve_is_asked_for_again_sparingly(tmp_path: Path) -> None:
    asked: list[int] = []
    fixed = False

    def where() -> str:
        asked.append(1)
        if not fixed:
            raise OSError("no private runtime directory")
        return str(tmp_path / "doc.json")

    doc = SharedDocument(where, lambda d: d, lambda d: d, name="budgets")
    for _ in range(100):  # every request polls
        assert doc.poll() is None
    assert len(asked) == 1 and doc.path is None
    with pytest.raises(OSError):
        doc.update(lambda d: {**d, "a": 1}, {})  # an update asks again at once
    assert len(asked) == 2
    fixed = True
    assert doc.update(lambda d: {**d, "a": 1}, {}) == {"a": 1}
    assert doc.path == str(tmp_path / "doc.json")


def test_a_sibling_whose_poll_was_turned_away_adopts_within_the_retry_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The price of polling sparingly: a reader turned away just before the
    directory was fixed sees the next update once RESOLVE_RETRY_S has passed."""
    now = [100.0]
    monkeypatch.setattr(shared_document.time, "monotonic", lambda: now[0])
    fixed = False

    def where() -> str:
        if not fixed:
            raise OSError("no private runtime directory")
        return str(tmp_path / "doc.json")

    writer = SharedDocument(where, lambda d: d, lambda d: d, name="budgets")
    reader = SharedDocument(where, lambda d: d, lambda d: d, name="budgets")
    assert reader.poll() is None  # turned away
    fixed = True
    writer.update(lambda d: {**d, "a": 1}, {})
    assert reader.poll() is None  # still inside the window
    now[0] += shared_document.RESOLVE_RETRY_S
    assert reader.poll() == {"a": 1}
