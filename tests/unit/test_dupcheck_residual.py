"""The duplicate check's rerank reads each leg 50 deep, and a
leg leader its picks crowded out rides after them.

Frozen from the duplicate-check benchmark on a real tracker record: #83407
"Join engine table UNSUPPORTED_JOIN_KEYS error when table has…" is a
duplicate of #74669, the vector leg's #1. A rerank that puts five picks first
and cuts at six would drop it; letting the leader displace the last pick
loses other true issues on held-out, so the leader is added after the picks
instead.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from server.settings import settings
from server.tracker.api.models import TrackerDuplicateCheckOutput
from server.tracker.app import dup_rerank as dr
from server.tracker.app import dupcheck as td
from server.tracker.app import dupcheck_candidates as td_candidates
from server.tracker.app.dupcheck_fanout import _merged_nearest
from server.tracker.domain import dupcheck_closest as dcl


def test_the_defaults_are_the_measured_knee() -> None:
    fields = type(settings).model_fields
    assert fields["tracker_dupcheck_rerank_per_leg"].default == 50
    assert fields["tracker_dupcheck_rerank_ns_cap"].default == 60, "100 and 150 measured worse"
    assert fields["tracker_dupcheck_rerank_pool_cap"].default == 200


def test_each_leg_keeps_as_many_rows_as_the_rerank_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_per_leg", 50)
    assert td_candidates._pool_head() == 50
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_per_leg", 10)
    assert td_candidates._pool_head() == td_candidates.POOL_HEAD, "never below the default head"


def test_the_call_cap_is_the_setting_or_the_module_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_pool_cap", 200)
    assert td_candidates.pool_cap() == 200
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_pool_cap", 0)
    assert td_candidates.pool_cap() == dr.POOL_CAP


class _Src:
    def repos(self) -> list[str]:
        return ["ClickHouse/ClickHouse"]

    async def edges_from(self, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        return []

    async def embed_items_by_number(
        self, org: str, ns: str, *, kind: str, numbers: list[int], body_chars: int
    ) -> list[dict[str, Any]]:
        return [
            {"number": n, "source_id": f"ClickHouse/ClickHouse#{n}", "title": f"t{n}"}
            for n in numbers
        ]


def test_a_namespace_pool_honours_its_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_enabled", True)
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_per_leg", 50)
    heads = {"lexical": list(range(1, 51)), "vector": list(range(101, 151))}
    run = {"q": SimpleNamespace(number=0), "leg_heads": heads, "meta": {}}
    from server.tracker.api.models import TrackerDuplicateCheckInput

    inp = TrackerDuplicateCheckInput(org="clickhouse", namespace="qa-clickhouse", title="a draft")
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_ns_cap", 60)
    pool = asyncio.run(td._rerank_pool(_Src(), inp, run, None))
    assert len(pool) == 60 and [c["number"] for c in pool[:4]] == [1, 101, 2, 102], (
        "legs take turns"
    )
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_ns_cap", 100)
    assert len(asyncio.run(td._rerank_pool(_Src(), inp, run, None))) == 100


def test_one_call_reads_at_most_the_call_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {}

    async def fake_call(inp: Any, cands: list[dict[str, Any]]) -> Any:
        seen["n"] = len(cands)
        return None

    monkeypatch.setattr(td, "_rerank_call", fake_call)
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_pool_cap", 90)
    from server.tracker.api.models import TrackerDuplicateCheckInput

    inp = TrackerDuplicateCheckInput(org="clickhouse", namespace="qa-clickhouse", title="a draft")
    pools = [[{"number": i} for i in range(60)], [{"number": 1000 + i} for i in range(60)]]
    asyncio.run(td._rerank_over(inp, pools))
    assert seen["n"] == 90


# ---- leaders survive the picks --------------------------------------------------


def _pick(n: int, conf: float = 0.5) -> dict[str, Any]:
    return {"number": n, "rerank": {"label": "related", "confidence": conf}}


def test_a_leader_the_cut_left_out_rides_after_the_picks() -> None:
    rows = [
        _pick(1, 0.9),
        _pick(2),
        _pick(3),
        _pick(4),
        _pick(5),
        {"number": 74669, "lead": "vector"},
    ]
    got = dcl.keep_leaders(rows, 5)
    assert [r["number"] for r in got] == [1, 2, 3, 4, 5, 74669], (
        "every pick stays; the leader is added"
    )
    assert dcl.keep_leaders(rows, 0) == []
    assert dcl.keep_leaders(rows[:3], 5) == rows[:3]


def test_at_most_two_leaders_are_added() -> None:
    rows = [_pick(1), *({"number": n, "lead": "x"} for n in (7, 8, 9))]
    assert [r["number"] for r in dcl.keep_leaders(rows, 1)] == [1, 7, 8]


def test_nearest_rows_mark_their_leaders() -> None:
    unserved = [
        {"number": 74669, "title": "Wrong result with Join engine", "confidence": 0.001},
        {"number": 5, "title": "UNSUPPORTED_JOIN_KEYS error on Join table", "confidence": 0.02},
    ]
    rows = dcl.nearest_rows(
        unserved,
        "Join engine table UNSUPPORTED_JOIN_KEYS error when table has ALIAS",
        legs_top={"vector": [74669], "lexical": [5]},
    )
    lead = {r["number"]: r.get("lead") for r in rows}
    assert lead[74669] == "vector" and lead[5] == "lexical"


def test_apply_picks_keeps_the_vector_leader_of_the_live_miss() -> None:
    """Five picks and the vector leader; six slots less the closest rows. The
    leader stays, the last pick gives way."""
    out = TrackerDuplicateCheckOutput(
        verdict="none",
        closest=[{"number": 900, "title": "same title"}],
        nearest=[{"number": 74669, "lead": "vector"}],
    )
    picks = [_pick(n) for n in (11, 12, 13, 14, 15)]
    rr = dr.Rerank(ran=True, picks=[(n, "related", 0.5) for n in (11, 12, 13, 14, 15)])
    shown = td._apply_picks(out, picks, rr)
    nums = [r["number"] for r in shown.nearest]
    assert nums == [11, 12, 13, 14, 15, 74669], "five slots of picks, the leader after them"


def test_the_fan_out_merge_keeps_leaders_too() -> None:
    a = TrackerDuplicateCheckOutput(verdict="none", nearest=[_pick(n) for n in (1, 2, 3, 4, 5, 6)])
    b = TrackerDuplicateCheckOutput(verdict="none", nearest=[{"number": 74669, "lead": "vector"}])
    rows = _merged_nearest([("qa-clickhouse", a), ("qa-example-private", b)], "none")
    assert len(rows) == dcl.NEAREST_TOTAL_MAX + 1 and rows[-1]["number"] == 74669


def test_the_residual_table_gives_every_miss_a_reason() -> None:
    rows = json.loads(
        Path(__file__).parents[1].joinpath("fixtures", "dupcheck_residual_misses.json").read_text()
    )
    assert rows and all(r["reason"] and r["reach"] for r in rows)
    assert {r["reach"] for r in rows} <= {
        "unreachable",
        "judgement",
        "ambiguous",
        "reachable in principle, net loss",
        "reachable only via links, net loss",
        "found this run (LLM variance: missed in the other run)",
    }
