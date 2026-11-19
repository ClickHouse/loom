"""A duplicate-check owner joins its optional PR reader before returning."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.core.errors import VectorCapacityError
from server.core.vectors.admission import current_work
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.tracker.api.models import TrackerDuplicateCheckInput
from server.tracker.app import dupcheck
from server.tracker.domain.similar import Query
from server.tracker.store.embed import TrackerEmbedRepo
from tests.unit.test_vector_read_admission import Driver, NativeBudgetError, client

TITLE = "stream reader retirement retains complete request scope"
BODY = "The complete draft body remains available.\nIts second line remains too."


@pytest.mark.parametrize("mode", ["refusal", "repeat-cancel", "cancel-only", "success"])
async def test_duplicate_check_joins_related_pr_reader(monkeypatch, mode):
    ch, _ = client(monkeypatch)
    pr_started, checking, cleaning, release, retired = (asyncio.Event() for _ in range(5))
    owned = set()
    seen = SimpleNamespace(refusal=None, detail=None, headers=None, candidate_calls=0, pr_calls=0)
    native_error = NativeBudgetError(158)

    class NativeDriver(Driver):
        async def query(self, sql, **kwargs):
            self.started += 1
            self.settings.append(kwargs["settings"])
            work = current_work()
            assert work is not None
            assert (work.org, work.namespaces, work.weight) == ("one-org", ("tracker-example",), 1)
            assert "FROM loom.qa_item_embeddings FINAL" in sql
            assert "cosineDistance" in sql
            assert "org = {org:String}" in sql and "namespace = {ns:String}" in sql
            assert kwargs["parameters"] == {
                "org": "one-org",
                "ns": "tracker-example",
                "q": [1.0, 0.0],
                "dim": 2,
                "limit": 50,
                "kinds": ["issue"],
            }
            assert kwargs["settings"]["max_rows_to_read"] == settings.vector_scan_max_rows_to_read
            assert kwargs["settings"]["read_overflow_mode"] == "throw"
            if mode in {"refusal", "repeat-cancel"}:
                raise native_error
            return SimpleNamespace(result_rows=[], column_names=[], summary={})

    driver = NativeDriver()
    monkeypatch.setattr(ch, "_client", driver)
    repo = TrackerEmbedRepo(ch)

    class Source:
        async def duplicate_gold(self, org, namespace, *, limit):
            assert (org, namespace, limit) == ("one-org", "tracker-example", 2000)
            return []

    async def candidate_reads(src, inp, *, wall, status):
        seen.candidate_calls += 1
        assert isinstance(src, Source)
        assert (inp.org, inp.namespace, inp.title, inp.body, wall) == (
            "one-org",
            "tracker-example",
            TITLE,
            BODY,
            None,
        )
        await pr_started.wait()
        checking.set()
        if mode == "cancel-only":
            await asyncio.Event().wait()
        try:
            rows = await repo.nearest(
                inp.org, inp.namespace, vector=[1.0, 0.0], dim=2, kinds=["issue"]
            )
        except VectorCapacityError as error:
            seen.refusal = error
            seen.detail, seen.headers = error.to_detail(), error.headers()
            raise
        assert rows == []
        status["vector"] = "empty"
        return {
            "q": Query(text=TITLE + "\n" + BODY, title=TITLE, terms=["stream", "retirement"]),
            "item": None,
            "ranked": [],
            "record": {},
            "meta": {},
            "draft_paths": [],
            "leg_heads": {},
            "fingerprint": [],
        }

    async def no_fit(org, namespace):
        assert (org, namespace) == ("one-org", "tracker-example")
        return None

    async def related_pr(inp):
        seen.pr_calls += 1
        assert (inp.org, inp.namespace, inp.title, inp.body) == (
            "one-org",
            "code-example",
            TITLE,
            BODY,
        )
        task = asyncio.current_task()
        assert task is not None
        owned.add(task)
        pr_started.set()
        if mode == "success":
            retired.set()
            return {
                "verdict": "related",
                "items": [
                    {
                        "pr_number": 109741,
                        "title": TITLE,
                        "state": "open",
                        "relation": "related",
                        "score": 0.75,
                    }
                ],
            }
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            retired.set()

    monkeypatch.setattr(dupcheck, "candidates", candidate_reads)
    monkeypatch.setattr(dupcheck, "cached_fit", no_fit)
    monkeypatch.setattr(dupcheck.tsource, "tracker_source", lambda org, namespace: Source())
    monkeypatch.setattr("server.code.app.pr_check.handle_code_pr_check", related_pr)
    draft = TrackerDuplicateCheckInput(
        org="one-org", namespace="code-example", title=TITLE, body=BODY, rerank=False
    )
    assert draft.related_prs is True
    plan = dupcheck.DuplicateCheckPlan(draft, ("tracker-example",), "code-example", False, False)
    assert OPERATIONS_BY_NAME["tracker.duplicate_check"].name == "tracker.duplicate_check"
    caller = asyncio.create_task(dupcheck.execute_duplicate_check(plan))
    owned.add(caller)
    try:
        await asyncio.wait_for(pr_started.wait(), 1)
        if mode == "success":
            out = await asyncio.wait_for(asyncio.shield(caller), 1)
            assert out.verdict == "none" and out.epistemic == "uncalibrated"
            assert out.items == [] and out.closest == [] and out.nearest == []
            assert out.legs == {"vector": "empty"}
            assert out.query == {
                "number": 0,
                "known": False,
                "terms": ["stream", "retirement"],
                "tokens": 0,
                "frames": [],
                "frames_source": "",
                "mentions": [],
                "component": "",
                "versions": [],
                "error_code": 0,
                "paths": [],
                "before": "",
                "record": "skipped",
            }
            assert out.related_prs == [
                {
                    "number": 109741,
                    "title": TITLE,
                    "state": "open",
                    "relation": "related",
                    "score": 0.75,
                }
            ]
            assert (
                out.answer
                == "no reported issue matches; a PR may already address this: #109741 (open)"
            )
        else:
            await asyncio.wait_for(checking.wait(), 1)
            if mode == "cancel-only":
                caller.cancel()
            await asyncio.wait_for(cleaning.wait(), 1)
            assert not caller.done(), (
                "duplicate_check returned while its related-PR reader was still retiring"
            )
            assert not retired.is_set()
            if mode in {"repeat-cancel", "cancel-only"}:
                caller.cancel()
                await asyncio.sleep(0)
                caller.cancel()
                await asyncio.sleep(0)
                assert not caller.done() and not retired.is_set()
            release.set()
            if mode == "cancel-only":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(caller), 1)
            else:
                with pytest.raises(VectorCapacityError) as caught:
                    await asyncio.wait_for(asyncio.shield(caller), 1)
                assert caught.value is seen.refusal
                assert caught.value.__cause__ is native_error
                assert caught.value.status_code == 429
                assert (
                    caught.value.to_detail()
                    == seen.detail
                    == {
                        "code": "VECTOR_CAPACITY_REFUSED",
                        "message": "the complete vector read exceeded its native budget",
                        "reason": "rows",
                        "retry_after_s": 1,
                    }
                )
                assert caught.value.headers() == seen.headers == {"Retry-After": "1"}
        assert retired.is_set() and all(task.done() for task in owned)
    finally:
        release.set()
        for task in owned:
            if not task.done():
                task.cancel()
        await asyncio.wait_for(asyncio.gather(*owned, return_exceptions=True), 1)
        assert seen.candidate_calls == seen.pr_calls == 1
        assert driver.started == (0 if mode == "cancel-only" else 1)
        assert driver.active == 0
        assert ch._inflight == {} and ch._retired == {}
        if mode in {"refusal", "repeat-cancel"}:
            assert isinstance(seen.refusal, VectorCapacityError)
            assert seen.refusal.status_code == 429
