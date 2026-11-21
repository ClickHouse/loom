"""Checked listing chooses complete families before filtering and pagination."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.knowledge.api.models_knowledge import KnowledgeListInput
from server.knowledge.app import source_documents
from server.knowledge.app.source_access import Admitted
from server.knowledge.domain.serving import Serving

NOW = datetime(2025, 1, 1, tzinfo=UTC)
NS = "acl-" + "1" * 24
SID = "11111111-1111-4111-8111-111111111111"
OTHER_SID = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def world(monkeypatch):
    state = SimpleNamespace(
        docs={},
        allowed={},
        counts={},
        shadows=set(),
        expired=False,
        stream_error=None,
        discovered=0,
        conditional_error=False,
    )
    sources = {(NS, SID): "team", (NS, OTHER_SID): "team"}
    plan = SimpleNamespace(
        org="acme",
        marked=True,
        failed=False,
        unread=False,
        discovery_sources=sources,
        discovery_reader=None,
        current=lambda _item: not state.expired,
    )

    def add(uri, stamp, *, source=SID, allowed=True, tags=("wanted",), title=None):
        namespace, sid = NS, source
        stored = uri + "#" + sid
        row = {
            "org": "acme",
            "namespace": namespace,
            "source_uri": stored,
            "version": 10,
            "revision": 0,
            "title": title or stored,
            "description": "Description",
            "tags": ["source-id:" + sid, *tags],
            "ingested_at": NOW + timedelta(seconds=stamp),
            "superseded_at": None,
            "blob_path": "",
            "blob_size": 0,
            "blob_content_type": "",
            "original_filename": "",
            "content_preview": "Preview",
        }
        key = (namespace, stored)
        state.docs[key], state.allowed[key], state.counts[key] = row, allowed, stamp
        return key

    async def check(candidates, *, chunks=False):
        assert len(candidates) <= 64 and not chunks
        if state.conditional_error:
            raise RuntimeError("private permission backend")
        return [
            Admitted(
                sources[(key[0], key[1].rsplit("#", 1)[1])],
                candidate,
                dict(state.docs[key]),
                serving=Serving(False, 0, 0, 9),
            )
            for candidate in candidates
            if (key := (candidate["namespace"], candidate["source_uri"])) in state.docs
            and state.allowed[key]
        ]

    @asynccontextmanager
    async def discovery(org, selected, coarse):
        assert org == "acme" and selected and selected.items() <= sources.items()

        async def rows():
            for ns, stored in sorted(state.docs, key=lambda key: (key[1].rsplit("#", 1)[0], *key)):
                if (ns, stored.rsplit("#", 1)[1]) not in selected:
                    continue
                state.discovered += 1
                yield {
                    "namespace": ns,
                    "source_uri": stored,
                    "public_namespace": sources[(ns, stored.rsplit("#", 1)[1])],
                    "public_uri": stored.rsplit("#", 1)[0],
                }
            if state.stream_error:
                raise state.stream_error

        yield rows()

    async def projection(org, namespace, uri, version, revision):
        key = (namespace, uri)
        assert state.allowed[key], "No denied title or preview read"
        row = state.docs[key]
        assert (org, version, revision) == (row["org"], row["version"], row["revision"])
        return dict(row)

    async def count(org, namespace, uri, revision, serving):
        assert org == "acme" and revision == 0 and serving == Serving(False, 0, 0, 9)
        return state.counts[(namespace, uri)]

    async def shadows(org, pairs, *, include_superseded=False):
        assert org == "acme" and len(pairs) <= 64
        return set(pairs) & state.shadows

    plan.check = AsyncMock(side_effect=check)
    monkeypatch.setattr(source_documents.store.pg, "enabled", lambda: False)
    monkeypatch.setattr(source_documents.store, "list_candidates", discovery)
    monkeypatch.setattr(
        source_documents.store, "list_projection", AsyncMock(side_effect=projection)
    )
    monkeypatch.setattr(source_documents.store, "list_chunk_count", AsyncMock(side_effect=count))
    monkeypatch.setattr(source_documents.store, "ordinary_families", AsyncMock(side_effect=shadows))
    return state, plan, add


async def collect(world, *, cursor=None, **fields):
    from server.knowledge.app import source_listing

    _state, plan, _add = world
    return (
        await source_listing.collect(
            plan,
            KnowledgeListInput(org="acme", namespace="team", **fields),
            namespaces=["team"],
            cursor=cursor,
        )
    ).rows


async def test_selection_reads_no_preview_or_chunk_count_before_title_projection(world):
    from server.knowledge.app import source_listing

    _state, plan, add = world
    add("gdrive:a", 10)
    selected = await source_listing.select(
        plan, KnowledgeListInput(org="acme", namespace="team", limit=1), None, namespaces=["team"]
    )
    assert [item.candidate["source_uri"] for item in selected] == ["gdrive:a#" + SID]
    source_documents.store.list_projection.assert_not_awaited()
    source_documents.store.list_chunk_count.assert_not_awaited()


@pytest.mark.parametrize("source_id", [None, OTHER_SID])
async def test_shared_plan_discovery_keeps_each_childs_resolved_namespace_scope(world, source_id):
    from server.knowledge.app import source_listing

    state, plan, add = world
    plan.discovery_sources[(NS, OTHER_SID)] = "another-team"
    add("gdrive:ours", 10)
    add("gdrive:other", 20, source=OTHER_SID)
    selected = await source_listing.select(
        plan,
        KnowledgeListInput(org="acme", namespace="team", limit=2),
        None,
        namespaces=["team"],
        source_id=source_id,
    )
    assert [item.namespace for item in selected] == ([] if source_id else ["team"])
    assert state.discovered == (0 if source_id else 1)


async def test_source_selection_keeps_its_own_copy_despite_other_source_and_ordinary_copy(world):
    from server.knowledge.app import source_listing

    state, plan, add = world
    add("gdrive:a", 10, title="Requested source")
    add("gdrive:a", 20, source=OTHER_SID, title="Another source")
    state.shadows.add(("team", "gdrive:a"))
    selected = await source_listing.select(
        plan,
        KnowledgeListInput(org="acme", namespace="team", limit=1),
        None,
        namespaces=["team"],
        source_id=SID,
    )
    assert [item.document["title"] for item in selected] == ["Requested source"]
    assert len(plan.discovery_sources) == 2 and state.discovered == 1
    source_documents.store.ordinary_families.assert_not_awaited()


async def test_unknown_source_selection_opens_no_conditional_discovery(world):
    from server.knowledge.app import source_listing

    state, plan, add = world
    add("gdrive:a", 10)
    assert (
        await source_listing.select(
            plan,
            KnowledgeListInput(org="acme", namespace="team"),
            None,
            namespaces=["team"],
            source_id="unregistered",
        )
        == []
    )
    assert state.discovered == 0
    plan.check.assert_not_awaited()


@pytest.mark.parametrize("source_id", [None, SID])
async def test_final_selection_barrier_only_catalogs_apply_ordinary_shadows(world, source_id):
    from server.knowledge.app import source_listing

    state, plan, add = world
    add("gdrive:a", 10)
    input = KnowledgeListInput(org="acme", namespace="team")
    selected = await source_listing.select(
        plan, input, None, namespaces=["team"], source_id=source_id
    )
    state.shadows.add(("team", "gdrive:a"))
    final = await source_listing.finish(plan, input, selected, source_id=source_id)
    assert (final is not None) == (source_id is not None)
    assert not plan.unread


async def test_family_winner_is_chosen_before_cursor_so_older_copy_never_reappears(world):
    _state, _plan, add = world
    add("gdrive:a", 100)
    add("gdrive:a", 80, source=OTHER_SID)
    add("gdrive:b", 90)
    add("gdrive:c", 70)
    first = await collect(world, limit=1)
    assert [(r["source_uri"], r["chunk_count"]) for r in first] == [
        ("gdrive:a", 100),
        ("gdrive:b", 90),
    ]
    second = await collect(world, limit=1, cursor=(NOW + timedelta(seconds=90), "team", "gdrive:b"))
    assert [(r["source_uri"], r["chunk_count"]) for r in second] == [("gdrive:c", 70)]


async def test_family_winner_is_chosen_before_tags_without_mixing_copy_fields(world):
    _state, _plan, add = world
    add("gdrive:a", 100, tags=("other",), title="new copy")
    add("gdrive:a", 80, source=OTHER_SID, tags=("wanted",), title="old copy")
    assert await collect(world, tags=["wanted"]) == []
    [row] = await collect(world)
    assert row["title"] == "new copy" and row["tags"] == ["other"] and row["chunk_count"] == 100


async def test_denied_newest_copy_has_no_effect_on_admitted_copy_or_position(world):
    _state, _plan, add = world
    add("gdrive:a", 100, allowed=False)
    add("gdrive:a", 80, source=OTHER_SID)
    [row] = await collect(world, cursor=(NOW + timedelta(seconds=90), "team", "gdrive:z"))
    assert row["chunk_count"] == 80 and row["source_uri"] == "gdrive:a"


async def test_more_than_six_thousand_denied_families_cannot_consume_page_or_lookahead(world):
    state, _plan, add = world
    for index in range(6000):
        add(f"gdrive:{index:04d}", 100, allowed=False)
    add("gdrive:z1", 90)
    add("gdrive:z2", 80)
    result = await collect(world, limit=1)
    assert [row["source_uri"] for row in result] == ["gdrive:z1", "gdrive:z2"]
    assert state.discovered == 6002
    assert source_documents.store.list_projection.await_count == 2


async def test_ordinary_family_shadows_every_copy_before_page_filters(world):
    state, _plan, add = world
    add("gdrive:a", 100)
    add("gdrive:a", 80, source=OTHER_SID)
    add("gdrive:b", 70)
    state.shadows.add(("team", "gdrive:a"))
    [row] = await collect(world, limit=1, tags=["wanted"], include_superseded=True)
    assert row["source_uri"] == "gdrive:b"
    assert all(
        call.kwargs["include_superseded"]
        for call in source_documents.store.ordinary_families.await_args_list
    )


@pytest.mark.parametrize("change", ["permission", "version", "serving", "expired", "shadow"])
async def test_late_change_to_returned_item_or_lookahead_invalidates_whole_mirror_leg(
    world, change
):
    state, plan, add = world
    first = add("gdrive:a", 100)
    add("gdrive:b", 80)
    original = source_documents.store.list_chunk_count.side_effect

    async def count(*args):
        result = await original(*args)
        if source_documents.store.list_chunk_count.await_count == 2:
            if change == "permission":
                state.allowed[first] = False
            elif change == "version":
                state.docs[first]["version"] += 1
            elif change == "expired":
                state.expired = True
            elif change == "shadow":
                state.shadows.add(("team", "gdrive:a"))
            else:
                check = plan.check.side_effect

                async def changed(*a, **k):
                    from dataclasses import replace

                    return [
                        replace(item, serving=Serving(False, 0, 0, 10))
                        for item in await check(*a, **k)
                    ]

                plan.check.side_effect = changed
        return result

    source_documents.store.list_chunk_count.side_effect = count
    assert await collect(world, limit=1) == []
    assert not plan.unread


async def test_discovery_failure_marks_unread_and_never_reads_selected_fields(world, monkeypatch):
    from server.core import request_work

    state, plan, add = world
    add("gdrive:a", 100)
    state.stream_error = RuntimeError("private candidate failure")
    mark = []
    monkeypatch.setattr(request_work, "mark_mirrored", lambda **kwargs: mark.append(kwargs))
    assert await collect(world) == []
    assert plan.failed and plan.unread and mark == [{"unread": True}]
    source_documents.store.list_projection.assert_not_awaited()


async def test_conditional_failure_keeps_all_fixed_discovery_but_does_not_mark_unread(world):
    state, plan, add = world
    for index in range(130):
        add(f"gdrive:{index:04d}", 100)
    state.conditional_error = True
    assert await collect(world) == []
    assert state.discovered == 130 and plan.failed and not plan.unread
    source_documents.store.list_projection.assert_not_awaited()


async def test_no_admitted_copy_has_exact_empty_leg_without_projection_or_count(world):
    _state, _plan, add = world
    add("gdrive:a", 100, allowed=False)
    assert await collect(world) == []
    source_documents.store.list_projection.assert_not_awaited()
    source_documents.store.list_chunk_count.assert_not_awaited()


async def test_cancellation_propagates_without_unread(world):
    state, plan, add = world
    add("gdrive:a", 100)
    state.stream_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await collect(world)
    assert not plan.unread


async def test_native_fragment_uri_and_public_tags_are_projected_only_from_admitted_copy(world):
    _state, _plan, add = world
    add("gdrive:a#native", 100, tags=("wanted", "second"))
    [row] = await collect(
        world, uri_prefix=["gdrive:a#"], tags=["WANTED", "SECOND"], tags_every=True
    )
    assert row["namespace"] == "team" and row["source_uri"] == "gdrive:a#native"
    assert row["tags"] == ["wanted", "second"]


async def test_empty_uri_prefix_does_not_override_a_nonempty_filter(world):
    _state, _plan, add = world
    add("gdrive:wanted", 100)
    add("gdrive:unrelated", 80)
    assert [
        row["source_uri"] for row in await collect(world, uri_prefix=["", "gdrive:wanted"])
    ] == ["gdrive:wanted"]
    assert {row["source_uri"] for row in await collect(world, uri_prefix=[""])} == {
        "gdrive:wanted",
        "gdrive:unrelated",
    }


@pytest.mark.parametrize(
    "field",
    ["org", "namespace", "source_uri", "version", "revision", "ingested_at", "superseded_at"],
)
async def test_wrong_projection_identity_invalidates_whole_mirror_leg(world, field):
    _state, plan, add = world
    add("gdrive:a", 100)
    original = source_documents.store.list_projection.side_effect

    async def changed(*args):
        row = await original(*args)
        row[field] = "other"
        return row

    source_documents.store.list_projection.side_effect = changed
    assert await collect(world) == [] and not plan.unread


@pytest.mark.parametrize("failure", ["projection", "count", "shadow"])
async def test_conditional_io_failure_has_no_unread_or_partial_page(world, failure):
    state, plan, add = world
    for i in range(130):
        add(f"gdrive:{i:04d}", 100)
    target = {
        "projection": "list_projection",
        "count": "list_chunk_count",
        "shadow": "ordinary_families",
    }[failure]
    getattr(source_documents.store, target).side_effect = RuntimeError("private backend detail")
    assert await collect(world) == [] and plan.failed and not plan.unread
    assert state.discovered == 130


async def test_already_failed_conditional_plan_keeps_fixed_discovery(world):
    state, plan, add = world
    add("gdrive:a", 100)
    plan.failed = True
    assert await collect(world) == [] and state.discovered == 1
    plan.check.assert_not_awaited()


async def test_invalid_admitted_metadata_is_conditional_without_unread(world):
    state, plan, add = world
    key = add("gdrive:a", 100)
    state.docs[key]["tags"] = None
    assert await collect(world) == [] and plan.failed and not plan.unread


async def test_selected_field_cancellation_propagates_without_unread(world):
    _state, plan, add = world
    add("gdrive:a", 100)
    source_documents.store.list_projection.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await collect(world)
    assert not plan.failed and not plan.unread
