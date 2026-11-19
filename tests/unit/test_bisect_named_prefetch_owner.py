"""A named bisect owns speculative reads until they retire or are handed over."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from server.code.api.models_history import CodeBisectCandidatesInput
from server.code.app import bisect, bisect_fusion
from server.code.app.bisect_facts import IssueFacts
from server.core.errors import VectorAdmissionError
from server.core.vectors.admission import current_work
from server.settings import settings
from tests.unit.ch_fake import RecordingCH

ORG, CODE, TRACKER = "acme", "code-example", "tracker-example"
PATH, TEXT = "src/Engine.cpp", "The complete reported symptom."
WALL = datetime(2026, 9, 1)
ROW = {
    "pr_number": 1001,
    "title": "Named engine change",
    "author": "writer",
    "merged_at": "2026-08-20 00:00:00",
    "closed_at": "2026-08-20 00:00:00",
    "minor_version": "26.8",
    "affected_file_paths": [PATH],
    "has_test": 1,
    "url": "https://example.test/pull/1001",
}


def input_model():
    # _CodeIdentity and every remaining field have defaults. All identity fields
    # are nevertheless explicit here; issue_number disables unrelated dupcheck work.
    return CodeBisectCandidatesInput(
        org=ORG,
        namespace=CODE,
        agent="reader",
        user_id="-",
        consumer="",
        paths=[PATH],
        text=TEXT,
        issue_number=8000,
        good_version="26.7",
        bad_version="26.8",
        before="2026-09-01 00:00:00",
        marks=False,
    )


async def join_owned(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)


def assert_named_body(out):
    assert out.candidates == [
        {
            "pr_number": 1001,
            "title": ROW["title"],
            "author": ROW["author"],
            "merged_at": ROW["merged_at"],
            "minor_version": "26.8",
            "matched_paths": [],
            "matched_symbols": [],
            "touches_affected": False,
            "kind": "change",
            "has_test": True,
            "url": ROW["url"],
            "score": 3.0,
            "merge_commit_sha": "",
            "merge_sha_source": "",
            "named_by": "caused_by_edge",
        }
    ]
    assert (out.verdict, out.causes, out.epistemic, out.candidates_from) == (
        "graph_named",
        ["caused_by_edge"],
        "lower_bound",
        "none",
    )
    assert out.named_by_graph == [1001] and out.paths == [PATH]
    assert out.frames == [] and out.patterns == [] and out.resolved == []
    assert out.versions_searched == ["26.8", "26.9"]
    assert out.good_version == "26.7" and out.bad_version == "26.8"
    assert out.window == "clean_to_bad_plus_slack"
    assert out.issue == {"number": 8000, "title": "Reported issue"}
    assert out.tracker_namespace == TRACKER and out.tracker_unavailable is False
    assert out.fusion == {} and out.dupcheck_fix == {} and out.fix_blame == {}
    assert out.trace_run_id == "fixture-trace" and out.partial is False


@pytest.fixture
def named_owner(monkeypatch):
    state = SimpleNamespace(
        tasks=set(),
        prefetches=[],
        graph_calls=[],
        window_calls=[],
        attached=[],
        started=asyncio.Event(),
        cleaning=asyncio.Event(),
        release=asyncio.Event(),
        settled=asyncio.Event(),
        graph_error=RuntimeError("graph item read failed"),
        unused_refusal=VectorAdmissionError(
            "unused speculative vector refused", reason="rows", retry_after_s=7
        ),
        graph_mode="named",
        caused=[1001],
        scanned=asyncio.Event(),
        immediate=False,
        raise_during_cleanup=True,
    )
    real_start = bisect.start_prefetch

    def start(*args, **kwargs):
        task = real_start(*args, **kwargs)
        assert task is not None
        state.tasks.add(task)
        state.prefetches.append(task)
        return task

    class Source:
        async def window_prs(self, org, namespace, versions, *, before, after):
            assert (org, namespace, versions, before, after) == (
                ORG,
                TRACKER,
                ["26.8", "26.9"],
                WALL,
                None,
            )
            state.tasks.add(asyncio.current_task())
            state.window_calls.append((org, namespace, list(versions)))
            state.started.set()
            if state.immediate:
                return [dict(ROW)]
            try:
                await state.release.wait()
                return [dict(ROW)]
            finally:
                state.cleaning.set()
                await state.release.wait()
                state.settled.set()
                if state.raise_during_cleanup:
                    raise state.unused_refusal

        async def items_by_number(self, org, namespace, numbers):
            assert (org, namespace, numbers) == (ORG, TRACKER, [1001])
            state.graph_calls.append(list(numbers))
            await state.started.wait()
            if state.immediate:
                # Observe completion without consuming the unused task's error.
                done, _ = await asyncio.wait({state.prefetches[-1]}, timeout=1)
                assert done
            if state.graph_mode == "error":
                raise state.graph_error
            return {1001: dict(ROW)}

    state.source = Source()

    async def authorize(org, namespace, *, hides_answer):
        assert (org, namespace, hides_answer) == (ORG, CODE, True)
        return TRACKER

    async def issue(source, org, namespace, number, **kwargs):
        assert source is state.source and (org, namespace, number) == (ORG, TRACKER, 8000)
        return IssueFacts(
            fact={"number": 8000, "title": "Reported issue"},
            bad="26.8",
            good="26.7",
            opened_at=WALL,
            caused=list(state.caused),
            qa_ok=True,
        ), None

    async def scan(source, org, namespace, tracker, plan, facts, **kwargs):
        assert source is state.source and (org, namespace, tracker) == (ORG, CODE, TRACKER)
        assert kwargs["scan"] is (not bool(state.caused))
        state.scanned.set()
        return [], True, "none"

    async def attach(repo, org, namespace, candidates):
        assert (org, namespace) == (ORG, CODE)
        state.attached.append([c.pr_number for c in candidates])

    async def embed(text):
        assert text == TEXT
        return [1.0, 0.0] if state.immediate else []

    monkeypatch.setattr(bisect, "require_code_intel", lambda: None)
    monkeypatch.setattr(bisect, "resolve_code_identity", lambda org, namespace: (org, namespace))
    monkeypatch.setattr(bisect, "CodeRepo", object)
    monkeypatch.setattr("server.tracker.app.binding.authorized_tracker_namespace", authorize)
    monkeypatch.setattr(
        "server.tracker.app.source.tracker_source", lambda org, namespace: state.source
    )
    monkeypatch.setattr(bisect, "_issue_stage", issue)
    monkeypatch.setattr(bisect, "_candidate_rows", scan)
    monkeypatch.setattr(bisect, "_attach_merge_shas", attach)
    monkeypatch.setattr(bisect, "schedule_code_trace", lambda **kwargs: None)
    monkeypatch.setattr(bisect, "new_trace_run_id", lambda: "fixture-trace")
    monkeypatch.setattr(bisect, "start_prefetch", start)
    monkeypatch.setattr(bisect_fusion, "_embed", embed)
    return state


@pytest.mark.parametrize("outcome", ["error", "named"])
@pytest.mark.parametrize("repeat_cancel", [False, True], ids=["held-cleanup", "repeated-cancel"])
async def test_public_bisect_joins_prefetch_discarded_by_real_named_helper(
    named_owner, outcome, repeat_cancel
):
    state = named_owner
    state.graph_mode = outcome
    caller = asyncio.create_task(bisect.handle_code_bisect_candidates(input_model()))
    waiter = asyncio.create_task(state.cleaning.wait())
    state.tasks.update((caller, waiter))
    try:
        done, _ = await asyncio.wait(
            {caller, waiter}, timeout=1, return_when=asyncio.FIRST_COMPLETED
        )
        assert done and state.started.is_set()
        assert not caller.done(), (
            "public bisect returned before the named helper's discarded prefetch retired"
        )
        assert state.cleaning.is_set() and not state.settled.is_set()
        if repeat_cancel:
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done() and not state.settled.is_set()
        state.release.set()
        if outcome == "error":
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(asyncio.shield(caller), 1)
            assert caught.value is state.graph_error
            assert state.attached == []
        elif repeat_cancel:
            # There was no earlier failure: a cancelled caller still receives
            # cancellation, after the unused child has finished its cleanup.
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(caller), 1)
        else:
            assert_named_body(await asyncio.wait_for(asyncio.shield(caller), 1))
            assert state.attached == [[1001]]
        assert state.settled.is_set()
        assert all(task.done() for task in state.tasks)
    finally:
        state.release.set()
        await join_owned(state.tasks)
        assert state.graph_calls == [[1001]]
        assert len(state.window_calls) == 1 and len(state.prefetches) == 1


async def test_public_bisect_accepts_ordinary_pending_prefetch_handover(named_owner):
    state = named_owner
    state.caused = []
    state.raise_during_cleanup = False
    caller = asyncio.create_task(bisect.handle_code_bisect_candidates(input_model()))
    state.tasks.add(caller)
    try:
        await asyncio.wait_for(state.started.wait(), 1)
        await asyncio.wait_for(state.scanned.wait(), 1)
        assert len(state.prefetches) == 1 and not state.prefetches[0].done()
        assert not caller.done() and state.graph_calls == []
        assert not state.cleaning.is_set()
        state.release.set()
        out = await asyncio.wait_for(asyncio.shield(caller), 1)
        assert out.candidates == [
            {
                "pr_number": 1001,
                "title": ROW["title"],
                "author": ROW["author"],
                "merged_at": ROW["merged_at"],
                "minor_version": "26.8",
                "matched_paths": [PATH],
                "matched_symbols": [],
                "touches_affected": True,
                "kind": "change",
                "has_test": None,
                "url": ROW["url"],
                "score": 1.0,
                "merge_commit_sha": "",
                "merge_sha_source": "",
                "why": ["exact_paths", "subtree", "recency"],
            }
        ]
        assert out.verdict == "candidates" and out.causes == [] and out.named_by_graph == []
        assert out.paths == [PATH] and out.versions_searched == ["26.8", "26.9"]
        assert out.fusion == {
            "window_prs": 1,
            "signals": {
                "scan": 0,
                "vector": 0,
                "lexical": 0,
                "exact_paths": 1,
                "subtree": 1,
                "recency": 1,
            },
            "suspects": {"named": [], "mentioned": []},
            "margin": 1.0,
            "weak": False,
        }
        assert state.attached == [[1001]] and state.settled.is_set()
        assert all(t.done() for t in state.tasks)
    finally:
        state.release.set()
        await join_owned(state.tasks)
        assert len(state.prefetches) == 1 and len(state.window_calls) == 1


async def test_public_named_body_ignores_completed_unused_vector_refusal(named_owner, monkeypatch):
    state = named_owner
    state.immediate = True
    native_calls = []

    def answer(sql, params):
        assert "FROM loom.qa_item_embeddings FINAL" in sql and "cosineDistance" in sql
        assert params == {
            "org": ORG,
            "ns": TRACKER,
            "q": [1.0, 0.0],
            "dim": 2,
            "limit": 200,
            "kinds": ["pr"],
            "nums": [1001],
        }
        work = current_work()
        assert work is not None and (work.org, work.namespaces, work.weight) == (ORG, (TRACKER,), 1)
        native_calls.append((sql, dict(params)))
        raise state.unused_refusal

    RecordingCH().on("FROM loom.qa_item_embeddings", answer).install(
        monkeypatch, "server.tracker.store.embed"
    )
    monkeypatch.setattr(settings, "vector_search_quantized_codes", False)
    try:
        out = await bisect.handle_code_bisect_candidates(input_model())
        assert_named_body(out)
        assert state.attached == [[1001]]
        assert len(state.prefetches) == 1 and state.prefetches[0].done()
        assert state.prefetches[0].exception() is state.unused_refusal
    finally:
        await join_owned(state.tasks)
        assert len(native_calls) == 1 and state.graph_calls == [[1001]]
        assert current_work() is None
