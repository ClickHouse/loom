"""The host directory (provider-process proofs) is a setting, read where every other setting is."""

from __future__ import annotations

import pytest

from server.settings import Settings


def test_blob_dir_keeps_its_env_name_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOOM_BLOB_DIR", raising=False)
    assert Settings(_env_file=None).blob_dir == "var/blobs"
    monkeypatch.setenv("LOOM_BLOB_DIR", "/srv/loom/blobs")
    assert Settings(_env_file=None).blob_dir == "/srv/loom/blobs"
