"""The duplicate check's rerank stage.

Fixtures model duplicate-check benchmark misses: #120850 "Add custom logic to
validate QueryCache results" is a duplicate of #108713 "Detect when tables
behind the query have changed - to improve query cache"; the vector leg puts
#108713 second while plain fusion serves it after every lexical row.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.settings import settings
from server.tracker.api.models import TrackerDuplicateCheckOutput
from server.tracker.app import dup_rerank as dr
from server.tracker.app import dupcheck as td
from server.tracker.app import dupcheck_candidates as td_candidates

# ---- pure helpers -------------------------------------------------------------


def test_the_pool_takes_each_legs_own_head_in_turns_not_the_fused_list() -> None:
    heads = {"lexical": [101498, 3908, 118473, 5], "vector": [80252, 108713, 34011]}
    pool = dr.build_pool(heads, per_leg=3, extra=[[108713, 777]], exclude={3908})
    # vector #2 is in the pool at its own rank, not after every lexical row;
    # the extra lists (the fingerprint rows) take their turn beside the legs
    assert pool == [101498, 80252, 108713, 777, 118473, 34011]


def test_the_pool_is_capped() -> None:
    heads = {"lexical": list(range(1, 50)), "vector": list(range(100, 150))}
    assert len(dr.build_pool(heads, per_leg=40, cap=30)) == 30


def test_parse_keeps_asked_numbers_same_before_related_and_at_most_five() -> None:
    text = (
        '{"top": [{"n": 2, "l": "related", "c": 0.9}, {"n": 1, "l": "same", "c": 0.7},'
        ' {"n": 99, "l": "same", "c": 1}, {"n": 3, "l": "different", "c": 0.9},'
        ' {"n": 4, "l": "related", "c": 0.2}, {"n": 5, "l": "related", "c": 0.3},'
        ' {"n": 6, "l": "related", "c": 0.4}, {"n": 7, "l": "related", "c": 0.5}]}'
    )
    got = dr.parse_top(text, [1, 2, 3, 4, 5, 6, 7])
    assert [p[0] for p in got] == [1, 2, 7, 6, 5]
    assert got[0] == (1, "same", 0.7)


def test_parse_is_empty_on_garbage() -> None:
    assert dr.parse_top("not json", [1]) == []
    assert dr.parse_top('{"top": [{"n": "x"}]}', [1]) == []


class _LLM:
    def __init__(self, text: str = "", fail: bool = False) -> None:
        self.text, self.fail, self.calls = text, fail, 0

    async def complete(self, system: str, user: str, **kw: Any) -> Any:
        self.calls += 1
        if self.fail:
            raise RuntimeError("down at https://gw.internal:8443")
        return SimpleNamespace(text=self.text, model="m", tokens_in=100, tokens_out=20)


def test_rerank_fails_open() -> None:
    got = asyncio.run(
        dr.rerank(_LLM(fail=True), "t", "", [{"number": 1, "title": "a"}], body_chars=50)
    )
    assert not got.ran and got.picks == [] and got.error == "RuntimeError"


# ---- the handler stage ----------------------------------------------------------


class _Src:
    def __init__(self, rows: dict[int, dict[str, Any]]) -> None:
        self.rows = rows
        self.asked: list[int] = []

    async def embed_items_by_number(
        self, org: str, ns: str, *, kind: str, numbers: list[int], body_chars: int
    ) -> list[dict[str, Any]]:
        self.asked = list(numbers)
        return [
            {"number": n, "source_id": f"ClickHouse/ClickHouse#{n}", **self.rows[n]}
            for n in numbers
            if n in self.rows
        ]


def _input(title: str = "Add custom logic to validate QueryCache results", as_of: str = "") -> Any:
    from server.tracker.api.models import TrackerDuplicateCheckInput

    return TrackerDuplicateCheckInput(
        org="clickhouse", namespace="qa-clickhouse", title=title, as_of=as_of
    )


@pytest.fixture
def _on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_enabled", True)
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_per_leg", 12)


def test_the_rerank_pick_leads_nearest_and_the_verdict_stays(
    monkeypatch: pytest.MonkeyPatch, _on: None
) -> None:
    # the model sees positional keys: the pool is [101498, 108713], so key 2
    llm = _LLM('{"top": [[2, "s", 0.8]]}')
    monkeypatch.setattr("server.llm.client.get_llm_client_for_model", lambda m: llm)
    src = _Src(
        {
            101498: {"title": "QueryCache shows stale rows", "created_at": datetime(2025, 1, 1)},
            108713: {
                "title": "Detect when tables behind the query have changed - to improve query cache",
                "created_at": datetime(2025, 5, 1),
            },
        }
    )
    run: dict[str, Any] = {
        "q": SimpleNamespace(number=0, frames=[], versions=[], component=""),
        "leg_heads": {"lexical": [101498], "vector": [80252, 108713]},
        "meta": {},
    }
    out = TrackerDuplicateCheckOutput(verdict="none", nearest=[{"number": 101498}])
    pool = asyncio.run(td._rerank_pool(src, _input(), run, None))
    got = asyncio.run(td._rerank_over(_input(), [pool]))
    assert got is not None
    rr, picks = got
    assert [c["number"] for c in pool] == [101498, 108713]
    assert rr.picks == [(108713, "same", 0.8)], "the block names the issue, not the model's key"
    shown = td._apply_picks(out, picks, rr)
    assert shown.verdict == "none" and shown.items == [], "a pick never changes the verdict"
    assert shown.nearest[0]["number"] == 108713 and shown.nearest[0]["rerank"]["label"] == "same"
    assert shown.nearest[0]["url"] == "https://github.com/ClickHouse/ClickHouse/issues/108713"
    assert shown.nearest[1] == {"number": 101498}, "the fit's own nearest rows follow the picks"


def test_a_candidate_filed_after_the_wall_never_reaches_the_model(
    monkeypatch: pytest.MonkeyPatch, _on: None
) -> None:
    src = _Src(
        {
            1: {"title": "a", "created_at": datetime(2025, 1, 1)},
            2: {"title": "b", "created_at": datetime(2026, 2, 1)},
        }
    )
    run = {
        "q": SimpleNamespace(number=0),
        "leg_heads": {"vector": [1, 2]},
        "meta": {},
    }
    pool = asyncio.run(
        td._rerank_pool(src, _input(as_of="2026-01-01 00:00:00"), run, datetime(2026, 1, 1))
    )
    assert [c["number"] for c in pool] == [1], "#2 was filed after the replay wall"


def test_off_or_unavailable_skips_the_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_enabled", False)
    run = {"q": SimpleNamespace(number=0), "leg_heads": {"vector": [1]}, "meta": {}}
    assert asyncio.run(td._rerank_pool(_Src({1: {"title": "a"}}), _input(), run, None)) == []
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_enabled", True)
    empty = _input("").model_copy(update={"title": "", "body": ""})
    assert (
        asyncio.run(td._rerank_pool(_Src({1: {"title": "a"}}), empty, {**run, "item": None}, None))
        == []
    )


def test_a_timeout_keeps_the_answer(monkeypatch: pytest.MonkeyPatch, _on: None) -> None:
    class Slow(_LLM):
        async def complete(self, *a: Any, **kw: Any) -> Any:
            await asyncio.sleep(1)
            return SimpleNamespace(text="{}")

    monkeypatch.setattr("server.llm.client.get_llm_client_for_model", lambda m: Slow())
    monkeypatch.setattr(settings, "tracker_dupcheck_rerank_timeout_s", 0.05)
    rr = asyncio.run(td._rerank_call(_input(), [{"number": 1, "title": "a"}]))
    assert rr is not None and rr.error == "timeout" and rr.picks == []


def test_one_call_over_every_namespace_and_colliding_numbers_stay_apart(
    monkeypatch: pytest.MonkeyPatch, _on: None
) -> None:
    llm = _LLM('{"top": [[2, "s", 0.9]]}')
    monkeypatch.setattr("server.llm.client.get_llm_client_for_model", lambda m: llm)
    merged = TrackerDuplicateCheckOutput(
        verdict="none",
        namespaces=["qa-clickhouse", "qa-example-private"],
        nearest=[{"number": 7, "namespace": "qa-clickhouse"}],
    )
    pools = [
        [{"number": 100, "title": "public", "namespace": "qa-clickhouse"}],
        [{"number": 100, "title": "private", "namespace": "qa-example-private"}],
    ]
    got = asyncio.run(td._rerank_over(_input(), pools))
    assert got is not None and llm.calls == 1, "one call for the whole fan-out"
    out = td._apply_picks(merged, got[1], got[0])
    # key 2 is the private #100, not the public one of the same number
    assert out.nearest[0] == {
        "number": 100,
        "namespace": "qa-example-private",
        "title": "private",
        "rerank": {"label": "same", "confidence": 0.9},
    }
    assert out.nearest[1]["number"] == 7 and out.rerank["pool"] == 2


def test_a_pick_never_repeats_a_served_item() -> None:
    from server.tracker.app import dup_rerank as dr

    out = TrackerDuplicateCheckOutput(verdict="related", items=[{"number": 5}])
    rr = dr.Rerank(ran=True, picks=[(5, "same", 0.9), (6, "related", 0.5)])
    got = td._apply_picks(out, [{"number": 5, "rerank": {}}, {"number": 6, "rerank": {}}], rr)
    assert [r["number"] for r in got.nearest] == [6]


def test_the_check_hands_its_pool_over_before_it_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_checked` starts the call once every namespace handed a pool over; a
    check that never hands one over (it failed early) counts as empty."""
    seen: list[int] = []

    async def fake_check(inp: Any, wall: Any, pool_ready: Any = None) -> Any:
        if inp.namespace == "a":
            pool_ready.set_result(
                [{"number": 1, "title": "x", "namespace": "a", "draft_title": "t"}]
            )
            await asyncio.sleep(0.01)
        return TrackerDuplicateCheckOutput(verdict="none")

    async def fake_over(inp: Any, pools: list[Any]) -> Any:
        seen.append(len([p for p in pools if p]))
        return None

    monkeypatch.setattr(td, "_check_one", fake_check)
    monkeypatch.setattr(td, "_rerank_over", fake_over)
    ins = [_input().model_copy(update={"namespace": ns}) for ns in ("a", "b")]
    out = asyncio.run(td._checked(ins, None, 10))
    assert seen == [1] and out.verdict == "none"


def test_the_fanout_puts_picks_first_across_namespaces() -> None:
    from server.tracker.app.dupcheck_fanout import _merged_nearest

    a = TrackerDuplicateCheckOutput(verdict="none", nearest=[{"number": 1}, {"number": 2}])
    b = TrackerDuplicateCheckOutput(
        verdict="none", nearest=[{"number": 9, "rerank": {"label": "same", "confidence": 0.9}}]
    )
    rows = _merged_nearest([("qa-clickhouse", a), ("qa-example-private", b)], "none")
    assert [r["number"] for r in rows] == [9, 1, 2]


# ---- promotion, error names, fingerprints ----------------------------------------


def _pick(n: int, label: str, conf: float, anchors: list[str] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "number": n,
        "title": "t",
        "rerank": {"label": label, "confidence": conf},
    }
    if anchors:
        row["anchors"] = anchors
    return row


def test_a_confident_anchored_same_pick_leads_nearest_and_never_decides() -> None:
    """The draft quotes SIZES_OF_COLUMNS_DOESNT_MATCH without `Code: 9`;
    #109877 (code 9, one of 9 such issues) is the model's `same` at 0.95
    while the fit says none: it leads `nearest`, the verdict stays the fit's."""
    from server.tracker.app import dup_rerank as dr

    out = TrackerDuplicateCheckOutput(verdict="none")
    rr = dr.Rerank(ran=True, picks=[(109877, "same", 0.95)])
    got = td._apply_picks(out, [_pick(109877, "same", 0.95, ["same error code 9"])], rr)
    assert got.verdict == "none" and got.items == []
    assert got.nearest[0]["number"] == 109877 and "promoted" not in got.rerank


def test_a_fit_duplicate_is_never_overridden() -> None:
    from server.tracker.app import dup_rerank as dr

    out = TrackerDuplicateCheckOutput(
        verdict="duplicate_of", items=[{"number": 5, "confidence": 0.4}]
    )
    got = td._apply_picks(out, [_pick(9, "same", 0.99, ["lexical"])], dr.Rerank(ran=True))
    assert [it["number"] for it in got.items] == [5]


def test_error_names_and_stids_are_read_from_the_draft() -> None:
    from server.tracker.domain import similar as sim

    text = "fails with `Size of permutation (100)` (SIZES_OF_COLUMNS_DOESNT_MATCH); Segmentation fault (STID: 0883-49E8)"
    assert sim.error_names_of(text) == ["SIZES_OF_COLUMNS_DOESNT_MATCH"]
    assert sim.stid_of(text) == "0883-49e8"
    assert sim.stid_of("no fingerprint") == ""


def test_a_quoted_error_name_resolves_to_its_code() -> None:
    class Src:
        async def error_name_codes(self, org: str, ns: str) -> dict[str, int]:
            return {"SIZES_OF_COLUMNS_DOESNT_MATCH": 9}

    q = SimpleNamespace(error_code=0)
    meta: dict[str, Any] = {}
    inp = _input("Size of permutation doesn't match").model_copy(
        update={"body": "x (SIZES_OF_COLUMNS_DOESNT_MATCH)"}
    )
    asyncio.run(td_candidates._resolve_error_name(Src(), inp, q, meta))
    assert q.error_code == 9 and meta["error_code_from"] == "SIZES_OF_COLUMNS_DOESNT_MATCH"
    q2 = SimpleNamespace(error_code=43)  # a `Code: N` the draft named wins
    asyncio.run(td_candidates._resolve_error_name(Src(), inp, q2, {}))
    assert q2.error_code == 43


def test_the_fingerprint_rows_are_anchors_in_the_pool(
    monkeypatch: pytest.MonkeyPatch, _on: None
) -> None:
    src = _Src(
        {
            109877: {"title": "SIZES_OF_COLUMNS_DOESNT_MATCH after LEFT JOIN"},
            29748: {"title": "other"},
        }
    )
    run = {
        "q": SimpleNamespace(number=0, error_code=9),
        "leg_heads": {"lexical": [29748]},
        "fingerprint": [109877],
        "meta": {},
    }
    pool = asyncio.run(td._rerank_pool(src, _input(), run, None))
    by = {c["number"]: c["anchors"] for c in pool}
    assert by == {29748: ["lexical"], 109877: ["same error code 9"]}


def test_two_sure_picks_are_left_to_the_reader() -> None:
    """A private and a public record both `same` above 0.9."""
    from server.tracker.app import dup_rerank as dr

    picks = [
        _pick(4825, "same", 0.95, ["same error code 47"]),
        _pick(111438, "same", 0.92, ["same error code 47"]),
    ]
    got = td._apply_picks(TrackerDuplicateCheckOutput(verdict="none"), picks, dr.Rerank(ran=True))
    assert got.verdict == "none" and [r["number"] for r in got.nearest[:2]] == [4825, 111438]
