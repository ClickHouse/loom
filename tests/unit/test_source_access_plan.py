"""Every mirror leg shares source freshness, strict permissions and failure policy."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from server.auth.jwt import AuthInfo
from server.auth.reader_identity import ReaderIdentity
from server.core import request_work
from server.knowledge.app import source_access as app
from server.knowledge.domain.access_list import AccessList, to_json
from server.knowledge.domain.serving import Serving
from server.operations.base import Role

SID = "11111111-1111-4111-8111-111111111111"
MIRROR = "acl-" + "1" * 24
URI = "gdrive:file#" + SID
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch):
    token = request_work._current.set(None)
    bag = request_work.begin()
    auth = AuthInfo(
        subject="lee",
        org="acme",
        namespaces=("docs",),
        roles=(Role.VIEWER,),
        session_id="sid",
        is_service_account=False,
    )
    doc = {
        "org": "acme",
        "namespace": MIRROR,
        "source_uri": URI,
        "tags": ["source-id:" + SID],
        "version": 10,
        "revision": 0,
        "title": "checked title",
        "superseded_at": None,
        "access_json": to_json(AccessList(emails=("lee@example.com",))),
    }
    mocks = {
        "sources": AsyncMock(return_value=[{"namespace": "docs", "source_id": SID}]),
        "mirrors": AsyncMock(return_value={MIRROR: "docs"}),
        "listings": AsyncMock(
            return_value=[{"namespace": MIRROR, "source_id": SID, "started_at": NOW}]
        ),
        "metadata": AsyncMock(side_effect=lambda *_a, **_k: [dict(doc)]),
        "groups": AsyncMock(return_value=[]),
        "heads": AsyncMock(return_value={}),
        "reader": AsyncMock(
            return_value=ReaderIdentity("lee", frozenset({"lee@example.com"}), frozenset())
        ),
        "owner": AsyncMock(return_value="lee"),
        "gate": AsyncMock(),
        "bag": bag,
        "doc": doc,
    }
    monkeypatch.setattr(app, "current_auth", lambda: auth)
    monkeypatch.setattr(app.settings, "auth_required", True)
    monkeypatch.setattr(app.settings, "knowledge_source_acl_stale_seconds", 3600)
    monkeypatch.setattr(app, "now", lambda: NOW)
    monkeypatch.setattr(app, "check_authorized", mocks["gate"])
    monkeypatch.setattr(app, "resolve_reader", mocks["reader"])
    monkeypatch.setattr(app, "canonical_subject", mocks["owner"])
    monkeypatch.setattr(app.pg, "enabled", lambda: False)
    monkeypatch.setattr(app.store, "active_sources", mocks["sources"])
    monkeypatch.setattr(app.mirrors_repo, "mirrors_of", mocks["mirrors"])
    monkeypatch.setattr(app.store, "listings", mocks["listings"])
    monkeypatch.setattr(app.store, "metadata", mocks["metadata"])
    monkeypatch.setattr(app.store, "group_members", mocks["groups"])
    monkeypatch.setattr(app.store, "heads", mocks["heads"])
    yield mocks
    request_work._current.reset(token)


def candidate(**changes):
    return {"namespace": MIRROR, "source_uri": URI, "doc_version": 10, "revision": 0, **changes}


async def test_owner_resolution_is_shared_within_one_batch_and_fresh_in_the_next(world):
    world["doc"]["access_json"] = to_json(AccessList(owner="owner@example.com"))
    plan = await app.prepare("acme", ["docs"])
    rows = [candidate(chunk_idx=i) for i in range(64)]
    assert len(await plan.check(rows, chunks=True)) == 64
    assert world["owner"].await_count == 1
    world["owner"].return_value = "somebody-else"
    assert await plan.check(rows, chunks=True) == []
    assert world["owner"].await_count == 2


async def test_active_source_marks_even_without_admitted_documents(world):
    plan = await app.prepare("acme", ["docs"])
    world["doc"]["access_json"] = to_json(AccessList(emails=("other@example.com",)))
    assert await plan.check([candidate()], chunks=True) == []
    assert plan.marked and not plan.unread and world["bag"].mirror_marked
    world["gate"].assert_awaited_once()


async def test_checked_document_retains_its_scope_and_metadata(world):
    plan = await app.prepare("acme", ["docs"])
    [admitted] = await plan.check([candidate()], chunks=True)
    assert admitted.namespace == "docs" and admitted.candidate == candidate()
    assert admitted.document == world["doc"]


@pytest.mark.parametrize("stage", ["sources", "mirrors", "listings", "reader"])
async def test_unconditional_read_failure_has_one_public_marker(world, stage):
    world[stage].side_effect = RuntimeError("private row or backend cause")
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True) == []
    assert plan.marked and plan.unread and world["bag"].mirror_unread


async def test_no_active_mirrored_source_runs_no_leg(world):
    world["sources"].return_value = []
    plan = await app.prepare("acme", ["docs"])
    assert not plan.marked and not world["bag"].mirror_marked
    world["mirrors"].assert_not_awaited()
    world["reader"].assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["auth_off", "missing_binding", "stale_listing", "future_listing", "wrong_source"]
)
async def test_missing_scope_or_freshness_admits_nothing(world, monkeypatch, change):
    if change == "auth_off":
        monkeypatch.setattr(app.settings, "auth_required", False)
    elif change == "missing_binding":
        world["mirrors"].return_value = {}
    elif change in {"stale_listing", "future_listing"}:
        world["listings"].return_value[0]["started_at"] = NOW + timedelta(
            seconds=-3601 if change == "stale_listing" else 1
        )
    else:
        world["doc"]["tags"] = ["source-id:22222222-2222-4222-8222-222222222222"]
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True) == []
    assert not plan.unread


async def test_unbound_direct_helper_serves_no_mirror(world):
    request_work._current.set(None)
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True) == []
    world["metadata"].assert_not_awaited()


@pytest.mark.parametrize("stage", ["metadata", "heads", "groups", "owner"])
async def test_candidate_failure_discards_prior_results_without_read_marker(
    world, monkeypatch, stage
):
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True)
    if stage == "heads":
        monkeypatch.setattr(app.pg, "enabled", lambda: True)
    if stage == "groups":
        world["doc"]["access_json"] = to_json(AccessList(groups=("team@example.com",)))
    if stage == "owner":
        world["doc"]["access_json"] = to_json(AccessList(owner="alias@example.com"))
    world[stage].side_effect = RuntimeError("private candidate error")
    assert await plan.check([candidate()], chunks=True) == []
    assert plan.failed and not plan.unread and not world["bag"].mirror_unread
    assert await plan.check([candidate()], chunks=True) == []


@pytest.mark.parametrize(
    "state", ["missing", "deleted", "wrong_revision", "missing_chunk_state", "old_chunk_version"]
)
async def test_pg_requires_current_live_head_and_recorded_chunk_pair(world, monkeypatch, state):
    monkeypatch.setattr(app.pg, "enabled", lambda: True)
    head = app.store.ReadHead(dict(world["doc"]), Serving(False, 0, 0, 10))
    if state == "deleted":
        head = head._replace(serving=Serving(True, 0, 0, 10))
    elif state == "wrong_revision":
        head = head._replace(serving=Serving(False, 1, 1, 10))
    elif state == "missing_chunk_state":
        head = head._replace(serving=Serving(False, 0, None, None))
    elif state == "old_chunk_version":
        head = head._replace(serving=Serving(False, 0, 0, 11))
    world["heads"].return_value = {} if state == "missing" else {(MIRROR, URI): head}
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True) == []
    assert not plan.unread


async def test_pg_metadata_only_acl_uses_recorded_chunks_and_current_permission(world, monkeypatch):
    monkeypatch.setattr(app.pg, "enabled", lambda: True)
    world["doc"]["access_json"] = ""  # publication has not carried a widening yet
    head = {
        **world["doc"],
        "version": 11,
        "access_json": to_json(AccessList(emails=("lee@example.com",))),
    }
    world["heads"].return_value = {
        (MIRROR, URI): app.store.ReadHead(head, Serving(False, 0, 0, 10))
    }
    plan = await app.prepare("acme", ["docs"])
    assert len(await plan.check([candidate()], chunks=True)) == 1
    head["access_json"] = to_json(AccessList(emails=("other@example.com",)))
    assert await plan.check([candidate()], chunks=True) == []


async def test_pg_document_admission_retains_serving_state_for_checked_counts(world, monkeypatch):
    monkeypatch.setattr(app.pg, "enabled", lambda: True)
    serving = Serving(False, 0, 0, 9)
    world["heads"].return_value = {(MIRROR, URI): app.store.ReadHead(dict(world["doc"]), serving)}
    plan = await app.prepare("acme", ["docs"])
    [admitted] = await plan.check([candidate()])
    assert admitted.serving == serving


async def test_group_membership_is_current_and_bound_to_owning_source(world):
    world["doc"]["access_json"] = to_json(AccessList(groups=("team@example.com",)))
    row = {
        "namespace": MIRROR,
        "source_id": SID,
        "group": "team@example.com",
        "members": ["lee@example.com"],
        "read_at": NOW,
        "deleted": False,
    }
    world["groups"].return_value = [row]
    plan = await app.prepare("acme", ["docs"])
    assert len(await plan.check([candidate()], chunks=True)) == 1
    for changed in (
        {"source_id": "other"},
        {"namespace": "acl-" + "2" * 24},
        {"read_at": NOW - timedelta(seconds=3601)},
        {"deleted": True},
    ):
        world["groups"].return_value = [{**row, **changed}]
        assert await plan.check([candidate()], chunks=True) == []


async def test_registry_read_is_sequential_before_listing_evidence(world, monkeypatch):
    from contextlib import contextmanager

    held = []

    @contextmanager
    def sequential():
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    async def sources(*_args, **_kwargs):
        assert held
        return [{"namespace": "docs", "source_id": SID}]

    monkeypatch.setattr(app, "sequential_reads", sequential)
    world["sources"].side_effect = sources
    plan = await app.prepare("acme", ["docs"])
    assert not plan.unread and plan.sources == {(MIRROR, SID): "docs"}


@pytest.mark.parametrize("authority", ["clickhouse", "postgres"])
async def test_wrong_tenant_document_metadata_cannot_authorize(world, monkeypatch, authority):
    world["doc"]["org"] = "another-org"
    if authority == "postgres":
        monkeypatch.setattr(app.pg, "enabled", lambda: True)
        world["heads"].return_value = {
            (MIRROR, URI): app.store.ReadHead(dict(world["doc"]), Serving(False, 0, 0, 10))
        }
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True) == []


@pytest.mark.parametrize("chunks", [False, True])
async def test_pg_head_metadata_must_match_requested_pair_and_serving_revision(
    world, monkeypatch, chunks
):
    monkeypatch.setattr(app.pg, "enabled", lambda: True)
    for change in ({"namespace": "other"}, {"source_uri": "other"}, {"revision": 1}):
        world["heads"].return_value = {
            (MIRROR, URI): app.store.ReadHead({**world["doc"], **change}, Serving(False, 0, 0, 10))
        }
        plan = await app.prepare("acme", ["docs"])
        assert await plan.check([candidate()], chunks=chunks) == []


async def test_listing_freshness_is_checked_when_each_batch_is_admitted(world, monkeypatch):
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True)
    monkeypatch.setattr(app, "now", lambda: NOW + timedelta(seconds=3601))
    assert await plan.check([candidate()], chunks=True) == []
    assert not plan.unread


async def test_ch_content_revision_must_match_current_metadata_without_requiring_metadata_version(
    world,
):
    world["doc"]["revision"] = 1
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate(revision=0)], chunks=True) == []
    assert await plan.check([candidate(revision=2)], chunks=True) == []
    world["doc"]["version"] = 11
    assert len(await plan.check([candidate(revision=1, doc_version=10)], chunks=True)) == 1


@pytest.mark.parametrize("count", [65, 129])
@pytest.mark.parametrize("independent", ["none", "email", "owner", "domain", "anyone"])
async def test_final_pool_rechecks_group_expiry_without_removing_independent_grants(
    world, monkeypatch, count, independent
):
    from server.knowledge.app import source_search

    access = {"groups": ("team@example.com",)}
    if independent == "email":
        access["emails"] = ("lee@example.com",)
    elif independent == "owner":
        access["owner"] = "owner-alias"
    elif independent == "domain":
        access["domains"] = ("example.com",)
    elif independent == "anyone":
        access["anyone"] = True
    world["doc"]["access_json"] = to_json(AccessList(**access))
    world["reader"].return_value = ReaderIdentity(
        "lee", frozenset({"lee@example.com"}), frozenset({"example.com"})
    )
    calls = 0

    async def metadata(_org, pairs):
        nonlocal calls
        calls += 1
        if calls > 1:
            monkeypatch.setattr(app, "now", lambda: NOW + timedelta(seconds=10))
        return [{**world["doc"], "source_uri": uri} for _namespace, uri in pairs]

    world["metadata"].side_effect = metadata
    world["groups"].return_value = [
        {
            "namespace": MIRROR,
            "source_id": SID,
            "group": "team@example.com",
            "members": ["lee@example.com"],
            "read_at": NOW - timedelta(seconds=3599),
            "deleted": False,
        }
    ]
    plan = await app.prepare("acme", ["docs"])
    retained = [
        app.Admitted("docs", candidate(source_uri=f"gdrive:file-{i}#" + SID, chunk_idx=0), {})
        for i in range(count)
    ]
    out = await source_search.recheck(plan, {"semantic": retained})
    assert len(out["semantic"]) == (0 if independent == "none" else count)
    assert not plan.failed and not plan.unread


@pytest.mark.parametrize("change", ["permission", "revision", "listing", "group"])
async def test_content_is_discarded_if_access_changes_during_hydration(world, monkeypatch, change):
    from server.knowledge.app import source_search

    if change == "group":
        world["doc"]["access_json"] = to_json(AccessList(groups=("team@example.com",)))
        world["groups"].return_value = [
            {
                "namespace": MIRROR,
                "source_id": SID,
                "group": "team@example.com",
                "members": ["lee@example.com"],
                "read_at": NOW - timedelta(seconds=3599),
                "deleted": False,
            }
        ]
    plan = await app.prepare("acme", ["docs"])
    [retained] = await plan.check([candidate(chunk_idx=0)], chunks=True)

    async def chunks(_org, _model, _keys, *, embedded_only=True):
        assert embedded_only
        if change == "permission":
            world["doc"]["access_json"] = to_json(AccessList())
        elif change == "revision":
            world["doc"]["revision"] = 1
        else:
            age = 3601 if change == "listing" else 10
            monkeypatch.setattr(app, "now", lambda: NOW + timedelta(seconds=age))
        return [{**retained.candidate, "content": "never returned"}]

    monkeypatch.setattr(source_search.store, "chunks", chunks)
    assert await source_search.hydrate(plan, {"semantic": [retained]}, model="fake") == {
        "semantic": []
    }
    assert not plan.failed and not plan.unread and not world["bag"].mirror_unread


@pytest.mark.parametrize(
    "change", ["inactive", "missing_binding", "ambiguous_binding", "listing", "reader"]
)
async def test_each_batch_refreshes_current_source_and_reader_evidence(world, change):
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True)
    if change == "inactive":
        world["sources"].return_value = []
    elif change == "missing_binding":
        world["mirrors"].return_value = {}
    elif change == "ambiguous_binding":
        world["mirrors"].return_value = {MIRROR: "docs", "acl-" + "2" * 24: "docs"}
    elif change == "listing":
        world["listings"].return_value = []
    else:
        world["reader"].return_value = ReaderIdentity("lee", frozenset(), frozenset())
    assert await plan.check([candidate()], chunks=True) == []
    assert not plan.failed and not plan.unread and not world["bag"].mirror_unread


@pytest.mark.parametrize("stage", ["sources", "mirrors", "listings", "reader"])
async def test_scope_refresh_failures_are_conditional_without_unread(world, stage):
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True)
    world[stage].side_effect = RuntimeError("private refresh failure")
    assert await plan.check([candidate()], chunks=True) == []
    assert plan.failed and not plan.unread and not world["bag"].mirror_unread


async def test_refresh_never_adds_new_source_legs_or_rebinds_initial_sources(world):
    plan = await app.prepare("acme", ["docs"])
    second = "22222222-2222-4222-8222-222222222222"
    world["sources"].return_value.append({"namespace": "docs", "source_id": second})
    world["listings"].return_value.append(
        {"namespace": MIRROR, "source_id": second, "started_at": NOW}
    )
    assert await plan.check([candidate()], chunks=True)
    assert plan.sources == {(MIRROR, SID): "docs"}
    replacement = "acl-" + "2" * 24
    world["mirrors"].return_value = {replacement: "docs"}
    world["listings"].return_value = [
        {"namespace": replacement, "source_id": SID, "started_at": NOW}
    ]
    assert await plan.check([candidate()], chunks=True) == []
    assert not plan.sources and not plan.unread


@pytest.mark.parametrize("independent", ["owner", "anyone"])
async def test_reader_contact_loss_keeps_independent_permission_arms(world, independent):
    world["doc"]["access_json"] = to_json(
        AccessList(
            **{
                "emails": ("lee@example.com",),
                independent: "owner-alias" if independent == "owner" else True,
            }
        )
    )
    plan = await app.prepare("acme", ["docs"])
    assert await plan.check([candidate()], chunks=True)
    world["reader"].return_value = ReaderIdentity("lee", frozenset(), frozenset())
    assert await plan.check([candidate()], chunks=True)
    assert plan.reader.emails == frozenset()
    assert not plan.failed and not plan.unread


@pytest.mark.parametrize("independent", [False, True])
async def test_old_group_proof_cannot_follow_a_changed_verified_email_set(world, independent):
    world["doc"]["access_json"] = to_json(
        AccessList(groups=("group@example.com",), anyone=independent)
    )
    world["groups"].return_value = [
        {
            "namespace": MIRROR,
            "source_id": SID,
            "group": "group@example.com",
            "members": ["lee@example.com"],
            "read_at": NOW,
            "deleted": False,
        }
    ]
    plan = await app.prepare("acme", ["docs"])
    [earlier] = await plan.check([candidate()], chunks=True)
    world["reader"].return_value = ReaderIdentity(
        "lee", frozenset({"other@example.com"}), frozenset()
    )
    later = await plan.check([candidate()], chunks=True)
    assert bool(later) is independent
    assert plan.current(earlier) is independent
    assert not plan.failed and not plan.unread


async def test_overlapping_checks_cannot_restore_a_source_after_observing_its_removal(world):
    plan = await app.prepare("acme", ["docs"])
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def active_sources(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            snapshot = list(world["sources"].return_value)
            started.set()
            await release.wait()
            return snapshot
        return []

    world["sources"].side_effect = active_sources
    earlier = asyncio.create_task(plan.check([candidate()], chunks=True))
    await started.wait()
    later = asyncio.create_task(plan.check([candidate()], chunks=True))
    try:
        await asyncio.sleep(0)
        assert not later.done()
    finally:
        release.set()
        first, last = await asyncio.gather(earlier, later)
    assert first and not last and calls == 2
    assert not plan.current(first[0]) and not plan.sources
    assert not plan.failed and not plan.unread


async def test_a_queued_check_observes_an_earlier_checks_failure(world):
    plan = await app.prepare("acme", ["docs"])
    started, release = asyncio.Event(), asyncio.Event()

    async def failed_metadata(*_args):
        started.set()
        await release.wait()
        raise RuntimeError("private failed evidence")

    world["metadata"].side_effect = failed_metadata
    earlier = asyncio.create_task(plan.check([candidate()], chunks=True))
    await started.wait()
    later = asyncio.create_task(plan.check([candidate()], chunks=True))
    release.set()
    assert await asyncio.gather(earlier, later) == [[], []]
    assert world["metadata"].await_count == 1
    assert plan.failed and not plan.unread


async def test_conditional_refresh_preserves_discovery_sources_and_reader(world):
    plan = await app.prepare("acme", ["docs"])
    initial_reader = plan.discovery_reader
    world["sources"].return_value = []
    world["reader"].return_value = ReaderIdentity("lee", frozenset(), frozenset())
    assert await plan.check([candidate()], chunks=True) == []
    assert plan.discovery_sources == {(MIRROR, SID): "docs"}
    assert plan.discovery_reader is initial_reader
    assert initial_reader.emails == frozenset({"lee@example.com"})
    plan.discovery_sources.clear()
    assert plan.discovery_sources == {(MIRROR, SID): "docs"}
