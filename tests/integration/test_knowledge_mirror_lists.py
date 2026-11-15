"""A mirrored source's list rewrite against the deployment's real stores, under whichever
storage authority the run uses: the row takes the new list and keeps its revision, so the
copy still counts as searchable over the chunks it had, and nothing is embedded again.

Skips cleanly when LOOM_CH_HOST is unset.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("LOOM_CH_HOST"),
        reason="LOOM_CH_HOST not set — integration tests require a live CH",
    ),
]


async def test_a_list_rewrite_keeps_the_copy_searchable(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core import pg
    from server.core.ch import get_client, sequential_reads
    from server.core.naming import new_mirror_name
    from server.knowledge.app import source_sync, source_sync_mirror
    from server.knowledge.domain import access_list
    from server.knowledge.domain.sources import (
        MIRRORED_ACCESS,
        SYNC_WORKER_ACTOR,
        KnowledgeSource,
        mirror_uri,
        source_id_tag,
    )
    from server.knowledge.store import sources as store

    ch = get_client()
    await ch.connect()
    org, mirror, source_id = f"mirror-{uuid.uuid4().hex[:8]}", new_mirror_name(), "sid1"
    uri = mirror_uri("https://docs.example.com/document/d/f1/edit", source_id)
    tag = source_id_tag(source_id)
    listed = access_list.AccessList(emails=("ann@example.com",))
    try:
        await ch.insert(
            "loom.documents",
            [
                {
                    "org": org,
                    "namespace": mirror,
                    "source_uri": uri,
                    "content": "Kept text.",
                    "tags": ["source-kind:design_doc", "source:decks", tag],
                    "metadata": json.dumps({"ingested_from": "f1", "source_revision": "r1"}),
                    "version": 1000,
                    "revision": 1,
                    "actor": SYNC_WORKER_ACTOR,
                    "access_json": access_list.to_json(listed),
                }
            ],
        )
        await ch.execute(
            "INSERT INTO loom.chunks (org, namespace, source_uri, chunk_idx, content, "
            "doc_version, chunk_metadata, embedding_model, embedding_dim, embedding, tags) "
            "SELECT {org:String}, {ns:String}, {uri:String}, 0, 'Kept text.', 1000, "
            "'{\"revision\":1}', 'm', 3072, arrayWithConstant(3072, toBFloat16(0)), "
            "[{tag:String}]",
            {"org": org, "ns": mirror, "uri": uri, "tag": tag},
        )

        async def _current(src: Any) -> None:
            return None  # no registry row: the turn's own check is not under test here

        monkeypatch.setattr(source_sync, "require_current", _current)
        src = KnowledgeSource(
            org=org,
            namespace="team",
            name="decks",
            kind="gdrive",
            source_id=source_id,
            access=MIRRORED_ACCESS,
            mirror=mirror,
        )
        written = await source_sync_mirror._write_lists(src, {uri: access_list.AccessList()})
        assert written

        with sequential_reads():
            [copy] = await store.every_copy(org, mirror, tag)
        assert access_list.from_json(copy["access_json"]) == access_list.AccessList()
        assert copy["searchable"] == "1"  # its chunks still serve: nothing re-embedded
        assert copy["source_revision"] == "r1"
    finally:
        for table in ("documents", "chunks"):
            await ch.execute(
                f"ALTER TABLE loom.{table} DELETE WHERE org = {{org:String}} "
                "SETTINGS mutations_sync = 2",
                {"org": org},
            )
        await ch.disconnect()
        if pg.enabled():
            await pg.close_pool()
