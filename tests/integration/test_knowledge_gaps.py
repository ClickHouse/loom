"""Knowledge gaps against a real ClickHouse: seeded misses grouped by the worker's build
(the privacy threshold in SQL, the merge, the marker row), served by the
knowledge.gaps.list handler, and removed by a user erasure's steps. The handler runs
in-process: the shared test server records no misses (gaps are off by default)."""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.core.ch import get_client  # noqa: E402
from server.erasure.domain.failure_reason import ABANDONED_REASON_PREFIX  # noqa: E402
from server.erasure.store import ledger, steps_ledgers  # noqa: E402
from server.knowledge.app import gaps as gaps_app  # noqa: E402
from server.knowledge.store import gaps as gaps_store  # noqa: E402
from server.knowledge.workers import knowledge_gaps as worker  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

pytestmark = pytest.mark.usefixtures("ch_target_required")

NS = "gaps"


@pytest.fixture(autouse=True)
def _capturing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Misses follow the capture level, `off` by default: these namespaces capture queries.
    from server.settings import settings

    monkeypatch.setattr(settings, "langfuse_capture_io_level", "query")


@pytest.fixture(autouse=True)
async def _ch_connected():
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


def _miss(org: str, user: str, normalized: str) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": NS,
        "asker": user,
        "normalized": normalized,
    }


async def _seed(org: str) -> None:
    rows = [_miss(org, u, "what is arr") for u in ("u1", "u2", "u3")]
    # Asked by two users only: under the threshold, never shown.
    rows += [_miss(org, u, "who owns billing") for u in ("u1", "u2")]
    await get_client().insert("loom.search_misses", rows)


@pytest.fixture(autouse=True)
def _gaps_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_gaps_enabled", True)
    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: True)


async def _list(org: str) -> list[dict[str, Any]]:
    from server.knowledge.api.models_gaps import KnowledgeGapsListInput

    out = await gaps_app.handle_knowledge_gaps_list(KnowledgeGapsListInput(org=org, namespace=NS))
    assert out.enabled and out.capturing
    return [g.model_dump() for g in out.gaps]


async def test_a_build_lists_only_what_enough_users_asked() -> None:
    org = unique_org("gaps")
    await _seed(org)
    assert await worker.build_namespace(org, NS) == 1

    gaps = await _list(org)
    assert [g["head"] for g in gaps] == ["what is arr"]
    assert (gaps[0]["askers"], gaps[0]["asks"]) == (3, 3)


async def test_a_user_erasure_removes_the_users_misses_and_gaps() -> None:
    org = unique_org("gapse")
    await _seed(org)
    assert await worker.build_namespace(org, NS) == 1

    # The saga's order: the gaps of the user's namespaces (the gap and its marker), then
    # the user's misses.
    assert await steps_ledgers.delete_knowledge_gaps_by_user(org, ["u1"]) == 2
    assert await steps_ledgers.delete_search_misses_by_user(org, ["u1"]) == 2
    assert await gaps_store.latest_gaps(org, NS, limit=10) == []

    # The next build regroups without u1: two users are under the threshold.
    assert await worker.build_namespace(org, NS) == 0
    assert await _list(org) == []


async def test_a_namespace_whose_misses_left_the_window_gets_an_empty_build() -> None:
    from datetime import timedelta

    from server.core.datetimes import naive_utc_now_seconds

    org = unique_org("gapsw")
    old = naive_utc_now_seconds() - timedelta(days=8)
    # Misses past the read window, and the build they made while they were in it.
    rows = [{**_miss(org, u, "what is arr"), "ts": old} for u in ("u1", "u2", "u3")]
    await get_client().insert("loom.search_misses", rows)
    built = naive_utc_now_seconds() - timedelta(days=3)  # inside the build's 7-day life
    base = {"org": org, "namespace": NS, "build": 1, "built_at": built, "last_at": old}
    gap = {**base, "gap_id": "g", "head": "what is arr", "samples": [], "askers": 3, "asks": 3}
    marker = {**base, "gap_id": "", "head": "", "samples": [], "askers": 0, "asks": 0}
    await gaps_store.insert_build([gap, marker])

    async def heads() -> list[str]:
        return [g["head"] for g in await gaps_store.latest_gaps(org, NS, limit=10) if g["gap_id"]]

    assert await heads() == ["what is arr"]

    # The live build still lists the namespace, and its pass writes an empty build.
    assert (org, NS) in await gaps_store.namespaces_to_build(gaps_store.MISS_DAYS, 10_000)
    assert await worker.build_namespace(org, NS) == 0
    assert await heads() == []
    # Its latest build holds no gap now, so later passes leave it alone.
    assert (org, NS) not in await gaps_store.namespaces_to_build(gaps_store.MISS_DAYS, 10_000)


async def test_a_user_erasure_matches_a_mixed_case_subject() -> None:
    from server.erasure.store import census

    org = unique_org("gapsc")
    # An end user's id keeps the token's case in `user_id`; the step compares lower-cased.
    # The asker is another spelling, so only the lower-cased `user_id` can match.
    row = {**_miss(org, "a.smith@x.io", "what is arr"), "user_id": "ASmith"}
    await get_client().insert("loom.search_misses", [row])
    before = await census.table_residue(org, "user_id", "ASmith", spellings=["asmith"])
    assert before["loom.search_misses"] == 1
    assert await steps_ledgers.delete_search_misses_by_user(org, ["asmith"]) == 1
    after = await census.table_residue(org, "user_id", "ASmith", spellings=["asmith"])
    assert after["loom.search_misses"] == 0


async def test_unfinished_reaching_skips_only_a_failed_abandoned_saga() -> None:
    org = unique_org("gapsa")

    def row(user: str, status: str, reason: str) -> dict[str, Any]:
        return {
            "id": uuid.uuid4(),
            "org": org,
            "scope_type": "user_id",
            "scope_value": user,
            "requester": "test",
            "status": status,
            "failure_reason": reason,
            "version": time.time_ns(),
        }

    rows = [
        row("abandoned", "failed", f"{ABANDONED_REASON_PREFIX} gave up"),
        row("retrying", "failed", "a step failed"),
        row("running", "in_progress", ""),
    ]
    await get_client().insert("loom.erasure_requests", rows)

    async def reaching(skip: bool) -> set[str]:
        found = await ledger.unfinished_reaching(org, NS, None, 0, 60, skip_abandoned=skip)
        return {str(r["scope_value"]) for r in found}

    try:
        assert await reaching(False) == {"abandoned", "retrying", "running"}
        assert await reaching(True) == {"retrying", "running"}
    finally:
        # The shared server's recovery worker would otherwise run these sagas.
        await get_client().execute(
            "DELETE FROM loom.erasure_requests WHERE org = {org:String}", {"org": org}
        )


async def test_a_recording_stores_every_column_and_an_open_erasure_stops_it() -> None:
    org = unique_org("gapsi")
    row = {
        "org": org,
        "namespace": NS,
        "user_id": "-",
        "asker": "alice",
        "normalized": "what is arr",
    }

    async def insert(normalized: str) -> None:
        # `_insert` gives back the in-flight slot `record_miss` takes.
        gaps_app.recordings.in_flight += 1
        await gaps_app._insert({**row, "normalized": normalized}, int(time.time()))

    async def stored() -> list[dict[str, Any]]:
        return await get_client().query_all(
            "SELECT * FROM loom.search_misses WHERE org = {org:String}", {"org": org}
        )

    await insert("what is arr")
    [got] = await stored()
    assert {k: got[k] for k in row} == row
    assert (got["agent"], got["session_id"]) == ("-", "-")
    assert len(got["miss_id"]) == 32 and got["ts"]

    erasure = {
        "id": uuid.uuid4(),
        "org": org,
        "scope_type": "user_id",
        "scope_value": "alice",
        "requester": "test",
        "status": "in_progress",
        "failure_reason": "",
        "version": time.time_ns(),
    }
    await get_client().insert("loom.erasure_requests", [erasure])
    try:
        await insert("who owns billing")
        assert [r["normalized"] for r in await stored()] == ["what is arr"]
    finally:
        # The shared server's recovery worker would otherwise run this saga.
        await get_client().execute(
            "DELETE FROM loom.erasure_requests WHERE org = {org:String}", {"org": org}
        )


async def test_a_dropped_build_is_gone_before_the_drop_returns() -> None:
    # An erasure's verify census may count the table right after the fence drops a build.
    org = unique_org("gapsd")
    await _seed(org)
    assert await worker.build_namespace(org, NS) == 1
    row = await get_client().query_one(
        "SELECT max(build) AS b FROM loom.knowledge_gaps WHERE org = {org:String}", {"org": org}
    )
    await gaps_store.delete_build(org, NS, int(row["b"]))
    left = await get_client().query_one(
        "SELECT count() AS n FROM loom.knowledge_gaps WHERE org = {org:String}", {"org": org}
    )
    # The delete's mutation has finished, not merely raced ahead of the count.
    pending = await get_client().query_one(
        "SELECT count() AS n FROM system.mutations WHERE database = 'loom'"
        " AND table = 'knowledge_gaps' AND NOT is_done AND position(command, {org:String}) > 0",
        {"org": org},
    )
    assert int(left["n"]) == 0 and int(pending["n"]) == 0
