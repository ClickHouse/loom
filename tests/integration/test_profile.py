"""agent.profile — always-loaded, char-limited, agent-editable context."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from loom.errors import LoomError  # noqa: E402


@pytest.mark.asyncio
async def test_profile_set_and_get(agent) -> None:
    r = await agent.profile.set(
        "persona",
        "Helpful, terse, no emoji.",
        char_limit=2048,
    )
    assert r["version"]
    assert r["content_sha256"]

    g = await agent.profile.get("persona")
    assert g is not None
    assert g["value"] == "Helpful, terse, no emoji."
    assert g["char_limit"] == 2048
    assert g["memory_type"] == "profile"


@pytest.mark.asyncio
async def test_profile_get_missing_returns_none(agent) -> None:
    out = await agent.profile.get("does-not-exist")
    assert out is None


@pytest.mark.asyncio
async def test_profile_char_limit_rejects_over(agent) -> None:
    with pytest.raises(LoomError) as excinfo:
        await agent.profile.set("overflow", "x" * 100, char_limit=50)
    msg = str(excinfo.value).lower()
    assert "char_limit" in msg or "exceeds" in msg


@pytest.mark.asyncio
async def test_profile_append_atomic(agent) -> None:
    await agent.profile.set("notes", "Line one.", char_limit=4096)
    await agent.profile.append("notes", "Line two.")

    g = await agent.profile.get("notes")
    assert g is not None
    assert g["value"] == "Line one.\nLine two."
    assert g["char_limit"] == 4096


@pytest.mark.asyncio
async def test_profile_append_on_empty_treats_as_set(agent) -> None:
    r = await agent.profile.append("fresh", "first content")
    assert r["version"]
    g = await agent.profile.get("fresh")
    assert g["value"] == "first content"


@pytest.mark.asyncio
async def test_profile_replace_substring(agent) -> None:
    await agent.profile.set(
        "persona",
        "Helpful, uses ClickHouse heavily.",
        char_limit=2048,
    )
    await agent.profile.replace("persona", "ClickHouse", "columnar databases")

    g = await agent.profile.get("persona")
    assert g["value"] == "Helpful, uses columnar databases heavily."


@pytest.mark.asyncio
async def test_profile_replace_missing_substring_raises(agent) -> None:
    await agent.profile.set("persona", "Some value", char_limit=2048)
    with pytest.raises(LoomError) as excinfo:
        await agent.profile.replace("persona", "totally-absent-text", "x")
    assert "not found" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_profile_list(agent) -> None:
    await agent.profile.set("persona", "p", char_limit=2048)
    await agent.profile.set("human", "h", char_limit=2048)
    await agent.memory.set(memory_key="not-a-block", value="just memory")

    rows = await agent.profile.list()
    keys = sorted(r["memory_key"] for r in rows)
    assert "persona" in keys
    assert "human" in keys
    assert "not-a-block" not in keys


@pytest.mark.asyncio
async def test_profile_render_xml(agent) -> None:
    await agent.profile.set("persona", "Helpful, terse.", char_limit=2048)
    await agent.profile.set("human", "User is a senior engineer.", char_limit=2048)

    rendered = await agent.profile.render()
    assert '<section label="human">User is a senior engineer.</section>' in rendered
    assert '<section label="persona">Helpful, terse.</section>' in rendered
    assert rendered.index("human") < rendered.index("persona")


@pytest.mark.asyncio
async def test_profile_render_markdown(agent) -> None:
    await agent.profile.set("persona", "Helpful, terse.", char_limit=2048)
    rendered = await agent.profile.render(format="markdown")
    assert "## persona" in rendered
    assert "Helpful, terse." in rendered


@pytest.mark.asyncio
async def test_profile_render_explicit_labels(agent) -> None:
    await agent.profile.set("a", "AA", char_limit=128)
    await agent.profile.set("b", "BB", char_limit=128)
    await agent.profile.set("c", "CC", char_limit=128)

    rendered = await agent.profile.render(labels=["c", "a"])
    assert rendered.index("CC") < rendered.index("AA")
    assert "BB" not in rendered


@pytest.mark.asyncio
async def test_profile_delete(agent) -> None:
    await agent.profile.set("tmp", "to be removed", char_limit=128)
    await agent.profile.delete("tmp")
    g = await agent.profile.get("tmp")
    assert g is None


@pytest.mark.asyncio
async def test_profile_per_user_binding(agent) -> None:
    await agent.profile.set("preferences", "User A prefers terse.", char_limit=512, user_id="userA")
    await agent.profile.set(
        "preferences", "User B prefers verbose.", char_limit=512, user_id="userB"
    )

    ga = await agent.profile.get("preferences", user_id="userA")
    gb = await agent.profile.get("preferences", user_id="userB")
    assert ga["value"] == "User A prefers terse."
    assert gb["value"] == "User B prefers verbose."


@pytest.mark.asyncio
async def test_profile_append_respects_existing_cap(agent) -> None:
    await agent.profile.set("tight", "small seed", char_limit=20)
    with pytest.raises(LoomError):
        await agent.profile.append("tight", "X" * 50)


@pytest.mark.asyncio
async def test_profile_restore_preserves_char_limit(agent) -> None:
    await agent.profile.set("revivable", "Stable persona text.", char_limit=1024)
    await agent.profile.delete("revivable")
    await agent.memory.restore("revivable")
    got = await agent.profile.get("revivable")
    assert got is not None
    assert got["char_limit"] == 1024


@pytest.mark.asyncio
async def test_memory_touch_preserves_char_limit_and_pinned(agent) -> None:
    await agent.profile.set("touchable", "Stable value", char_limit=512)
    await agent.memory.touch("touchable", ttl_seconds=3600)
    got = await agent.profile.get("touchable")
    assert got is not None
    assert got["char_limit"] == 512, f"touch silently dropped char_limit; got {got['char_limit']!r}"


@pytest.mark.asyncio
async def test_bulk_set_accepts_char_limit_and_pinned(agent) -> None:
    await agent.memory.bulk_set(
        [
            {
                "memory_key": "bulk_section_a",
                "memory_type": "profile",
                "value": "Bulk-seeded persona A",
                "char_limit": 1024,
            },
            {
                "memory_key": "bulk_pinned_b",
                "memory_type": "semantic",
                "value": "Bulk-pinned fact",
                "pinned": True,
            },
        ]
    )
    got_a = await agent.memory.get(memory_key="bulk_section_a")
    got_b = await agent.memory.get(memory_key="bulk_pinned_b")
    assert got_a is not None and got_a.get("char_limit") == 1024, (
        f"bulk_set lost char_limit; got {got_a.get('char_limit') if got_a else None!r}"
    )
    assert got_b is not None and got_b.get("pinned") is True, (
        f"bulk_set lost pinned=True; got pinned={got_b.get('pinned') if got_b else None!r}"
    )


@pytest.mark.asyncio
async def test_memory_update_by_id_preserves_tags(agent) -> None:
    set_out = await agent.memory.set(
        "tagged",
        {"data": "v1"},
        memory_type="semantic",
        tags=["critical", "team-ops"],
    )
    mid = set_out["memory_id"]
    await agent.memory.update_by_id(mid, {"data": "v2"})
    listing = await agent.memory.list()
    rows = {r["memory_key"]: r for r in listing.get("entries", [])}
    assert "tagged" in rows
    tags = sorted(rows["tagged"].get("tags") or [])
    assert tags == ["critical", "team-ops"], f"update_by_id silently dropped tags; got {tags!r}"


@pytest.mark.asyncio
async def test_memory_restore_preserves_pinned(agent) -> None:
    await agent.memory.pin("important", "Sticky fact", "semantic")
    got = await agent.memory.get(memory_key="important")
    assert got is not None and got.get("pinned") is True, "pin failed to take"

    await agent.memory.delete(memory_key="important")
    await agent.memory.restore("important")

    got = await agent.memory.get(memory_key="important")
    assert got is not None
    assert got.get("pinned") is True, (
        "restore silently lost pinned=True; restore_row needs the "
        "column and the SELECT needs to fetch it"
    )


@pytest.mark.asyncio
async def test_profile_cannot_be_promoted(agent, control_plane_url, mint_test_token) -> None:
    from loom import Agent

    await agent.profile.set("persona", "Private agent persona", char_limit=2048)

    admin_tok = mint_test_token(
        org=agent.org, namespaces=[agent.namespace], roles=["admin", "writer"]
    )
    async with Agent(
        org=agent.org,
        namespace=agent.namespace,
        agent=agent.agent,
        base_url=control_plane_url,
        api_key=admin_tok,
    ) as admin:
        with pytest.raises(LoomError) as excinfo:
            await admin.memory.promote("persona")
    msg = str(excinfo.value).lower()
    assert "profile" in msg or "refus" in msg, (
        f"expected promote refusal message, got: {excinfo.value}"
    )


@pytest.mark.asyncio
async def test_memory_search_excludes_profile_by_default(agent) -> None:
    """Unfiltered memory.search must exclude profile rows even with a live
    embedding; force-insert one and confirm it's filtered."""
    import uuid as _uuid

    from server.settings import settings
    from tests.integration._resources import ch_client_from_env

    label = f"persona-srch-{_uuid.uuid4().hex[:8]}"
    await agent.profile.set(label, "Distinctive persona text for search test", char_limit=2048)

    ch = ch_client_from_env()
    ch.insert(
        "loom.memory_embeddings",
        [
            [
                agent.org,
                agent.namespace,
                agent.agent,
                "-",  # user_id
                "-",  # session_id
                label,
                "profile",
                "private",
                "Distinctive persona text for search test",
                "fake-ngram-1024",
                settings.embedding_dim,
                [0.1] * settings.embedding_dim,
                [0.0] * settings.embedding_dim,
                1.0,
                None,
                # Aware: the driver takes a naive datetime as the host's local time.
                __import__("datetime").datetime.now(__import__("datetime").timezone.utc),
            ]
        ],
        column_names=[
            "org",
            "namespace",
            "agent",
            "user_id",
            "session_id",
            "memory_key",
            "memory_type",
            "scope",
            "content_excerpt",
            "embedding_model",
            "embedding_dim",
            "embedding",
            "memory_key_embedding",
            "confidence",
            "tombstoned_at",
            "created_at",
        ],
    )

    hits = await agent.memory.search("Distinctive persona text")
    hit_keys = [h.memory_key for h in hits]
    assert label not in hit_keys, (
        f"unfiltered memory.search returned a profile section ({label!r}); "
        f"non-searchable types must be excluded by default"
    )


# Profile.set queues an embedding so loom_search's profile leg finds a
# section by meaning; that profile text never ranks in memory.search is
# test_memory_search_excludes_profile_by_default above.
