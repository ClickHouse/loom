"""`loom blobs import` racing a namespace purge, on live ClickHouse: the import
lists a legacy document, the purge completes before the import copies it, and
the import then resumes. Nothing of the erased original may come back.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.skipif(
    not os.environ.get("LOOM_CH_HOST"),
    reason="LOOM_CH_HOST not set — integration tests require a live CH",
)

NS = "blob-race"


async def _document_residue(org: str) -> dict[str, int]:
    from server.erasure.store import census

    counts = await census.residue_census(org, "namespace", [NS])
    return {t: counts[t] for t in ("loom.documents", "loom.document_blobs")}


def test_an_import_resumed_after_a_purge_restores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.cli import portability
    from server.cli.app import main
    from server.erasure.app import saga_steps
    from server.erasure.store.mutations import purge_scope
    from server.knowledge.store import documents as documents_repo

    org = unique_org("blob-race")
    data = b"erased original " + org.encode()
    sha = hashlib.sha256(data).hexdigest()
    legacy = f"{org}/{NS}/{sha[:2]}/{sha}"
    (tmp_path / org / NS / sha[:2]).mkdir(parents=True)
    (tmp_path / legacy).write_bytes(data)
    real_import_one = portability._import_one
    census_after_purge: list[dict[str, int]] = []

    async def purge_then_import(ch: Any, *args: Any) -> bool:
        # Paused after the import's listing: the purge runs to its census.
        with purge_scope(True):
            await saga_steps._step_tombstone_documents_by_ns(org, [NS])
            census_after_purge.append(await _document_residue(org))
        return await real_import_one(ch, *args)

    async def seed(ch: Any) -> int:
        row = documents_repo.build_document_row(
            org=org, namespace=NS, source_uri="doc", content="text", version=1, blob_path=legacy
        )
        await documents_repo.insert_one(row, ch=ch)
        return 0

    portability.run_connected(seed)
    monkeypatch.setattr(portability, "_import_one", purge_then_import)
    # The store is shared: the import sees only this test's org.
    scoped = f"org = '{org}' AND {documents_repo.LEGACY_BLOB_PATH}"
    monkeypatch.setattr(documents_repo, "LEGACY_BLOB_PATH", scoped)
    assert main(["blobs", "import", str(tmp_path), "--apply"]) == 0

    # The legacy row outlives the purge while its file is on disk; the import
    # stores none of its bytes, deletes the file and stamps the row.
    assert census_after_purge == [{"loom.documents": 1, "loom.document_blobs": 0}]
    assert not (tmp_path / legacy).exists()

    async def after(ch: Any) -> dict[str, int]:
        assert await _document_residue(org) == {"loom.documents": 0, "loom.document_blobs": 0}
        with purge_scope(True):  # the recovery worker's re-run of the step
            await saga_steps._step_tombstone_documents_by_ns(org, [NS])
            return await _document_residue(org)

    assert portability.run_connected(after) == {"loom.documents": 0, "loom.document_blobs": 0}
