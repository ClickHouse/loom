"""A composed pack renews both document pools and rebuilds its entire ordinary wire."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from server.auth.reader_identity import ReaderIdentity
from server.core import request_tail
from server.knowledge.api.models_knowledge import KnowledgeSearchOutput, _SearchHit
from server.knowledge.app import source_access, source_publication
from server.knowledge.app.knowledge_search import CheckedSearch
from server.knowledge.app.search import ChunkHit
from server.knowledge.app.source_passages import CheckedPassages
from server.knowledge.app.source_search_proofs import SearchProofs
from server.knowledge.domain.access_list import AccessList
from server.knowledge.store import source_candidates
from server.memory.api.models_context import ContextPackInput
from server.memory.api.models_lifecycle import _StackEntry
from server.memory.app import context_pack, stack, stack_knowledge
from server.memory.domain.context_layout import _DEFAULT_SECTION_TOKENS
from server.settings import settings


@pytest.fixture
def world(monkeypatch):
    clock = SimpleNamespace(at=datetime(2025, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(source_access, "now", lambda: clock.at)
    monkeypatch.setattr(settings, "knowledge_source_acl_stale_seconds", 5)
    state = SimpleNamespace(revoked=set(), renders=[], checks=[], expire_on_stack=False)
    items = {}
    for index, name in enumerate(("direct", "stack"), 1):
        ns = "acl-" + str(index) * 24
        sid = (
            str(index) * 8
            + "-"
            + str(index) * 4
            + "-"
            + str(index) * 4
            + "-"
            + str(index) * 4
            + "-"
            + str(index) * 12
        )
        uri = "gdrive:file#" + sid
        items[name] = source_access.Admitted(
            "docs",
            {"namespace": ns, "source_uri": uri, "chunk_idx": 0, "doc_version": 10, "revision": 0},
            {
                "namespace": ns,
                "source_uri": uri,
                "version": 10,
                "revision": 0,
                "ingested_at": clock.at,
                "tags": ["source-id:" + sid],
            },
            AccessList(emails=("lee@example.com",)),
            reader_emails=frozenset({"lee@example.com"}),
        )
    plan = source_access.ReadPlan(
        "acme",
        sources={
            (
                item.candidate["namespace"],
                item.document["tags"][0].removeprefix("source-id:"),
            ): "docs"
            for item in items.values()
        },
        reader=ReaderIdentity("lee", frozenset({"lee@example.com"}), frozenset()),
        marked=True,
    )
    plan.listing_starts = {key: clock.at for key in plan.sources}

    async def check(candidates, **_kwargs):
        found = []
        for candidate in candidates:
            name = next(
                name
                for name, item in items.items()
                if item.candidate["source_uri"] == candidate["source_uri"]
            )
            state.checks.append(name)
            if name == "stack" and state.expire_on_stack:
                clock.at += timedelta(seconds=6)
                stack_item = items["stack"]
                stack_key = (
                    stack_item.candidate["namespace"],
                    stack_item.document["tags"][0].removeprefix("source-id:"),
                )
                plan.listing_starts[stack_key] = clock.at
                state.expire_on_stack = False
            if name not in state.revoked:
                found.append(items[name])
        return found

    async def keys(org, model, wanted, *, embedded_only=True):
        assert org == "acme" and model == "fake"
        return wanted

    monkeypatch.setattr(plan, "check", check)
    monkeypatch.setattr(source_candidates, "chunk_keys", keys)

    def passage(name):
        return _SearchHit(
            namespace="docs",
            source_uri=name,
            chunk_idx=0,
            content=name,
            tags=[],
            distance=0.1,
            similarity=0.9,
        )

    class Renderer:
        def __init__(self, marked):
            self.marked = marked

        async def render(self, rows, dropped):
            state.renders.append("ordinary-direct")
            return KnowledgeSearchOutput(
                results=[passage("ordinary-direct")], embedding_model="fake"
            )

    def build(*, private=True, large=False):
        input = ContextPackInput(
            org="acme",
            namespace="docs",
            agent="reader",
            query="question",
            include_knowledge=True,
            section_token_budgets={"memory_stack": 80},
            token_budget=2000,
            only_sections=["profile", "knowledge", "memory_stack"],
        )
        prefix = context_pack._PackState(input, dict(_DEFAULT_SECTION_TOKENS[input.mode]))
        prefix.put(
            "profile",
            "ordinary profile",
            citations=["profile:one"],
            source_count=1,
            extra_meta={"nested": {"rows": ["stable"]}},
        )
        direct_hit = passage("private-direct" if private else "ordinary-direct")
        if large and private:
            direct_hit.content = "private-direct" * 20
        first = CheckedSearch(
            KnowledgeSearchOutput(results=[direct_hit], embedding_model="fake"),
            SearchProofs(plan, "fake", (items["direct"],)) if private else None,
            Renderer(private),
            [],
            0,
        )
        direct = CheckedPassages(first, None, "reader", 2, None, [direct_hit])
        fill = stack._StackFill(budget=200)
        fill.add(
            _StackEntry(
                section="pinned",
                memory_key="kept-row",
                memory_type="fact",
                value="ordinary pinned",
                tokens=4,
                namespace="docs",
            )
        )
        mixed = ChunkHit(
            "docs",
            "private-stack" if private else "ordinary-stack",
            0,
            "private-stack" * (50 if large and private else 1) if private else "ordinary-stack",
            [],
            0.1,
        )
        ordinary = ChunkHit("docs", "ordinary-stack", 0, "ordinary-stack", [], 0.2)
        knowledge = stack_knowledge.StackKnowledge(
            [mixed], [ordinary], SearchProofs(plan, "fake", (items["stack"],)) if private else None
        )
        checked_stack = stack.CheckedStack(fill, knowledge, private)
        return context_pack.CheckedPack(prefix, checked_stack, direct, private)

    return SimpleNamespace(state=state, clock=clock, plan=plan, build=build)


@pytest.mark.parametrize("pool", ["direct", "stack"])
async def test_loss_of_either_pool_rebuilds_the_entire_ordinary_pack(world, pool):
    checked = world.build()
    mixed = await checked.current()
    assert "private-direct" in mixed.rendered and "private-stack" in mixed.rendered
    world.state.revoked.add(pool)
    actual = await checked.current()
    expected = await world.build(private=False).current()
    assert actual.model_dump() == expected.model_dump()
    assert actual._served_rows == expected._served_rows
    assert checked.lost and checked.fresh()
    assert world.state.renders == ["ordinary-direct"]
    assert (await checked.current()).model_dump() == expected.model_dump()
    assert (await checked.ordinary())._served_rows == expected._served_rows
    assert world.state.renders == ["ordinary-direct"]


async def test_private_truncation_and_counts_do_not_survive_ordinary_rebuild(world):
    checked = world.build(large=True)
    mixed = await checked.current()
    assert mixed.truncated
    world.state.revoked.add("direct")
    actual = await checked.current()
    expected = await world.build(private=False).current()
    assert not actual.truncated
    assert actual.model_dump() == expected.model_dump()
    assert actual._served_rows == expected._served_rows
    assert actual.section_meta["memory_stack"]["source_count"] == 2


async def test_last_child_wait_cannot_leave_an_earlier_pool_expired(world):
    checked = world.build()
    world.state.expire_on_stack = True
    actual = await checked.current()
    expected = await world.build(private=False).current()
    assert "direct" in world.state.checks and "stack" in world.state.checks
    assert actual.model_dump() == expected.model_dump()
    assert actual._served_rows == expected._served_rows and checked.lost


async def test_cpu_render_deadline_crossing_discards_the_whole_mixed_pack(world, monkeypatch):
    render = context_pack._render_checked_pack
    calls = []

    def expire(prefix, stack_output, hits, **kwargs):
        output = render(prefix, stack_output, hits, **kwargs)
        calls.append(output.rendered)
        if len(calls) == 1:
            world.clock.at += timedelta(seconds=6)
        return output

    monkeypatch.setattr(context_pack, "_render_checked_pack", expire)
    actual = await world.build().current()
    expected = await world.build(private=False).current()
    assert len(calls) == 3 and "private-direct" in calls[0]
    assert actual.model_dump() == expected.model_dump()
    assert actual._served_rows == expected._served_rows


async def test_repeated_outputs_and_nested_metadata_never_mutate_the_prefix(world):
    checked = world.build()
    first = await checked.current()
    first.section_meta["profile"]["nested"]["rows"].append("caller mutation")
    first.section_meta["profile"]["citations"].append("caller mutation")
    first.sections["profile"] = "caller mutation"
    second = await checked.current()
    assert second.section_meta["profile"]["nested"]["rows"] == ["stable"]
    assert second.section_meta["profile"]["citations"] == ["profile:one"]
    assert "caller mutation" not in second.rendered
    ordinary = await checked.ordinary()
    ordinary.section_meta["profile"]["nested"]["rows"].append("other mutation")
    assert (await checked.current()).section_meta["profile"]["nested"]["rows"] == ["stable"]


@pytest.mark.parametrize("section", ["memory_stack", "knowledge"])
async def test_optional_renderer_failure_discards_its_partial_mutation(world, monkeypatch, section):
    def broken(st, output):
        st.put(section, "partial private section", source_count=77)
        raise ValueError("render failed")

    monkeypatch.setattr(context_pack, "_put_" + section, broken)
    checked = world.build()
    actual = await checked.current()
    assert section not in actual.sections and "partial private section" not in actual.rendered
    assert actual.sections["profile"] == "ordinary profile"
    assert actual.section_meta["profile"]["source_count"] == 1
    assert actual.section_count == 2

    async def app(scope, receive, send):
        for proof in checked.publication_proofs():
            source_publication.retain(proof)
        world.state.revoked.add("stack" if section == "memory_stack" else "direct")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": actual.model_dump_json().encode()})

    sent = []

    async def send(message):
        sent.append(message)

    await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
    assert "partial private section" not in sent[-1]["body"].decode()


@pytest.mark.parametrize("pool", ["direct", "stack"])
@pytest.mark.parametrize("change", ["access", "version", "chunk"])
async def test_accounting_wait_cannot_publish_a_mixed_pack_after_either_pool_changes(
    world, monkeypatch, pool, change
):
    sent = []
    namespace = "acl-" + ("1" if pool == "direct" else "2") * 24

    async def revoke():
        if change == "access":
            world.state.revoked.add(pool)
        elif change == "version":
            original = world.plan.check

            async def updated(candidates, **kwargs):
                return [
                    replace(item, document={**item.document, "version": 11})
                    if item.candidate["namespace"] == namespace
                    else item
                    for item in await original(candidates, **kwargs)
                ]

            monkeypatch.setattr(world.plan, "check", updated)
        else:

            async def gone(org, model, wanted, *, embedded_only=True):
                return [key for key in wanted if key[0] != namespace]

            monkeypatch.setattr(source_candidates, "chunk_keys", gone)

    async def app(scope, receive, send):
        checked = world.build()
        output = await checked.current()
        assert "private-direct" in output.rendered and "private-stack" in output.rendered
        for proof in checked.publication_proofs():
            source_publication.retain(proof)
        assert request_tail.defer_required(revoke)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": output.model_dump_json().encode()})

    async def send(message):
        sent.append(message)

    with pytest.raises(request_tail.RequiredTailError):
        await request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
    assert sent == [] and world.state.renders == []
