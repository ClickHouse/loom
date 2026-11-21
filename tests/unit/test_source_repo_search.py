"""Repository composition retains source proofs across both search legs."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

from server.core.diagnostics import codes_only
from server.knowledge.api.models_knowledge import (
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)
from server.knowledge.app import knowledge_search as search
from server.knowledge.app import repo_docs, source_access
from server.knowledge.app.source_corpus import LostCorpus
from tests.unit.clock_fake import ManualClock


def hit(uri, score, namespace):
    return _SearchHit(
        source_uri=uri,
        content=uri,
        chunk_idx=0,
        tags=[],
        distance=1 - score,
        similarity=score,
        score=score,
        namespace=namespace,
    )


def install(monkeypatch, *, revoke=False, extra_error=False, base_error=False):
    events = []
    children = {}
    expired = False
    base_ready = asyncio.Event()

    async def resolve(inp):
        events.append("resolve:" + inp.namespace)
        return search.SearchRequest(inp.model_copy(deep=True), (inp.namespace,), ())

    async def prepare(org, namespaces):
        events.append(("prepare", tuple(namespaces)))
        return source_access.ReadPlan(org, marked=True)

    async def run(request, plan):
        nonlocal expired
        ns = request.input.namespace
        events.append(("run", ns, codes_only()))
        if ns == "home":
            if base_error:
                base_ready.set()
                raise RuntimeError("base failed")
            base_ready.set()
        else:
            await base_ready.wait()
            expired = revoke
            if extra_error:
                raise RuntimeError("PRIVATE-SOURCE-CONTENT")
        mixed = KnowledgeSearchOutput(
            results=[hit("private:" + ns, 0.9, ns)],
            embedding_model="fake",
            reranked_by="private-rank",
        )
        ordinary = KnowledgeSearchOutput(
            results=[hit("ordinary:" + ns, 0.4, ns)], embedding_model="fake", stale_dropped=2
        )

        async def check():
            events.append("check:" + ns)
            if expired:
                raise LostCorpus()

        async def render(*_args):
            events.append("ordinary:" + ns)
            return ordinary

        result = search.CheckedSearch(
            mixed,
            SimpleNamespace(check=check, fresh=lambda: not expired, mirrored=True),
            SimpleNamespace(marked=True, render=render),
            [],
            2,
        )
        children[ns] = result
        return result

    async def public(inp):
        return (await run(await resolve(inp), source_access.ReadPlan(inp.org, marked=True))).output

    monkeypatch.setattr(search, "resolve_search", resolve)
    monkeypatch.setattr(search, "run_checked_search", run)
    monkeypatch.setattr(search, "handle_knowledge_search", public)
    monkeypatch.setattr(source_access, "prepare", prepare)
    return events, children


INPUT = KnowledgeSearchInput(org="acme", namespace="home", query="cache", top_k=3)


async def test_sibling_revocation_rebuilds_both_complete_ordinary_searches(monkeypatch):
    events, children = install(monkeypatch, revoke=True)
    out = await repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo")
    assert [h.source_uri for h in out.results] == ["ordinary:home", "ordinary:repo"]
    assert out.reranked_by == "" and out.stale_dropped == 2
    assert events.count("ordinary:home") == events.count("ordinary:repo") == 1
    assert all(child.lost for child in children.values())


async def test_union_is_prepared_before_either_provider_and_both_legs_are_private(monkeypatch):
    events, _ = install(monkeypatch)
    out = await repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo")
    assert ("prepare", ("home", "repo")) in events
    prep = events.index(("prepare", ("home", "repo")))
    assert all(
        prep < i and private
        for i, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "run"
        for private in [event[2]]
    )
    assert [h.source_uri for h in out.results] == ["private:home", "private:repo"]
    assert not codes_only()


async def test_additive_exception_is_generic_and_base_keeps_its_proof(monkeypatch, caplog):
    install(monkeypatch, extra_error=True)
    with caplog.at_level("INFO", logger="loom.repo_docs"):
        out = await repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo")
    assert [h.source_uri for h in out.results] == ["private:home"]
    assert "PRIVATE-SOURCE-CONTENT" not in caplog.text


async def test_base_error_still_propagates(monkeypatch):
    install(monkeypatch, base_error=True)
    with pytest.raises(RuntimeError, match="base failed"):
        await repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo")


@pytest.mark.parametrize("failure", ["error", "timeout"])
async def test_additive_scope_failure_keeps_the_base_and_drops_its_discovery_scope(
    monkeypatch, failure
):
    events, _ = install(monkeypatch)
    original = search.resolve_search
    cleanup = []

    async def resolve(inp):
        if inp.namespace == "repo":
            if failure == "error":
                raise RuntimeError("PRIVATE-SCOPE-DETAIL")
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.append("drained")
        return await original(inp)

    monkeypatch.setattr(search, "resolve_search", resolve)
    monkeypatch.setattr(repo_docs.settings, "knowledge_repo_docs_timeout_s", 0.001)
    out = await repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo")
    assert [h.source_uri for h in out.results] == ["private:home"]
    assert ("prepare", ("home",)) in events
    assert not any(isinstance(e, tuple) and e[:2] == ("run", "repo") for e in events)
    assert cleanup == (["drained"] if failure == "timeout" else [])


async def test_base_scope_failure_never_starts_discovery_or_providers(monkeypatch):
    events, _ = install(monkeypatch)

    async def fail(inp):
        raise RuntimeError("base scope refused")

    monkeypatch.setattr(search, "resolve_search", fail)
    with pytest.raises(RuntimeError, match="base scope refused"):
        await repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo")
    assert events == []


@pytest.mark.parametrize("failure", ["cancel", "base_error"])
async def test_parent_failure_cancels_and_drains_owned_siblings(monkeypatch, failure):
    install(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    active, drained = set(), set()

    async def run(request, plan):
        ns = request.input.namespace
        active.add(ns)
        if len(active) == 2:
            entered.set()
        try:
            if ns == "home" and failure == "base_error":
                await entered.wait()
                raise RuntimeError("base failed")
            await release.wait()
        finally:
            drained.add(ns)

    monkeypatch.setattr(search, "run_checked_search", run)
    task = asyncio.create_task(repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo"))
    await entered.wait()
    if failure == "cancel":
        task.cancel()
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await task
    assert active == drained == {"home", "repo"}


async def test_expired_additive_budget_never_starts_its_provider(monkeypatch):
    events, _ = install(monkeypatch)
    request = await repo_docs.resolve_repo_search(INPUT, repo_namespace="repo")
    from dataclasses import replace

    request = replace(request, extra_deadline=asyncio.get_running_loop().time() - 1)
    result = await repo_docs.run_repo_search(request, source_access.ReadPlan("acme", marked=True))
    assert [h.source_uri for h in result.output.results] == ["private:home"]
    assert not any(isinstance(e, tuple) and e[:2] == ("run", "repo") for e in events)
    assert result.children == (result.base,) and result.extra is None


@pytest.mark.parametrize("child", ["base", "extra"])
async def test_every_child_org_is_checked_before_any_provider(monkeypatch, child):
    from dataclasses import replace

    events, _ = install(monkeypatch)
    request = await repo_docs.resolve_repo_search(INPUT, repo_namespace="repo")
    changed = getattr(request, child)
    changed = replace(changed, input=changed.input.model_copy(update={"org": "other"}))
    request = replace(request, **{child: changed})
    with pytest.raises(ValueError, match="document read scope changed"):
        await repo_docs.run_repo_search(request, source_access.ReadPlan("acme", marked=True))
    assert not any(isinstance(e, tuple) and e[0] == "run" for e in events)


async def test_actual_checked_children_publish_only_their_cached_ordinary_rerenders(monkeypatch):
    from unittest.mock import AsyncMock

    install(monkeypatch)
    request = await repo_docs.resolve_repo_search(INPUT, repo_namespace="repo")
    renderers = []
    children = []
    for ns in ["home", "repo"]:
        renderer = SimpleNamespace(
            marked=True,
            render=AsyncMock(
                return_value=KnowledgeSearchOutput(
                    results=[hit("ordinary:" + ns, 0.4, ns)],
                    embedding_model="ordinary-model",
                    stale_dropped=7,
                )
            ),
        )
        proof = SimpleNamespace(check=AsyncMock(side_effect=LostCorpus()), fresh=lambda: False)
        children.append(
            search.CheckedSearch(
                KnowledgeSearchOutput(
                    results=[hit("private:" + ns, 0.9, ns)],
                    embedding_model="private-model",
                    reranked_by="private-rank",
                ),
                proof,
                renderer,
                [],
                7,
            )
        )
        renderers.append(renderer)
    result = repo_docs.RepoSearch(request, *children)
    assert result.output.reranked_by == "private-rank"
    await result.current()
    out = result.output
    assert [h.source_uri for h in out.results] == ["ordinary:home", "ordinary:repo"]
    assert (out.embedding_model, out.reranked_by, out.stale_dropped) == ("ordinary-model", "", 7)
    await result.current()
    for renderer in renderers:
        renderer.render.assert_awaited_once_with([], 7)


@pytest_asyncio.fixture(loop_scope="function")
async def renewal_clock(monkeypatch):
    before = set(asyncio.all_tasks())
    loop = asyncio.get_running_loop()
    idle_clock = ManualClock(t=loop.time())
    selector = loop._selector
    real_select = selector.select

    def select(timeout=None):
        if timeout is not None:
            idle_clock.advance(max(0.0, timeout))
            timeout = 0
        return real_select(timeout)

    # The watchdog measures idle time; CPU scheduling cannot spend its budget.
    monkeypatch.setattr(loop, "time", idle_clock.monotonic)
    monkeypatch.setattr(selector, "select", select)
    clock = ManualClock()
    active = []

    @asynccontextmanager
    async def timeout_at(deadline):
        async with asyncio.timeout(None) as scope:
            active.append((deadline, scope))
            try:
                yield scope
            finally:
                active.pop()

    # Repository expiry remains explicit and separate from the loop watchdog.
    monkeypatch.setattr(
        repo_docs,
        "asyncio",
        SimpleNamespace(
            get_running_loop=lambda: SimpleNamespace(time=clock.monotonic), timeout_at=timeout_at
        ),
    )
    yield clock, active
    leaked = asyncio.all_tasks() - before - {asyncio.current_task()}
    for task in leaked:
        task.cancel()
    await asyncio.gather(*leaked, return_exceptions=True)
    assert not leaked, "repository renewal leaves no task on its private loop"


@pytest.mark.asyncio(loop_scope="function")
async def test_additive_final_permission_renewal_is_bounded_without_failing_the_base(
    monkeypatch, renewal_clock
):
    events, children = install(monkeypatch)
    clock, active = renewal_clock
    original = search.run_checked_search
    entered = []
    cancelled = []
    cleaned = []

    async def run(request, plan):
        result = await original(request, plan)
        if request.input.namespace == "repo":

            async def wait_forever():
                try:
                    entered.append("repo")
                    deadline, scope = active[-1]
                    assert deadline - clock.monotonic() == 0.005
                    clock.advance(0.005)
                    scope.reschedule(asyncio.get_running_loop().time())
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.append("repo")
                    raise
                finally:
                    cleaned.append("extra proof drained")

            result.proofs.check = wait_forever
        return result

    monkeypatch.setattr(search, "run_checked_search", run)
    monkeypatch.setattr(repo_docs.settings, "knowledge_repo_docs_timeout_s", 0.005)
    out = await asyncio.wait_for(
        repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo"), timeout=0.1
    )
    assert [h.source_uri for h in out.results] == ["private:home"]
    assert entered == ["repo"]
    assert cancelled == ["repo"]
    assert cleaned == ["extra proof drained"]
    assert not children["home"].lost and "check:home" in events


@pytest.mark.asyncio(loop_scope="function")
async def test_expiry_before_renewal_does_not_start_the_extra_proof(monkeypatch, renewal_clock):
    events, children = install(monkeypatch)
    clock, _ = renewal_clock
    original = repo_docs.run_repo_search
    entered = []
    cleaned = []

    async def wait_forever():
        entered.append("repo")
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append("extra proof drained")

    async def expire_before_renewal(request, plan):
        result = await original(request, plan)
        children["repo"].proofs.check = wait_forever
        clock.advance(request.extra_deadline - clock.monotonic())
        return result

    monkeypatch.setattr(repo_docs, "run_repo_search", expire_before_renewal)
    monkeypatch.setattr(repo_docs.settings, "knowledge_repo_docs_timeout_s", 0.005)
    out = await asyncio.wait_for(
        repo_docs.knowledge_with_repo_docs(INPUT, repo_namespace="repo"), timeout=0.1
    )
    assert [h.source_uri for h in out.results] == ["private:home"]
    assert entered == [] and cleaned == []
    assert not children["home"].lost and "check:home" in events


async def test_timed_out_extra_fallback_keeps_loss_sticky_and_preserves_base_rebuild(monkeypatch):
    from unittest.mock import AsyncMock

    events, children = install(monkeypatch)
    original = search.run_checked_search
    canceled = []

    async def run(request, plan):
        result = await original(request, plan)
        if request.input.namespace == "repo":

            async def block(*_args):
                try:
                    await asyncio.Event().wait()
                finally:
                    canceled.append("drained")

            result.proofs.check = AsyncMock(side_effect=LostCorpus())
            result._renderer.render = block
        return result

    monkeypatch.setattr(search, "run_checked_search", run)
    monkeypatch.setattr(repo_docs.settings, "knowledge_repo_docs_timeout_s", 0.01)
    request = await repo_docs.resolve_repo_search(INPUT, repo_namespace="repo")
    result = await repo_docs.run_repo_search(request, source_access.ReadPlan("acme", marked=True))
    out = await asyncio.wait_for(result.current(), timeout=0.2)
    assert [h.source_uri for h in out.results] == ["ordinary:home"]
    assert result.lost and result.extra is None and canceled == ["drained"]
    assert children["repo"]._failed
    assert await result.current() is children["home"].output
    assert events.count("ordinary:home") == 1


async def test_concurrent_composed_renewals_share_one_cached_fallback(monkeypatch):
    events, _ = install(monkeypatch, revoke=True)
    request = await repo_docs.resolve_repo_search(INPUT, repo_namespace="repo")
    result = await repo_docs.run_repo_search(request, source_access.ReadPlan("acme", marked=True))
    outputs = await asyncio.gather(*(result.current() for _ in range(8)))
    assert all(
        [h.source_uri for h in out.results] == ["ordinary:home", "ordinary:repo"] for out in outputs
    )
    assert events.count("ordinary:home") == events.count("ordinary:repo") == 1


async def test_additive_recipe_keeps_resolved_scope_filters_and_cap(monkeypatch):
    install(monkeypatch)
    inp = INPUT.model_copy(update={"top_k": 200, "tags": ["narrow"], "uri_prefix": ["notes:"]})
    request = await repo_docs.resolve_repo_search(inp, repo_namespace="repo")
    inp.tags.append("later")
    inp.uri_prefix.append("later:")
    assert request.base.input.tags == ["narrow"] and request.base.input.uri_prefix == ["notes:"]
    assert request.extra.input.top_k == 20 and request.extra.input.rerank == "none"
    assert request.extra.input.tags == [] and request.extra.input.namespace_scope == "this"
    assert request.namespaces == ["home", "repo"]


async def test_base_wait_can_expire_the_additive_result_without_reusing_its_wire(monkeypatch):
    from dataclasses import replace

    events, children = install(monkeypatch)
    request = await repo_docs.resolve_repo_search(INPUT, repo_namespace="repo")
    result = await repo_docs.run_repo_search(request, source_access.ReadPlan("acme", marked=True))
    result.request = replace(request, extra_deadline=asyncio.get_running_loop().time() + 0.005)
    original = children["home"].proofs.check

    async def check():
        await original()
        await asyncio.sleep(0.01)

    children["home"].proofs.check = check
    out = await result.current()
    assert [h.source_uri for h in out.results] == ["private:home"]
    assert result.extra is None and not children["home"].lost
    assert "ordinary:home" not in events


@pytest.mark.parametrize("force_ordinary", [False, True])
@pytest.mark.parametrize("prior_loss", [False, True])
async def test_completed_ordinary_extra_survives_base_work_after_its_deadline(
    monkeypatch, force_ordinary, prior_loss
):
    from dataclasses import replace

    _, children = install(monkeypatch)
    request = await repo_docs.resolve_repo_search(INPUT, repo_namespace="repo")
    result = await repo_docs.run_repo_search(request, source_access.ReadPlan("acme", marked=True))
    await children["repo"].ordinary()
    children["repo"].lost = prior_loss
    result.request = replace(request, extra_deadline=asyncio.get_running_loop().time() - 1)
    out = await (result.ordinary() if force_ordinary else result.current())
    home = "ordinary:home" if force_ordinary or prior_loss else "private:home"
    assert [h.source_uri for h in out.results] == [home, "ordinary:repo"]
    assert result.extra is children["repo"]
