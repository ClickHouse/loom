"""The document blob directory is a setting, read where every other setting is."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.settings import Settings, settings
from server.storage import blob_store


def test_blob_dir_keeps_its_env_name_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_BLOB_DIR", raising=False)
    assert Settings(_env_file=None).blob_dir == "var/blobs"
    monkeypatch.setenv("LOOM_BLOB_DIR", "/srv/loom/blobs")
    assert Settings(_env_file=None).blob_dir == "/srv/loom/blobs"


def test_get_store_roots_at_the_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "blob_dir", str(tmp_path / "blobs"))
    monkeypatch.setattr(blob_store, "_default_store", None)
    monkeypatch.delenv("LOOM_BLOB_DIR", raising=False)
    _, rel = blob_store.get_store().put("o", "ns", b"hello")
    assert (tmp_path / "blobs" / rel).read_bytes() == b"hello"
