"""The shared rate table is private to the server's user (0600), is never
opened through a symlink (O_NOFOLLOW), and lives under a runtime directory
($XDG_RUNTIME_DIR/loom, else /run/loom) with /tmp as the fallback — a
predictable world-readable /tmp file would be a namespace-existence oracle
for anything inside the container."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from server.core import ratelimit
from server.core.ratelimit_shared import (
    MIN_SLOTS,
    SharedBucketStore,
    default_path,
    open_table_fd,
)
from server.core.runtime_files import FILE_MODE, runtime_dir, server_file


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_a_new_table_is_created_private_to_the_user(tmp_path: Path) -> None:
    p = tmp_path / "t"
    st = SharedBucketStore(str(p), MIN_SLOTS)
    try:
        assert _mode(p) == FILE_MODE == 0o600
    finally:
        st.close()


def test_a_world_readable_table_is_tightened_on_open(tmp_path: Path) -> None:
    """An existing 0644 file is reused — the header still agrees — and
    its mode is narrowed the first time it is opened."""
    p = tmp_path / "t"
    p.write_bytes(b"")
    os.chmod(p, 0o644)
    assert _mode(p) == 0o644
    st = SharedBucketStore(str(p), MIN_SLOTS)
    try:
        assert _mode(p) == 0o600
        st2 = SharedBucketStore(str(p), MIN_SLOTS)  # a second opener still shares it
        st2.close()
    finally:
        st.close()


def test_a_symlink_at_the_path_is_refused_and_the_limiter_falls_back(
    tmp_path: Path, settings_override
) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(b"")
    link = tmp_path / "rl.buckets"
    os.symlink(target, link)
    with pytest.raises(OSError):
        open_table_fd(str(link))
    assert target.read_bytes() == b"", "the link's target was not written through"
    settings_override(
        ratelimit_shared="on",
        ratelimit_shared_path=str(link),
        ratelimit_shared_slots=256,
        web_workers=4,
    )
    lim = ratelimit.Limiter()
    assert lim.shared_active is False
    reason = str(lim.backend_status()["reason"])
    # `/v1/health` shows it without a token: the error's kind, not the path's text.
    assert reason.startswith("shared table unavailable (") and str(link) not in reason


def test_a_directory_or_a_foreign_file_at_the_path_is_refused(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(OSError):
        open_table_fd(str(d))


def test_default_path_prefers_the_runtime_dir_and_falls_back_to_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import runtime_files as rs

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    (tmp_path / "run-user").mkdir()
    assert runtime_dir() == str(tmp_path / "run-user" / "loom")
    assert stat.S_IMODE((tmp_path / "run-user" / "loom").stat().st_mode) == 0o700
    assert default_path() == server_file("ratelimit") + ".buckets"
    assert os.path.dirname(default_path()) == str(tmp_path / "run-user" / "loom")
    # no XDG dir and no writable /run/loom: this user's private /tmp directory
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(rs, "RUN_DIR", str(tmp_path / "no-perm" / "loom"))
    monkeypatch.setattr(rs, "_TMP_DIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    (tmp_path / "no-perm").mkdir(mode=0o500)
    try:
        if os.geteuid() == 0:  # root can always write; the branch is not testable as root
            pytest.skip("running as root")
        private = str(tmp_path / "tmp" / f"loom-{os.geteuid()}")
        assert runtime_dir() == private and os.path.dirname(default_path()) == private
        assert stat.S_IMODE(os.stat(private).st_mode) == 0o700
    finally:
        os.chmod(tmp_path / "no-perm", 0o700)
    # a creatable /run/loom stand-in is taken when XDG is unset
    monkeypatch.setattr(rs, "RUN_DIR", str(tmp_path / "run" / "loom"))
    assert runtime_dir() == str(tmp_path / "run" / "loom")
    assert stat.S_IMODE((tmp_path / "run" / "loom").stat().st_mode) == 0o700
    if os.geteuid() == 0:
        return
    # a stale XDG_RUNTIME_DIR (the directory is gone) and an unwritable one fall through
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "logged-out" / "1000"))
    (tmp_path / "logged-out").mkdir(mode=0o500)
    (tmp_path / "run-user" / "loom").chmod(0o500)
    try:
        assert runtime_dir() == str(tmp_path / "run" / "loom")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
        assert runtime_dir() == str(tmp_path / "run" / "loom")
    finally:
        (tmp_path / "logged-out").chmod(0o700)
        (tmp_path / "run-user" / "loom").chmod(0o700)


def test_the_launchers_choice_binds_every_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings_override
) -> None:
    """scripts/serve.sh resolves the directory once and exports it; the
    processes then take it as it is, whatever its permissions."""
    import subprocess
    import sys

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    (tmp_path / "xdg").mkdir()
    chosen = subprocess.run(
        [sys.executable, "-m", "server.core.runtime_files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert chosen == str(tmp_path / "xdg" / "loom")
    settings_override(runtime_dir=chosen)
    (tmp_path / "xdg" / "loom").chmod(0o500)
    try:
        assert runtime_dir() == chosen
    finally:
        (tmp_path / "xdg" / "loom").chmod(0o700)


def test_the_limiter_uses_the_hardened_open(tmp_path: Path, settings_override) -> None:
    p = tmp_path / "rl.buckets"
    settings_override(
        ratelimit_shared="on", ratelimit_shared_path=str(p), ratelimit_shared_slots=256
    )
    lim = ratelimit.Limiter()
    assert lim.shared_active is True
    assert _mode(p) == 0o600


def test_a_fifo_where_a_shared_file_goes_is_refused_without_waiting(tmp_path: Path) -> None:
    from server.core.runtime_files import open_private

    fifo = tmp_path / "loom-overrides.json"
    os.mkfifo(fifo)
    with pytest.raises(OSError, match="not a regular file"):
        open_private(str(fifo), os.O_RDONLY)


def test_a_runtime_directory_that_is_not_private_is_passed_over(
    tmp_path: Path, monkeypatch
) -> None:
    from server.core import runtime_files as rs

    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    (tmp_path / "planted").mkdir()
    monkeypatch.setattr(rs, "RUN_DIR", str(tmp_path / "run-loom"))
    (tmp_path / "run-loom").symlink_to(tmp_path / "planted")  # a link, not a directory
    monkeypatch.setattr(rs, "_TMP_DIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    assert rs.choose_runtime_dir() == str(tmp_path / "tmp" / f"loom-{os.geteuid()}")
    (tmp_path / "tmp" / f"loom-{os.geteuid()}").rmdir()
    (tmp_path / "tmp" / f"loom-{os.geteuid()}").symlink_to(tmp_path / "planted")
    with pytest.raises(OSError, match="only this user can enter"):
        rs.choose_runtime_dir()


def test_a_pinned_runtime_directory_must_be_private(tmp_path: Path, settings_override) -> None:
    from server.core import runtime_files as rs

    own = tmp_path / "own"
    settings_override(runtime_dir=str(own))
    assert rs.runtime_dir() == str(own) and stat.S_IMODE(own.stat().st_mode) == 0o700
    (tmp_path / "link").symlink_to(own)
    settings_override(runtime_dir=str(tmp_path / "link"))
    with pytest.raises(OSError, match="LOOM_RUNTIME_DIR"):
        rs.runtime_dir()


def test_an_unusable_runtime_directory_leaves_the_limiter_per_process(
    settings_override, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import ratelimit_shared

    def nowhere(name: str) -> str:
        raise OSError("no private runtime directory")

    monkeypatch.setattr(ratelimit_shared, "server_file", nowhere)
    settings_override(ratelimit_shared="on")
    lim = ratelimit.Limiter()
    assert lim.check("ns1", "memory.set", org="o").allowed
    assert not lim.shared_active


def test_a_pinned_directory_others_can_enter_is_refused_not_changed(
    tmp_path: Path, settings_override
) -> None:
    from server.core import runtime_files as rs

    open_dir = tmp_path / "rt"
    open_dir.mkdir(mode=0o755)
    open_dir.chmod(0o755)
    settings_override(runtime_dir=str(open_dir))
    with pytest.raises(OSError, match="LOOM_RUNTIME_DIR"):
        rs.runtime_dir()
    assert stat.S_IMODE(open_dir.stat().st_mode) == 0o755
