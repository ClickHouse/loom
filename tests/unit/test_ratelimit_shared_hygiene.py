"""The shared rate table is private to the server's user (0600), is never
opened through a symlink (O_NOFOLLOW), and lives under a runtime directory
($XDG_RUNTIME_DIR/loom, else /run/loom) with /tmp as the fallback — a
predictable world-readable /tmp file would be a namespace-existence oracle
for anything inside the container."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from server.core import ratelimit
from server.core.ratelimit_shared import (
    MIN_SLOTS,
    SharedBucketStore,
    default_path,
    open_table_fd,
    runtime_dir,
    server_file,
)
from server.core.shared_document import FILE_MODE


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
    """A file an older build created 0644 is reused — the header
    still agrees — and its mode is narrowed the first time this build opens it."""
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
    assert "shared table unavailable" in str(lim.backend_status()["reason"])


def test_a_directory_or_a_foreign_file_at_the_path_is_refused(tmp_path: Path) -> None:
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(OSError):
        open_table_fd(str(d))


def test_default_path_prefers_the_runtime_dir_and_falls_back_to_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import ratelimit_shared as rs

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    (tmp_path / "run-user").mkdir()
    assert runtime_dir() == str(tmp_path / "run-user" / "loom")
    assert stat.S_IMODE((tmp_path / "run-user" / "loom").stat().st_mode) == 0o700
    assert default_path() == server_file("ratelimit") + ".buckets"
    assert os.path.dirname(default_path()) == str(tmp_path / "run-user" / "loom")
    # no XDG dir and no writable /run/loom: the /tmp fallback
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(rs, "RUN_DIR", str(tmp_path / "no-perm" / "loom"))
    (tmp_path / "no-perm").mkdir(mode=0o500)
    try:
        if os.geteuid() == 0:  # root can always write; the branch is not testable as root
            pytest.skip("running as root")
        assert runtime_dir() == "/tmp"
        assert os.path.dirname(default_path()) == "/tmp"
    finally:
        os.chmod(tmp_path / "no-perm", 0o700)
    # a creatable /run/loom stand-in is taken when XDG is unset
    monkeypatch.setattr(rs, "RUN_DIR", str(tmp_path / "run" / "loom"))
    assert runtime_dir() == str(tmp_path / "run" / "loom")
    assert stat.S_IMODE((tmp_path / "run" / "loom").stat().st_mode) == 0o700


def test_the_limiter_uses_the_hardened_open(tmp_path: Path, settings_override) -> None:
    p = tmp_path / "rl.buckets"
    settings_override(
        ratelimit_shared="on", ratelimit_shared_path=str(p), ratelimit_shared_slots=256
    )
    lim = ratelimit.Limiter()
    assert lim.shared_active is True
    assert _mode(p) == 0o600


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc")
def test_server_files_are_named_by_the_parent_and_its_start_time() -> None:
    """A later server that reuses the parent pid (a restarted container) gets new files."""
    started = open(f"/proc/{os.getppid()}/stat").read().rsplit(")", 1)[1].split()[19]
    name = os.path.basename(server_file("budgets"))
    assert name == f"loom-budgets-{os.getppid()}-{started}"
