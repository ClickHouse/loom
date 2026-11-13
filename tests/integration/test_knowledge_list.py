"""knowledge.list — paginated curation listing."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("klist")


@pytest.mark.asyncio
async def test_list_returns_docs_in_namespace(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kl-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(3):
            await a.knowledge.add(
                content=f"doc {i}",
                source_uri=f"doc://{ns}/{i}",
                title=f"t-{i}",
                tags=["batch"],
            )
        page = await a.knowledge.list(limit=10)

    uris = {r["source_uri"] for r in page["rows"]}
    assert len(uris) == 3
    for r in page["rows"]:
        assert r["chunk_count"] >= 1
        assert r["superseded"] is False
        assert "batch" in r["tags"]


@pytest.mark.asyncio
async def test_list_cursor_pagination(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kl-pg-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for i in range(7):
            await a.knowledge.add(
                content=f"doc {i}",
                source_uri=f"doc://pg/{i:02d}",
                title=f"t-{i}",
            )

        seen: list[str] = []
        cursor = None
        for _ in range(10):  # safety bound
            page = await a.knowledge.list(limit=3, cursor=cursor)
            for r in page["rows"]:
                seen.append(r["source_uri"])
            cursor = page.get("next_cursor")
            if not cursor:
                break

    assert len(seen) == len(set(seen)), f"duplicate uris across pages: {seen}"
    assert len(seen) == 7


@pytest.mark.asyncio
async def test_list_excludes_deleted_by_default(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kl-del-{uuid.uuid4().hex[:6]}"
    keep = f"doc://keep-{uuid.uuid4().hex[:6]}"
    delete = f"doc://delete-{uuid.uuid4().hex[:6]}"

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="x", source_uri=keep, title="k")
        await a.knowledge.add(content="x", source_uri=delete, title="d")
        await a.knowledge.delete(delete)

        default_page = await a.knowledge.list(limit=10)
        with_superseded = await a.knowledge.list(
            limit=10,
            include_superseded=True,
        )

    default_uris = {r["source_uri"] for r in default_page["rows"]}
    assert keep in default_uris
    assert delete not in default_uris

    incl_uris = {r["source_uri"] for r in with_superseded["rows"]}
    assert delete in incl_uris


@pytest.mark.asyncio
async def test_list_tag_filter(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kl-tag-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(
            content="x",
            source_uri=f"doc://tagged-{uuid.uuid4().hex[:6]}",
            title="t",
            tags=["python", "doc"],
        )
        await a.knowledge.add(
            content="x",
            source_uri=f"doc://other-{uuid.uuid4().hex[:6]}",
            title="t",
            tags=["rust"],
        )

        py_page = await a.knowledge.list(tags=["python"], limit=10)
    uris = {r["source_uri"] for r in py_page["rows"]}
    assert any("tagged" in u for u in uris)
    assert not any("other" in u for u in uris)


@pytest.mark.asyncio
async def test_list_cross_namespace_isolation(control_plane_url: str) -> None:
    from loom import Agent

    ns_a = f"kl-a-{uuid.uuid4().hex[:6]}"
    ns_b = f"kl-b-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns_a, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="A", source_uri=f"doc://{ns_a}/x", title="a")
    async with Agent(org=ORG, namespace=ns_b, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="B", source_uri=f"doc://{ns_b}/x", title="b")
        page = await a.knowledge.list(limit=10)
    uris = {r["source_uri"] for r in page["rows"]}
    assert any(ns_b in u for u in uris)
    assert not any(ns_a in u for u in uris)


@pytest.mark.asyncio
async def test_list_tags_match_in_any_case(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kl-case-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="x", source_uri="doc://specs", title="t", tags=["Specs"])
        await a.knowledge.add(
            content="y", source_uri="doc://summer", title="t", tags=["ÉTÉ", "Draft"]
        )
        await a.knowledge.add(content="z", source_uri="doc://plain", title="t", tags=["été"])

        def uris(page: dict) -> set[str]:
            return {r["source_uri"] for r in page["rows"]}

        assert uris(await a.knowledge.list(tags=["specs"], limit=10)) == {"doc://specs"}
        summer = await a.knowledge.list(tags=["Été"], limit=10)
        assert uris(summer) == {"doc://summer", "doc://plain"}
        every = await a.knowledge.list(tags=["été", "draft"], tags_every=True, limit=10)
        assert uris(every) == {"doc://summer"}


@pytest.mark.asyncio
async def test_list_reads_several_namespaces_as_one_newest_first(control_plane_url: str) -> None:
    from loom import Agent

    tag = uuid.uuid4().hex[:6]
    namespaces_listed = [f"kl-a-{tag}", f"kl-b-{tag}", f"kl-c-{tag}"]
    for ns in namespaces_listed:
        async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
            for i in range(2):
                await a.knowledge.add(content=f"{ns} {i}", source_uri=f"doc://{i}", title=ns)
    async with Agent(
        org=ORG, namespace=namespaces_listed[0], agent="w", base_url=control_plane_url
    ) as a:
        keys: list[tuple[str, str, str]] = []
        cursor = None
        # Two a page, through the SDK: the cursor walks across the namespaces.
        while True:
            page = await a.knowledge.list(
                limit=2, cursor=cursor, namespaces=namespaces_listed[1:] + [namespaces_listed[0]]
            )
            keys += [(r["ingested_at"], r["namespace"], r["source_uri"]) for r in page["rows"]]
            cursor = page["next_cursor"]
            if not cursor:
                break
        one = await a.transport.post(
            "/v1/knowledge.list", {"org": ORG, "namespace": namespaces_listed[1], "limit": 10}
        )
    # Newest first, by the keyset the cursor walks, with nothing repeated or skipped.
    assert keys == sorted(keys, reverse=True)
    assert sorted((ns, uri) for _, ns, uri in keys) == sorted(
        (ns, f"doc://{i}") for ns in namespaces_listed for i in range(2)
    )
    assert {r["namespace"] for r in one["rows"]} == {namespaces_listed[1]}


@pytest.mark.asyncio
async def test_a_cursor_for_other_namespaces_is_refused(control_plane_url: str) -> None:
    from loom import Agent
    from loom.errors import ValidationError

    tag = uuid.uuid4().hex[:6]
    a_ns, b_ns, c_ns = f"kl-x-{tag}", f"kl-y-{tag}", f"kl-z-{tag}"
    async with Agent(org=ORG, namespace=a_ns, agent="w", base_url=control_plane_url) as a:
        for i in range(3):
            await a.knowledge.add(content=f"x {i}", source_uri=f"doc://{i}", title="x")
        page = await a.transport.post(
            "/v1/knowledge.list", {"org": ORG, "namespace": a_ns, "namespaces": [b_ns], "limit": 1}
        )
        with pytest.raises(ValidationError) as refused:
            await a.transport.post(
                "/v1/knowledge.list",
                {
                    "org": ORG,
                    "namespace": b_ns,
                    "namespaces": [c_ns],
                    "cursor": page["next_cursor"],
                },
            )
    assert refused.value.status == 422
