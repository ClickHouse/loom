"""Graded run outcomes and fact proposals, end to end.

A proposal is written, hidden from default reads, served on request, relied
on by two runs, graded by session, listed with its graded counts, left alone
under the manual default, accepted once the owner sets `auto`, and rejected
on a fail at `reject_after_fails`. A regrade with the same verdict is a no-op,
and a later run.complete keeps the grade.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration._eventually import eventually


async def _call(c: httpx.AsyncClient, op: str, body: dict[str, Any], expect: int = 200) -> Any:
    r = await c.post(f"/v1/{op}", json=body)
    assert r.status_code == expect, f"{op}: {r.status_code} {r.text[:400]}"
    return r.json()


def _keys(out: dict[str, Any]) -> list[str]:
    return [h["memory_key"] for h in out.get("results", [])]


@pytest.mark.asyncio
async def test_proposal_lifecycle_on_graded_runs(control_plane_url: str) -> None:
    org, ns = "grades-test", f"grades-{uuid.uuid4().hex[:6]}"
    ident = {"org": org, "namespace": ns, "agent": "w"}
    scope = {"org": org, "namespace": ns}
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        await _call(c, "namespace.register", {**scope, "owner_email": "t@example.com"})
        await _call(
            c,
            "memory.set",
            {
                **ident,
                "memory_key": "fact_a",
                "value": "Merges of wide parts are throttled by the pool",
            },
        )
        wrote = await _call(
            c,
            "memory.set",
            {
                **ident,
                "memory_key": "lesson_p1",
                "proposed": True,
                "value": "Check patch parts before trusting the codes-first vector read",
            },
        )
        assert wrote["status"] == "proposed"
        refused = await _call(
            c,
            "memory.set",
            {**ident, "memory_key": "fact_a", "value": "other", "proposed": True},
            expect=409,
        )
        assert "KEY_HOLDS_FACT" in str(refused)

        query = {**ident, "query": "patch parts codes-first vector read", "top_k": 10}

        async def _served(body: dict[str, Any]) -> bool:
            return "lesson_p1" in _keys(await _call(c, "memory.search", body))

        # Searchable first (the embedding insert is async), then: hidden by default.
        await eventually(
            lambda: _served({**query, "include_proposed": True}),
            description="proposal served under include_proposed",
        )
        assert not await _served(query)

        run_ids = []
        for i in (1, 2):
            rid = (
                await _call(c, "run.begin", {**ident, "goal": f"review {i}", "session_id": "pr_1"})
            )["run_id"]
            await _call(
                c,
                "run.complete",
                {
                    **scope,
                    "run_id": rid,
                    "status": "completed",
                    "memories_used": ["lesson_p1", "fact_a"],
                },
            )
            run_ids.append(rid)
        await _call(c, "run.begin", {**ident, "goal": "still open", "session_id": "pr_1"})
        grade = {
            **scope,
            "session_id": "pr_1",
            "grade": "pass",
            "source": "maintainer",
            "ref": "finding_x",
        }
        g = await _call(c, "run.grade", grade)
        assert (g["graded"], g["skipped_active"], g["memories_counted"]) == (2, 1, 2)
        assert g["proposals_decided"] == []  # manual by default
        again = await _call(c, "run.grade", grade)
        assert (again["graded"], again["unchanged"]) == (0, 2)

        listing = await _call(c, "memory.proposals", scope)
        p1 = {p["memory_key"]: p for p in listing["proposals"]}["lesson_p1"]
        assert (p1["graded_passes"], p1["graded_fails"], p1["ready"]) == (2, 0, "accept")
        assert listing["policy"]["mode"] == "manual"

        # a re-sent run.complete must not clear the grade (every column carried)
        await _call(
            c,
            "run.complete",
            {**scope, "run_id": run_ids[0], "status": "completed", "memories_used": ["lesson_p1"]},
        )
        run = (await _call(c, "run.get", {**scope, "run_id": run_ids[0]}))["run"]
        assert run["grade"] == "pass" and run["grade_source"] == "maintainer"
        assert len((await _call(c, "run.list", {**scope, "grade": "ungraded"}))["runs"]) == 1

        await _call(
            c,
            "namespace.register",
            {
                **scope,
                "owner_email": "t@example.com",
                "proposal_policy": {
                    "mode": "auto",
                    "min_passes": 2,
                    "max_fails": 0,
                    "reject_after_fails": 1,
                },
            },
        )
        decided = (await _call(c, "run.grade", grade))["proposals_decided"]
        assert [(d["memory_key"], d["decision"]) for d in decided] == [("lesson_p1", "accept")]
        assert (await _call(c, "memory.get", {**ident, "memory_key": "lesson_p1"}))[
            "status"
        ] == "active"
        await eventually(lambda: _served(query), description="accepted fact served by default")

        await _call(
            c,
            "memory.set",
            {
                **ident,
                "memory_key": "lesson_p2",
                "proposed": True,
                "value": "Always rebuild the index after every insert",
            },
        )
        rid = (await _call(c, "run.begin", {**ident, "goal": "review 3", "session_id": "pr_2"}))[
            "run_id"
        ]
        await _call(
            c,
            "run.complete",
            {**scope, "run_id": rid, "status": "completed", "memories_used": ["lesson_p2"]},
        )
        failed = await _call(
            c, "run.grade", {**scope, "run_id": rid, "grade": "fail", "source": "reviewer"}
        )
        assert [(d["memory_key"], d["decision"]) for d in failed["proposals_decided"]] == [
            ("lesson_p2", "reject")
        ]
        assert (await _call(c, "memory.get", {**ident, "memory_key": "lesson_p2"}))[
            "status"
        ] == "rejected"

        await _call(
            c,
            "memory.set",
            {
                **ident,
                "memory_key": "lesson_p3",
                "proposed": True,
                "value": "Prefer the EXCHANGE TABLES swap for repartitions",
            },
        )
        out = await _call(
            c,
            "memory.proposal_decide",
            {**scope, "decision": "accept", "memory_keys": ["lesson_p3", "fact_a", "nope"]},
        )
        by = {r["memory_key"]: r for r in out["results"]}
        assert out["decided"] == 1 and by["lesson_p3"]["status"] == "active"
        assert not by["fact_a"]["decided"] and by["nope"]["reason"] == "not found"


@pytest.mark.asyncio
async def test_run_grade_needs_exactly_one_target(control_plane_url: str) -> None:
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=30) as c:
        base = {"org": "grades-test", "namespace": "grades-x", "grade": "pass"}
        await _call(c, "run.grade", base, expect=422)
        await _call(c, "run.grade", {**base, "run_id": "a", "session_id": "b"}, expect=422)
        await _call(c, "run.grade", {**base, "run_id": "no-such-run"}, expect=404)
