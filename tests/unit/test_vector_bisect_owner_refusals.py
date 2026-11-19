"""Bisect refuses a missing vector signal instead of ranking a smaller read."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from server.core.errors import VectorAdmissionError, VectorCapacityError, VectorOverloadedError
from server.core.vectors.admission import current_work
from server.settings import settings
from tests.unit.ch_fake import RecordingCH

ORG, CODE, TRACKER = "acme", "code-example", "tracker-example"
WALL = datetime(2026, 9, 1)
TEXT = "Opaque symptom\nThe entire report body remains unchanged."
PATH = "src/Engine.cpp"
ROWS = [
    {
        "pr_number": 1001,
        "title": "Alpha",
        "author": "first",
        "closed_at": "2026-08-20 00:00:00",
        "minor_version": "26.8",
        "affected_file_paths": [],
        "url": "https://example.test/pull/1001",
    },
    {
        "pr_number": 1002,
        "title": "Beta",
        "author": "second",
        "closed_at": "2026-08-10 00:00:00",
        "minor_version": "26.8",
        "affected_file_paths": [],
        "url": "https://example.test/pull/1002",
    },
]


def refused(status):
    if status == 429:
        return VectorAdmissionError("complete tracker read refused", reason="rows", retry_after_s=7)
    assert status == 503
    return VectorOverloadedError("complete tracker read refused", reason="pool", retry_after_s=7)


def assert_refusal(actual, expected, status):
    assert actual is expected and actual.status_code == status
    assert actual.to_detail() == {
        "code": "VECTOR_CAPACITY_REFUSED" if status == 429 else "OVERLOADED",
        "message": str(expected),
        "reason": "rows" if status == 429 else "pool",
        "retry_after_s": 7,
    }
    assert actual.headers() == {"Retry-After": "7"}


async def join_owned(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)


def candidate(number, *, score, why=(), scan=False, named=False):
    row = next(r for r in ROWS if r["pr_number"] == number)
    out = {
        "pr_number": number,
        "title": row["title"],
        "author": row["author"],
        "merged_at": row["closed_at"],
        "minor_version": "26.8",
        "matched_paths": [PATH] if scan else [],
        "matched_symbols": [],
        "touches_affected": scan,
        "kind": "change",
        "has_test": None,
        "url": row["url"],
        "score": score,
        "merge_commit_sha": "",
        "merge_sha_source": "",
    }
    if why:
        out["why"] = list(why)
    if named:
        out["named_cause_by"] = [{"pr_number": 9000, "role": "fix"}]
    return out


@pytest.mark.parametrize(
    "wide,status", [(False, 429), (True, 503)], ids=["initial-fusion", "wider-fusion"]
)
@pytest.mark.parametrize("mode", ["refusal", "ordinary", "complete"])
async def test_consumed_bisect_fusion_keeps_native_refusals(monkeypatch, wide, status, mode):
    from server.code.app import bisect_fusion

    expected = refused(status) if mode == "refusal" else RuntimeError("ordinary tracker outage")
    native_calls, window_calls = [], []

    def answer(sql, params):
        assert "cosineDistance" in sql and "FROM loom.qa_item_embeddings FINAL" in sql
        assert "org = {org:String}" in sql and "namespace = {ns:String}" in sql
        assert "number IN {nums:Array(UInt64)}" in sql and "tombstoned_at IS NULL" in sql
        assert params == {
            "org": ORG,
            "ns": TRACKER,
            "q": [1.0, 0.0],
            "dim": 2,
            "limit": 200,
            "kinds": ["pr"],
            "nums": [1001, 1002],
        }
        work = current_work()
        assert work is not None and (work.org, work.namespaces, work.weight) == (ORG, (TRACKER,), 1)
        native_calls.append((sql, dict(params)))
        if mode != "complete":
            raise expected
        return [
            {
                "kind": "pr",
                "number": n,
                "source_id": str(n),
                "component": "",
                "is_open": 0,
                "created_at": WALL,
                "distance": distance,
            }
            for n, distance in [(1002, 0.1), (1001, 0.2)]
        ]

    RecordingCH().on("FROM loom.qa_item_embeddings", answer).install(
        monkeypatch, "server.tracker.store.embed"
    )
    monkeypatch.setattr(settings, "vector_search_quantized_codes", False)

    async def embed(text):
        assert text == TEXT
        return [1.0, 0.0]

    class Source:
        async def window_prs(self, org, namespace, versions, *, before, after):
            assert (org, namespace, before, after) == (ORG, TRACKER, WALL, None)
            assert versions == (["26.8", "26.9"] if wide else ["26.8"])
            window_calls.append(list(versions))
            return [dict(r) for r in ROWS]

    monkeypatch.setattr(bisect_fusion, "_embed", embed)
    tasks = set()

    async def run():
        if wide:
            pool, rows = await bisect_fusion.wide_extras(
                Source(),
                ORG,
                TRACKER,
                good="26.7",
                bad="26.8",
                master=False,
                versions=["26.8"],
                report_at=WALL,
                in_window=set(),
                text=TEXT,
                paths=[],
            )
            summary = None
        else:
            task = bisect_fusion.start_prefetch(
                Source(), ORG, TRACKER, versions=["26.8"], before=WALL, report_at=WALL, text=TEXT
            )
            assert task is not None
            tasks.add(task)
            pool, summary, rows = await bisect_fusion.fusion_stage(
                task, [], report_at=WALL, before=WALL, text=TEXT, paths=[]
            )
        return pool, summary, rows

    try:
        if mode == "refusal":
            with pytest.raises(VectorCapacityError) as caught:
                await run()
            assert_refusal(caught.value, expected, status)
        else:
            pool, summary, rows = await run()
            order = [1002, 1001] if mode == "complete" else [1001, 1002]
            why = ["vector", "recency"] if mode == "complete" else ["recency"]
            assert [c.as_dict() for c in pool] == [
                candidate(
                    n,
                    score=0.0 if wide else (1.0 if i == 0 else 0.5),
                    why=(["wider_window", *why] if wide else why),
                )
                for i, n in enumerate(order)
            ]
            assert rows == {r["pr_number"]: r for r in ROWS}
            if not wide:
                assert summary == {
                    "window_prs": 2,
                    "signals": {
                        "scan": 0,
                        "vector": 2 if mode == "complete" else 0,
                        "lexical": 0,
                        "exact_paths": 0,
                        "subtree": 0,
                        "recency": 2,
                    },
                    "suspects": {"named": [], "mentioned": []},
                    "margin": 0.016 if mode == "complete" else 0.048,
                    "weak": True,
                }
    finally:
        await join_owned(tasks)
        assert len(native_calls) == 1 and len(window_calls) == 1
        assert current_work() is None


@pytest.fixture
def public_bisect(monkeypatch):
    from server.code.app import bisect

    calls, tasks = [], set()

    class Repo:
        async def merge_commit_of(self, org, namespace, number):
            assert (org, namespace, number) == (ORG, CODE, 9000)
            calls.append(("blame-ledger", number))
            return ""

    class Source:
        async def causes_named_by(self, org, namespace, number):
            assert (org, namespace, number) == (ORG, TRACKER, 9000)
            calls.append(("fix-named", number))
            return [1002]

    async def authorized(org, namespace, *, hides_answer):
        assert (org, namespace, hides_answer) == (ORG, CODE, False)
        return TRACKER

    async def named_or_prefetch(*args, **kwargs):
        return [], None

    async def scan(source, org, namespace, tracker, plan, facts, **kwargs):
        assert (org, namespace, tracker) == (ORG, CODE, TRACKER)
        assert plan.paths == [PATH] and plan.versions == ["26.8", "26.9"]
        calls.append(("scan", list(plan.paths)))
        return (
            [{**r, "merged_at": r["closed_at"], "matched_paths": [PATH]} for r in ROWS],
            True,
            "tracker",
        )

    async def attach(repo, org, namespace, ranked):
        assert (org, namespace) == (ORG, CODE)
        calls.append(("attach", [c.pr_number for c in ranked]))

    monkeypatch.setattr(bisect, "require_code_intel", lambda: None)
    monkeypatch.setattr(bisect, "resolve_code_identity", lambda org, namespace: (org, namespace))
    monkeypatch.setattr(bisect, "CodeRepo", Repo)
    monkeypatch.setattr("server.tracker.app.binding.authorized_tracker_namespace", authorized)
    monkeypatch.setattr("server.tracker.app.source.tracker_source", lambda org, namespace: Source())
    monkeypatch.setattr(bisect, "_candidate_rows", scan)
    monkeypatch.setattr(bisect, "_named_or_prefetch", named_or_prefetch)
    monkeypatch.setattr(bisect, "_attach_merge_shas", attach)
    monkeypatch.setattr(bisect, "new_trace_run_id", lambda: "fixture-trace")
    monkeypatch.setattr(bisect, "schedule_code_trace", lambda **kwargs: None)
    return SimpleNamespace(module=bisect, calls=calls, tasks=tasks)


def bisect_input():
    from server.code.api.models_history import CodeBisectCandidatesInput

    # _CodeIdentity gives all five identity fields defaults. Spell them out here;
    # the bounded model's remaining fields all have defaults and this shape is valid.
    return CodeBisectCandidatesInput(
        org=ORG,
        namespace=CODE,
        agent="reader",
        user_id="-",
        consumer="",
        paths=[PATH],
        text=TEXT,
        good_version="26.7",
        bad_version="26.8",
        before="2026-09-01 00:00:00",
        marks=False,
    )


def install_duplicate_child(monkeypatch, state, mode, before_raise=None):
    from server.tracker.api.models import TrackerDuplicateCheckOutput

    expected = (
        refused(503) if mode == "refusal" else RuntimeError("ordinary duplicate-check outage")
    )

    async def check(inp):
        state.tasks.add(asyncio.current_task())
        assert (inp.org, inp.namespace, inp.title, inp.body) == (
            ORG,
            CODE,
            "Opaque symptom",
            "The entire report body remains unchanged.",
        )
        assert inp.related_prs is False and inp.rerank is False
        assert inp.frames == [] and inp.paths == [PATH] and inp.as_of == "2026-09-01 00:00:00"
        state.calls.append(("duplicate", inp.namespace))
        if before_raise is not None:
            await before_raise.wait()
        if mode != "complete":
            raise expected
        return TrackerDuplicateCheckOutput(
            verdict="duplicate_of",
            items=[
                {
                    "kind": "issue",
                    "number": 8000,
                    "namespace": TRACKER,
                    "state": "closed",
                    "fixed_in": "26.9",
                    "fixed_in_source": "release_tag",
                    "links": [{"number": 9000, "state": "merged"}],
                }
            ],
        )

    monkeypatch.setattr("server.tracker.app.dupcheck.handle_tracker_duplicate_check", check)
    return expected


@pytest.mark.parametrize("mode", ["refusal", "ordinary", "complete"])
async def test_public_bisect_keeps_duplicate_fix_refusal(monkeypatch, public_bisect, mode):
    state = public_bisect
    expected = install_duplicate_child(monkeypatch, state, mode)
    try:
        if mode == "refusal":
            with pytest.raises(VectorCapacityError) as caught:
                await state.module.handle_code_bisect_candidates(bisect_input())
            assert_refusal(caught.value, expected, 503)
            assert not any(c[0] == "attach" for c in state.calls)
        else:
            out = await state.module.handle_code_bisect_candidates(bisect_input())
            complete = mode == "complete"
            order = [1002, 1001] if complete else [1001, 1002]
            assert out.candidates == [
                candidate(n, score=1.0, scan=True, named=complete and n == 1002) for n in order
            ]
            assert (out.verdict, out.causes, out.epistemic, out.candidates_from) == (
                "candidates",
                [],
                "lower_bound",
                "tracker",
            )
            assert out.paths == [PATH] and out.frames == [] and out.patterns == []
            assert out.versions_searched == ["26.8", "26.9"]
            assert out.good_version == "26.7" and out.bad_version == "26.8"
            assert out.window == "clean_to_bad_plus_slack" and out.tracker_namespace == TRACKER
            assert (
                out.fusion == {}
                and out.trace_run_id == "fixture-trace"
                and out.tracker_unavailable is False
            )
            assert out.widened_by == (["dupcheck_fix"] if complete else [])
            assert out.fix_blame == (
                {"fix_pr": 9000, "skipped": "fix_not_in_ledger", "named_by_fix": [1002]}
                if complete
                else {}
            )
            assert out.dupcheck_fix == (
                {
                    "status": "matched",
                    "issue": 8000,
                    "fix_pr": 9000,
                    "match": "duplicate",
                    "namespace": TRACKER,
                    "note": "the report is a duplicate of #8000, fixed by #9000; the candidates below lead with what that fix's changed lines and cause statement point at",
                }
                if complete
                else {}
            )
            assert ("attach", order) in state.calls
    finally:
        await join_owned(state.tasks)
        assert len([c for c in state.calls if c[0] == "duplicate"]) == 1


async def test_public_bisect_joins_owned_prefetch_before_duplicate_refusal(
    monkeypatch, public_bisect
):
    state = public_bisect
    started, cleaning, release, settled = (asyncio.Event() for _ in range(4))
    expected = install_duplicate_child(monkeypatch, state, "refusal", started)

    async def pending():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            settled.set()

    async def named_or_prefetch(*args, **kwargs):
        task = asyncio.create_task(pending())
        state.tasks.add(task)
        kwargs["owned"].append(task)
        return [], task

    monkeypatch.setattr(state.module, "_named_or_prefetch", named_or_prefetch)
    caller = asyncio.create_task(state.module.handle_code_bisect_candidates(bisect_input()))
    waiter = asyncio.create_task(cleaning.wait())
    state.tasks.update((caller, waiter))
    try:
        done, _ = await asyncio.wait(
            {caller, waiter}, timeout=1, return_when=asyncio.FIRST_COMPLETED
        )
        assert done and cleaning.is_set(), "duplicate refusal did not begin owned prefetch cleanup"
        assert not caller.done() and not settled.is_set(), (
            "bisect returned before owned prefetch finished"
        )
        caller.cancel()
        await asyncio.sleep(0)
        caller.cancel()
        await asyncio.sleep(0)
        assert not caller.done() and not settled.is_set()
        release.set()
        with pytest.raises(VectorCapacityError) as caught:
            await asyncio.wait_for(asyncio.shield(caller), 1)
        assert_refusal(caught.value, expected, 503)
        assert settled.is_set()
        assert all(t.done() for t in state.tasks)
    finally:
        release.set()
        await join_owned(state.tasks)
