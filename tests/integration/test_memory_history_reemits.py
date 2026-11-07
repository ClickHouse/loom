"""memory.history leaves out the entity tagger's re-emits and keeps every change
a caller made: tags, attestations, values — also past a run of re-emits longer
than the limit."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("history-reemit")


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


async def _tag_entities(ns: str, key: str, entities: list[str]) -> None:
    """A copy of the key's head with other entities: a re-emit, as a copy writer leaves."""
    from server.memory.store.lifecycle import insert_versions_over_heads
    from server.memory.store.reads import memory_rows_by_identity_keys
    from server.memory.store.rows import build_memory_version_row, next_version

    identity = (ORG, ns, "t", "-", "-", key)
    head = (await memory_rows_by_identity_keys([identity], sequential=True))[identity]
    row = build_memory_version_row(head, entities=entities, version=next_version())
    await insert_versions_over_heads([row], [head])


async def _history(a: Any, key: str, n: int, *, limit: int = 50) -> list[dict[str, Any]]:
    async def _n_rows() -> list[dict[str, Any]] | None:
        rows = await a.memory.history(key, limit=limit)
        return rows if len(rows) >= n else None

    return await eventually(_n_rows, description=f"{n} history rows of {key}")


@pytest.mark.asyncio
async def test_reemits_are_left_out_and_edits_stay(control_plane_url, mint_test_token) -> None:
    from loom import Agent

    ns = f"hist-reemit-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        # Value edit, then twelve re-emits: more than the inspector's eight-row window.
        await a.memory.set("v", "First meaning")
        await a.memory.set("v", "Second meaning")
        await _history(a, "v", 2)
        await _tag_entities(ns, "v", ["meaning"])
        for i in range(11):
            await _tag_entities(ns, "v", ["meaning", f"e{i}"])
        rows = await _history(a, "v", 2, limit=8)
        assert [r["value"] for r in rows] == ["Second meaning", "First meaning"], rows

        # A tag change is a caller's edit.
        await a.memory.set("t", "owned", tags=["owner:old"])
        await a.memory.set("t", "owned", tags=["owner:new"])
        tagged = await _history(a, "t", 2)
        assert [r["tags"] for r in tagged] == [["owner:new"], ["owner:old"]], tagged

        # Attest, then revoke: three versions.
        await a.memory.set("a", "attested")
        await _history(a, "a", 1)
        assert (await a.memory.verify("a")).get("changed")
        assert (await a.memory.verify("a", revoke=True)).get("changed")
        attested = await _history(a, "a", 3)
        assert [bool(r["verified_at"]) for r in attested] == [False, True, False], attested

        # A second verifier and a lock update that keeps its mode: history carries
        # the attestation and lock metadata that tell them from the version before.
        await a.memory.set("r", "reviewed")
        await _history(a, "r", 1)
        assert (await a.memory.verify("r")).get("changed")
        tok2 = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"], subject="reviewer2")
        async with Agent(
            org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok2
        ) as b:
            assert (await b.memory.verify("r")).get("changed")
        reviewed = await _history(a, "r", 3)
        first, second = reviewed[1]["verified_by"], reviewed[0]["verified_by"]
        assert first and second and first != second, reviewed

        # Verify, edit, verify again within one second: the same verifier and timestamp,
        # a new attested hash.
        await a.memory.set("e", "draft")
        await _history(a, "e", 1)
        assert (await a.memory.verify("e")).get("changed")
        await a.memory.set("e", "final")
        assert (await a.memory.verify("e")).get("changed")
        edited = await _history(a, "e", 4)
        assert edited[0]["verified_sha256"] == edited[0]["content_sha256"], edited
        assert (
            edited[1]["verified_sha256"]
            == edited[2]["content_sha256"]
            != edited[0]["content_sha256"]
        ), edited

        await a.memory.set("l", "held")
        await a.memory.lock("l", reason="audit", mode="no_delete", ttl_seconds=600)
        await a.memory.lock("l", reason="legal hold", mode="no_delete")
        held = await _history(a, "l", 3)
        assert [(r["lock_reason"], r["lock_until"] is None) for r in held[:2]] == [
            ("legal hold", True),
            ("audit", False),
        ], held
