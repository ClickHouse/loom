"""memory.search's term leg: which governed definitions a query names, in what order,
where they go in the served list, and what the leg reports when it adds nothing."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, cast

import pytest

from server.memory.api.models_search import MemorySearchInput, _MemorySearchHit
from server.memory.app.search import term_leg
from server.memory.app.search.context import SearchContext
from server.memory.domain.definition_check import SharedDefinition
from server.memory.domain.promotion_source import promotion_source
from server.settings import settings

NOW = datetime(2026, 10, 1)


def _row(key: str, term: str, aliases: list[str] | None = None, age_days: int = 1) -> dict:
    return {
        "memory_key": key,
        "value": json.dumps({"term": term, "aliases": aliases or [], "body": f"{term} is ..."}),
        "promoted_from": [promotion_source("o", "n", "-", "-", "-", key)],
        "promoted_at": NOW - timedelta(days=age_days),
        "promotion_event": f"e-{key}",
        "total": 0,
    }


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"rows": [], "lapsed": {}, "checked": [], "values_read": []}

    def names(row: dict) -> dict:
        value = json.loads(row["value"])
        return {**row, "term": value["term"], "aliases": value["aliases"]}

    async def shared_definitions(
        org: str, namespace: str, limit: int, *, strong: bool
    ) -> list[dict]:
        assert strong is False
        return [names(r) for r in state.get("listed", state["rows"])[:limit]]

    async def definition_values(org: str, namespace: str, keys: list[str]) -> dict[str, str]:
        state["values_read"].append(sorted(keys))
        return {r["memory_key"]: r["value"] for r in state["rows"] if r["memory_key"] in keys}

    async def source_reasons(
        org: str, namespace: str, defs: list[Any], *, strong: bool, max_value_chars: int
    ) -> dict[str, Any]:
        assert strong is False  # the search path reads without sequential consistency
        state["checked"].append(sorted(d.memory_key for d in defs))
        return {d.memory_key: state["lapsed"].get(d.memory_key) for d in defs}

    monkeypatch.setattr(term_leg.shared_repo, "shared_definitions", shared_definitions)
    monkeypatch.setattr(term_leg.shared_repo, "definition_values", definition_values)
    monkeypatch.setattr(term_leg, "source_reasons", source_reasons)
    return state


async def _keys(query: str, excluded: frozenset[str] = frozenset()) -> tuple[str, list[str]]:
    leg = await term_leg._read("o", "n", query, set(excluded), None, "m")
    return leg.status, [h.memory_key for h in leg.hits]


async def test_a_query_naming_no_term_reads_no_source(store: dict[str, Any]) -> None:
    store["rows"] = [_row("definition/arr", "ARR")]
    assert await _keys("what did we ship") == ("complete", [])
    assert store["checked"] == [] and store["values_read"] == []


async def test_an_empty_namespace_reports_empty(store: dict[str, Any]) -> None:
    assert await _keys("what is ARR") == ("empty", [])


async def test_names_are_served_in_query_order_the_longer_first(store: dict[str, Any]) -> None:
    store["rows"] = [
        _row("definition/arr", "ARR", ["annual recurring revenue"]),
        _row("definition/arr-growth", "ARR growth"),
        _row("definition/churn", "Churn rate"),
    ]
    status, keys = await _keys("churn rate against ARR growth")
    assert status == "complete"
    assert keys == ["definition/churn", "definition/arr-growth", "definition/arr"]
    # only the named definitions' sources are read
    assert store["checked"] == [["definition/arr", "definition/arr-growth", "definition/churn"]]


async def test_a_lapsed_or_excluded_definition_is_not_served(store: dict[str, Any]) -> None:
    store["rows"] = [_row("definition/arr", "ARR"), _row("definition/mrr", "MRR")]
    store["lapsed"] = {"definition/mrr": "changed"}
    assert await _keys("ARR and MRR") == ("complete", ["definition/arr"])
    assert await _keys("ARR and MRR", frozenset({"definition/arr"})) == ("complete", [])


async def test_a_name_two_hold_goes_to_the_latest_promotion(store: dict[str, Any]) -> None:
    store["rows"] = [
        _row("definition/new", "ARR", age_days=1),
        _row("definition/old", "Annual revenue", ["ARR"], age_days=5),
    ]
    assert await _keys("what is ARR") == ("complete", ["definition/new"])
    # the older one keeps the names it holds alone
    assert await _keys("annual revenue") == ("complete", ["definition/old"])


async def test_a_copy_not_promoted_from_its_namespace_wide_row_is_not_served(
    store: dict[str, Any],
) -> None:
    row = _row("definition/arr", "ARR")
    row["promoted_from"] = [promotion_source("o", "n", "bot", "-", "-", "definition/arr")]
    store["rows"] = [row]
    assert await _keys("what is ARR") == ("complete", [])


async def test_past_the_ceiling_the_leg_reports_capped(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_shared_definitions_max", 1)
    store["rows"] = [_row("definition/arr", "ARR"), _row("definition/mrr", "MRR", age_days=3)]
    assert await _keys("ARR and MRR") == ("capped", ["definition/arr"])


def _input(**over: Any) -> MemorySearchInput:
    return MemorySearchInput(**{"org": "o", "namespace": "n", "query": "what is ARR", **over})


@pytest.mark.parametrize(
    "over",
    [
        {"memory_type": "decision"},
        {"tags": ["x"]},
        {"category": "c"},
        {"as_of": NOW},
        {"min_body_chars": 10},
        {"recent_first": 2},
        {"anchor_first": 1},
        {"legs": ["sessions"]},
        {"page": "p1"},
        {"query": "what is \ud800"},
    ],
)
def test_a_field_asking_for_other_rows_turns_the_leg_off(over: dict[str, Any]) -> None:
    assert term_leg.leg_on(_input(**over)) is False


@pytest.mark.parametrize(
    "over",
    [
        {},
        {"tags": ["x"], "tags_soft": True},
        {"category": "c", "category_soft": True},
        {"agent": "bot", "user_id": "u", "session_id": "s", "scope": "shared"},
        {"exclude_keys": ["k"], "min_similarity": 0.4, "legs": ["memory", "sessions"]},
    ],
)
def test_a_field_saying_whose_rows_to_read_keeps_it_on(over: dict[str, Any]) -> None:
    assert term_leg.leg_on(_input(**over)) is True


def test_a_merged_pass_runs_no_leg() -> None:
    merged = _input()
    merged._merged_into_parent = True
    assert term_leg.leg_on(merged) is False


async def test_a_leg_past_its_bound_or_failing_adds_nothing() -> None:
    async def slow() -> term_leg.TermLeg:
        await asyncio.sleep(1)
        return term_leg.TermLeg("complete")

    async def broken() -> term_leg.TermLeg:
        raise RuntimeError("down")

    def leg(coro: Any) -> asyncio.Task[term_leg.TermLeg]:
        return asyncio.create_task(coro)

    assert (await term_leg.outcome(leg(slow()), 0.01)).status == "timed_out"
    assert (await term_leg.outcome(leg(broken()), 1)).status == "failed"
    assert (await term_leg.outcome(None, 1)).status == "off"


def _hit(key: str, rank: float, *, source: str = "fact", parent: str = "") -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=key,
        distance=0.5,
        similarity=0.5,
        rank_score=rank,
        source=source,
        parent_memory_key=parent,
    )


def _governed(key: str) -> _MemorySearchHit:
    row = _row(key, key)
    d = SharedDefinition(
        memory_key=key,
        value=json.loads(row["value"]),
        promoted_from=tuple(row["promoted_from"]),
        promoted_at=row["promoted_at"],
        promotion_event=row["promotion_event"],
    )
    return term_leg._hit(d, "n")


def test_governed_hits_lead_after_a_computed_row_and_push_the_rest_to_the_tail() -> None:
    derived = _hit("derived_aggregate", 0.9 + 1e-6, source="computed")  # ceiling + eps
    a, b, c = _hit("a", 0.9), _hit("b", 0.8), _hit("c", 0.7)
    tail = [_hit("d", 0.6)]
    governed = [_governed("definition/x"), _governed("definition/y")]
    served, rest = term_leg.merge([derived, a, b, c], governed, tail, "n", top_k=4)
    assert [h.memory_key for h in served] == [
        "derived_aggregate",
        "definition/x",
        "definition/y",
        "a",
    ]
    assert [h.memory_key for h in rest] == ["b", "c", "d"]
    assert derived.rank_score > governed[0].rank_score > governed[1].rank_score > a.rank_score


def test_another_row_under_a_governed_key_is_left_out() -> None:
    private = _hit("definition/x", 0.9)
    chunk = _hit("chunk:s:1", 0.8, source="chunk", parent="definition/x")
    other = _hit("a", 0.7)
    served, rest = term_leg.merge(
        [private, chunk, other], [_governed("definition/x")], [_hit("definition/x", 0.1)], "n", 5
    )
    assert [h.memory_key for h in served] == ["definition/x", "a"]
    assert served[0].governed is True
    assert rest == []


def test_governed_hits_past_top_k_are_cut_the_later_first_and_lead_the_next_page() -> None:
    governed = [_governed("definition/x"), _governed("definition/y")]
    served, rest = term_leg.merge([_hit("a", 0.9)], governed, [], "n", top_k=1)
    assert [h.memory_key for h in served] == ["definition/x"]
    assert [h.memory_key for h in rest] == ["definition/y", "a"]


def test_a_governed_hit_is_marked_and_shaped_as_a_shared_row() -> None:
    hit = _governed("definition/x")
    wire = hit.model_dump()
    assert wire["governed"] is True and wire["memory_type"] == "definition"
    assert wire["similarity"] == 0.0 and wire["distance"] == 1.0
    assert "governed" not in _hit("a", 0.5).model_dump()


def test_loom_search_keeps_governed_hits_first_through_its_floors() -> None:
    from server.memory.app.loom_search.answer_first import answer_first

    governed = {"memory_key": "definition/x", "governed": True, "similarity": 0.0}
    lead = {"memory_key": "a", "similarity": 0.9, "namespace": "n", "source": "fact"}
    weak = {"memory_key": "b", "similarity": 0.05, "namespace": "n", "source": "fact"}
    payload = {"memory": {"results": [lead, governed, weak]}, "knowledge": {"results": []}}
    out = answer_first(payload, "n")
    assert [h["memory_key"] for h in out["memory"]["results"]] == ["definition/x", "a"]


def test_governed_hits_count_within_the_tracker_verdicts_memory_cap() -> None:
    from server.memory.app.loom_search.answer_first import _restore_governed
    from server.tracker.domain.defect_answer import VERDICT_MEMORY_MAX

    governed = [{"memory_key": f"definition/{i}", "governed": True} for i in range(2)]
    rest = [{"memory_key": f"r{i}"} for i in range(VERDICT_MEMORY_MAX)]
    payload = {
        "tracker": {"verdict": "duplicate"},
        "memory": {"results": rest},
        "withheld": {"reason": "tracker_verdict_leads"},
    }
    _restore_governed(payload, governed)
    keys = [h["memory_key"] for h in payload["memory"]["results"]]
    assert keys[:2] == ["definition/0", "definition/1"] and len(keys) == VERDICT_MEMORY_MAX
    assert payload["withheld"]["memory"] == 2


def test_a_merged_decision_never_goes_above_a_governed_hit() -> None:
    from server.memory.app.loom_search.repo_decisions import merge_memory_hits

    governed = _governed("definition/x")
    decision = _hit("d", 0.0)
    decision.similarity = 0.99
    hits, added = merge_memory_hits([governed, _hit("a", 0.5)], [decision], "n")
    assert [h.memory_key for h in hits][0] == "definition/x"
    assert added == [decision]


async def test_a_name_goes_to_its_latest_promotion_even_when_the_query_misses_that_one(
    store: dict[str, Any],
) -> None:
    """`CHURN` (newer) holds the name `churn` that `Churn` (older) also claims; the query
    matches only `Churn`, case-folded, and still serves neither copy of the name."""
    store["rows"] = [
        _row("definition/new", "CHURN", age_days=1),
        _row("definition/old", "Churn", age_days=5),
    ]
    assert await _keys("what is churn") == ("complete", [])
    assert store["checked"] == [["definition/new", "definition/old"]]
    # the same when the winner is excluded
    assert await _keys("what is churn", frozenset({"definition/new"})) == ("complete", [])


class _Ctx:
    """The fields of a SearchContext that `term_leg.join` reads and writes."""

    def __init__(self) -> None:
        self.input = _input(top_k=3)
        self.search_budget: Any = None
        self.page_tail: list[Any] = []
        self.candidates_total = 4
        self.actual_work: dict[str, Any] = {}
        self.term_leg_status = "off"


def _context() -> SearchContext:
    return cast(SearchContext, _Ctx())


async def test_join_places_the_hits_and_clears_the_abstain() -> None:
    governed = _governed("definition/x")

    async def found() -> term_leg.TermLeg:
        return term_leg.TermLeg("complete", [governed])

    ctx = _context()
    task = asyncio.create_task(found())
    hits, verdict = await term_leg.join(ctx, task, [_hit("a", 0.5)], (True, "no_match", "x"))
    assert [h.memory_key for h in hits] == ["definition/x", "a"]
    assert verdict == (False, "", "") and ctx.term_leg_status == "complete"
    assert ctx.candidates_total == 5 and ctx.actual_work["term_leg_served"] == 1


async def test_the_leg_scores_its_hits_by_their_shared_embedding(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store["rows"] = [_row("definition/arr", "ARR"), _row("definition/mrr", "MRR")]

    async def vectors(
        org: str, ns: str, idents: list[Any], *, model: str, sequential: bool
    ) -> dict[Any, Any]:
        assert model == "m" and sequential is False
        return {i: [0.6, 0.8] for i in idents if i[3] == "definition/arr"}

    monkeypatch.setattr(term_leg, "live_vectors", vectors)
    leg = await term_leg._read("o", "n", "ARR and MRR", set(), [1.0, 0.0], "m")
    arr, mrr = leg.hits
    assert arr.similarity == pytest.approx(0.6) and arr.distance == pytest.approx(0.4)
    assert (mrr.similarity, mrr.distance) == (0.0, 1.0)  # no shared embedding


async def test_names_are_matched_from_the_value_that_is_served(store: dict[str, Any]) -> None:
    """A re-promotion between the two reads dropped the alias the query names."""
    store["rows"] = [_row("definition/arr", "ARR", ["annual recurring revenue"])]
    listed = dict(store["rows"][0])
    store["rows"][0]["value"] = json.dumps({"term": "ARR", "aliases": [], "body": "..."})
    store["listed"] = [listed]
    assert await _keys("annual recurring revenue") == ("complete", [])


async def test_join_with_no_hit_keeps_the_verdict() -> None:
    async def none() -> term_leg.TermLeg:
        return term_leg.TermLeg("empty")

    ctx = _context()
    task = asyncio.create_task(none())
    hits, verdict = await term_leg.join(ctx, task, [_hit("a", 0.5)], (True, "no_match", "x"))
    assert [h.memory_key for h in hits] == ["a"] and verdict == (True, "no_match", "x")
    assert ctx.term_leg_status == "empty"


def test_a_dated_reading_order_keeps_governed_hits_first() -> None:
    from server.memory.app.ask.rendering import evidence_order

    governed = _governed("definition/x")
    governed.created_at = governed.valid_at = "2019-01-01"  # promoted long ago
    old, new = _hit("old", 0.5), _hit("new", 0.5)
    old.valid_at, new.valid_at = "2020-01-01", "2026-01-01"
    computed = _hit("derived_aggregate", 1.0, source="computed")
    ordered, mode = evidence_order([old, governed, new, computed], None, "what is the latest ARR")
    assert mode == "reverse_chronological"
    assert [h.memory_key for h in ordered] == ["derived_aggregate", "definition/x", "new", "old"]
    ordinary, _ = evidence_order([computed, old, new], None, "what is the latest ARR")
    assert [h.memory_key for h in ordinary] == ["new", "old", "derived_aggregate"]


def test_restated_value_check_neither_retires_nor_is_retired_by_a_governed_hit() -> None:
    from server.memory.app.ask.supersession import resolve_templated_conflicts

    governed = _governed("definition/ceo")
    governed.content_excerpt = "The CEO of Acme is Ann Lee"
    newer = _hit("ceo-note", 0.5)
    newer.content_excerpt, newer.created_at = "The CEO of Acme is Bob Ray", "2026-01-01"
    kept, retired = resolve_templated_conflicts([governed, newer])
    assert kept == [governed, newer] and retired == []


def test_the_ask_pool_puts_governed_hits_first_whichever_search_served_them() -> None:
    from server.memory.domain.search_types import governed_after_computed

    governed = _governed("definition/x")
    governed.similarity = 0.0
    pool = [_hit(f"r{i}", 0.9) for i in range(30)]  # reranked past a 15-row window
    # a sub-search's governed hit, appended after the primary search's rows
    ordered = governed_after_computed([*pool, governed])
    assert ordered[0] is governed and len(ordered) == 31
    # a stage that dropped it decided so: it is not put back
    assert governed_after_computed(pool) == pool


async def test_join_waits_the_bound_and_a_re_entrant_pass_only_its_time_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.search.budget import SearchBudget

    async def slow() -> term_leg.TermLeg:
        await asyncio.sleep(0.05)
        return term_leg.TermLeg("complete")

    monkeypatch.setattr(settings, "memory_term_leg_wait_ms", 1000)
    first = _context()
    first.search_budget = SearchBudget(max_rounds=0, max_wall_ms=0)
    await term_leg.join(first, asyncio.create_task(slow()), [], (True, "", ""))
    assert first.term_leg_status == "complete"  # a first pass is not gated by the budget
    again = _context()
    again.input._pass_depth = 1
    again.search_budget = SearchBudget(max_rounds=0, max_wall_ms=0)
    await term_leg.join(again, asyncio.create_task(slow()), [], (True, "", ""))
    assert again.term_leg_status == "timed_out"


@pytest.mark.parametrize("bodies", ["unit", "source"])
async def test_bodies_leave_a_governed_hit_s_text_alone(
    bodies: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another row under the governed key would answer a read by key: none is made."""
    from server.memory import store as memory_store
    from server.memory.app.search import payload

    read: list[list[str]] = []

    async def get_many_by_keys(identity: Any, keys: list[str], **kw: Any) -> list[Any]:
        read.append(keys)
        return []

    class _Links:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[Any]:
            read.append(params["keys"])
            return []

    monkeypatch.setattr(memory_store, "get_many_by_keys", get_many_by_keys)
    monkeypatch.setattr(payload, "get_client", _Links)
    governed = _governed("definition/x")
    text = governed.content_excerpt
    hits = [governed, _hit("a", 0.5)]
    await payload._expand_unit_bodies(hits, _input(bodies=bodies))
    if bodies == "source":
        await payload._expand_source_bodies(hits, _input(bodies=bodies))
    assert read and all("definition/x" not in keys for keys in read)
    assert governed.content_excerpt == text


async def test_tags_and_usage_are_not_read_by_a_governed_hit_s_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.search import payload

    keys_read: list[list[str]] = []

    async def read_row_tags(org: str, nss: list[str], keys: list[str]) -> dict[Any, Any]:
        keys_read.append(keys)
        return {}

    monkeypatch.setattr(payload, "_read_row_tags", read_row_tags)
    governed = _governed("definition/x")
    await payload._fill_hit_tags([governed], _input(include_tags=True), ["n"])
    assert keys_read == [] and governed.tags == []
    clients: list[None] = []

    def client() -> Any:
        clients.append(None)
        raise RuntimeError("no ClickHouse in a unit test")

    monkeypatch.setattr(payload, "get_client", client)
    await payload._fill_hit_usage([governed], _input(include_usage=True), ["n"])
    assert clients == []  # a governed hit's counters are not read by its key
    assert governed.used_count == 0 and governed.harm_count == 0


async def test_a_definition_in_another_script_is_served_as_written(store: dict[str, Any]) -> None:
    store["rows"] = [_row("definition/sales", "売上")]
    # stored as memory.promote stores it: every non-ASCII character escaped
    store["rows"][0]["value"] = json.dumps({"term": "売上", "aliases": [], "body": "月次の売上"})
    leg = await term_leg._read("o", "n", "今月の売上は", set(), None, "m")
    assert [h.memory_key for h in leg.hits] == ["definition/sales"]
    assert "月次の売上" in leg.hits[0].content_excerpt


def test_a_governed_hit_a_computed_row_pushes_out_leads_the_tail() -> None:
    derived = _hit("derived_aggregate", 9.0, source="computed")
    governed = [_governed("definition/x")]
    served, rest = term_leg.merge([derived, _hit("a", 0.5)], governed, [], "n", top_k=1)
    assert [h.memory_key for h in served] == ["derived_aggregate"]
    assert [h.memory_key for h in rest] == ["definition/x", "a"]


async def test_a_parked_governed_hit_stays_governed_and_reads_no_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A governed hit cut to the next page keeps its flag, and its tags are still not read
    by its key there."""
    from server.memory.app import search_pages
    from server.memory.app.search import payload

    keys_read: list[list[str]] = []

    async def read_row_tags(org: str, nss: list[str], keys: list[str]) -> dict[Any, Any]:
        keys_read.append(keys)
        return {}

    monkeypatch.setattr(payload, "_read_row_tags", read_row_tags)
    _, tail = term_leg.merge(
        [_hit("a", 0.5)], [_governed("definition/x"), _governed("definition/y")], [], "n", 1
    )
    page = [search_pages.unparked(search_pages._parked(h)) for h in tail]
    assert [(h.memory_key, h.governed) for h in page if h] == [
        ("definition/y", True),
        ("a", None),
    ]
    await payload._fill_hit_tags([h for h in page if h], _input(include_tags=True), ["n"])
    assert keys_read == [["a"]]
    assert page[0] is not None and page[0].tags == []


def test_the_verdict_cap_holds_when_the_verdict_cut_nothing() -> None:
    from server.memory.app.loom_search.answer_first import _restore_governed
    from server.tracker.domain.defect_answer import VERDICT_MEMORY_MAX

    governed = [{"memory_key": f"definition/{i}", "governed": True} for i in range(2)]
    rest = [{"memory_key": f"r{i}"} for i in range(VERDICT_MEMORY_MAX - 1)]
    payload: dict[str, Any] = {"tracker": {"verdict": "new"}, "memory": {"results": rest}}
    _restore_governed(payload, governed)
    assert len(payload["memory"]["results"]) == VERDICT_MEMORY_MAX
    assert payload["withheld"] == {"memory": 1, "reason": "tracker_verdict_leads"}


async def test_whole_bodies_serve_a_long_definition_whole(store: dict[str, Any]) -> None:
    from server.memory.app.search import payload

    body = "Annual recurring revenue, counted per contract. " * 40  # past any excerpt
    store["rows"] = [_row("definition/arr", "ARR")]
    store["rows"][0]["value"] = json.dumps({"term": "ARR", "aliases": [], "body": body})
    leg = await term_leg._read("o", "n", "what is ARR", set(), None, "m", whole=True)
    (hit,) = leg.hits
    assert body in hit.content_excerpt
    returned, elided, tokens = await payload._expand_unit_bodies([hit], _input(bodies="unit"))
    assert (returned, elided) == (1, 0) and tokens > 0 and not hit.body_truncated


@pytest.mark.parametrize("bodies", ["unit", "source"])
async def test_a_governed_whole_body_must_fit_the_remaining_budget(bodies: str) -> None:
    from server.memory.app.search import payload

    first, second = _governed("definition/arr"), _governed("definition/mrr")
    first.content_excerpt = "x" * 1400
    second.content_excerpt = "y" * 1400
    returned, elided, tokens = await payload._expand_unit_bodies(
        [first, second], _input(bodies=bodies, body_budget_tokens=500)
    )
    assert (returned, elided, tokens) == (1, 1, 350)
    assert first.content_excerpt == "x" * 1400 and not first.body_truncated
    assert len(second.content_excerpt) <= 1200 and second.body_truncated
    assert "memory.search" in second.next and "memory.get" not in second.next


def test_loom_search_and_ask_keep_a_computed_row_above_governed_hits() -> None:
    from server.memory.app.loom_search.answer_first import answer_first
    from server.memory.domain.search_types import governed_after_computed

    count = {"memory_key": "derived_aggregate", "source": "computed", "similarity": 0.95}
    governed = {"memory_key": "definition/arr", "governed": True, "similarity": 0.0}
    fact = {"memory_key": "a", "similarity": 0.9, "namespace": "n", "source": "fact"}
    out = answer_first({"memory": {"results": [count, governed, fact]}}, "n")
    assert [h["memory_key"] for h in out["memory"]["results"]] == [
        "derived_aggregate",
        "definition/arr",
        "a",
    ]
    hit = _governed("definition/arr")
    derived = _hit("derived_aggregate", 1.0, source="computed")
    ordered = governed_after_computed([_hit("a", 0.9), derived, hit])
    assert [h.memory_key for h in ordered] == ["derived_aggregate", "definition/arr", "a"]
    ordinary = [_hit("a", 0.9), derived]
    assert governed_after_computed(ordinary) == ordinary


@pytest.mark.parametrize("computed_key", ["derived_aggregate", "temporal_diff"])
@pytest.mark.parametrize("with_definition", [False, True])
async def test_ask_knee_preserves_computed_precedence_before_token_trimming(
    monkeypatch: pytest.MonkeyPatch, computed_key: str, with_definition: bool
) -> None:
    from types import SimpleNamespace

    from server.memory.app import code_grounding
    from server.memory.app.ask import pool

    async def unchanged(query: str, search_input: Any, hits: list[Any], config: Any) -> list[Any]:
        return hits

    monkeypatch.setattr(code_grounding, "apply_code_grounding", unchanged)
    monkeypatch.setattr(pool, "_drop_superseded", lambda _: False)
    computed = _hit(computed_key, 1.0, source="computed")
    rows = [computed]
    if with_definition:
        rows.append(_governed("definition/arr"))
    rows.extend([_hit("on-topic", 0.9), _hit("weak", 0.1), _hit("weak2", 0.1)])
    for hit in rows:
        hit.similarity = hit.rank_score
    run = pool._AskRun(
        search_input=_input(),
        question="ARR",
        query_text="ARR",
        settings=SimpleNamespace(memory_ask_relevance_gate_min_keep=2),
        target_k=5,
        window=5,
        recency_weight=0.0,
        ranked=list(rows),
        synth_pool=list(rows),
    )
    await pool._ground_and_cut(run, window_rel_floor=0.8)
    assert run.gate_dropped == 2
    assert run.synth_pool == rows[:-2]
    await pool._settle_window(run, token_budget=1)
    assert run.memories == [computed]
    assert run.context_trimmed == len(rows) - 3


async def test_launch_serves_whole_bodies_when_the_search_asks_for_them(
    store: dict[str, Any],
) -> None:
    body = "Annual recurring revenue, counted per contract. " * 40
    store["rows"] = [_row("definition/arr", "ARR")]
    store["rows"][0]["value"] = json.dumps({"term": "ARR", "aliases": [], "body": body})
    for bodies, whole in (("unit", True), ("source", True), ("full", False)):
        task = term_leg.launch(_input(bodies=bodies), None, "m")
        assert task is not None
        (hit,) = (await term_leg.outcome(task, 5)).hits
        assert (body in hit.content_excerpt) is whole


@pytest.mark.parametrize("lapse", ["", "changed", "superseded", "expired", "demoted", "renamed"])
async def test_a_parked_definition_must_still_be_governed_for_its_query(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch, lapse: str
) -> None:
    from server.memory.app import search_pages
    from server.memory.app.search import output

    store["rows"] = [_row("definition/arr", "ARR")]
    leg = await term_leg._read("o", "n", "what is ARR", set(), None, "m")
    page = json.loads(
        search_pages.encode_page(
            query="what is ARR",
            top_k=1,
            candidates_total=1,
            tail=leg.hits,
            namespaces_searched=["n"],
            namespaces_skipped=[],
        )
    )
    if lapse == "changed":
        store["rows"][0]["value"] = json.dumps({"term": "ARR", "aliases": [], "body": "Edited"})
    elif lapse == "demoted":
        store["rows"] = []
    elif lapse == "renamed":
        store["rows"] = [_row("definition/arr", "MRR")]
    elif lapse:
        store["lapsed"]["definition/arr"] = lapse

    async def clear(*args: Any) -> list[bool]:
        return [False] * len(args[-1])

    monkeypatch.setattr(output, "_stale_flags", clear)
    monkeypatch.setattr(output, "withheld_definition_flags", clear)
    hits, next_offset, dropped = await output._parked_slice(_input(top_k=1), page, 0, {"n"})
    assert [h.memory_key for h in hits] == ([] if lapse else ["definition/arr"])
    assert next_offset == -1 and dropped == 0


async def test_a_failed_or_timed_out_page_check_withholds_governed_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed(*args: Any, **kwargs: Any) -> term_leg.TermLeg:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(term_leg, "_read", failed)
    assert await term_leg.refresh_parked(
        "o", "n", "ARR", [_governed("definition/arr")], whole=False
    ) == [None]
    monkeypatch.setattr(settings, "memory_term_leg_wait_ms", 0)
    assert await term_leg.refresh_parked(
        "o", "n", "ARR", [_governed("definition/arr")], whole=False
    ) == [None]


@pytest.mark.parametrize("parked_whole", [False, True])
@pytest.mark.parametrize("bodies", ["unit", "source", "full", "none"])
async def test_governed_pages_apply_the_requested_bodies_and_budget(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch, parked_whole: bool, bodies: str
) -> None:
    from server.memory.app import search_pages
    from server.memory.app.search import output

    store["rows"] = [_row("definition/arr", "ARR")]
    store["rows"][0]["value"] = json.dumps({"term": "ARR", "aliases": [], "body": "x" * 8000})
    leg = await term_leg._read("o", "n", "what is ARR", set(), None, "m", whole=parked_whole)
    page = json.loads(
        search_pages.encode_page(
            query="what is ARR",
            top_k=1,
            candidates_total=1,
            tail=leg.hits,
            namespaces_searched=["n"],
            namespaces_skipped=[],
        )
    )
    parked_text = page["hits"][0]["content_excerpt"]

    async def load(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return page

    async def clear(*args: Any) -> list[bool]:
        return [False] * len(args[-1])

    monkeypatch.setattr(search_pages, "load_page", load)
    monkeypatch.setattr(output, "_stale_flags", clear)
    monkeypatch.setattr(output, "withheld_definition_flags", clear)
    for budget in (200, 4000):
        out = await output._serve_parked_page(
            _input(
                top_k=1, page="page/" + "a" * 20 + "@0", bodies=bodies, body_budget_tokens=budget
            )
        )
        hit = out.results[0]
        if bodies in ("unit", "source"):
            assert hit.body_truncated is (budget == 200)
            assert out.bodies_elided == (1 if budget == 200 else 0)
            assert out.tokens_returned <= budget
            assert ("x" * 8000 in hit.content_excerpt) is (budget == 4000)
        elif bodies == "none":
            assert hit.content_excerpt == ""
        else:
            assert len(hit.content_excerpt) <= 1200
        assert page["hits"][0]["content_excerpt"] == parked_text


@pytest.mark.parametrize("oversized", ["aliases", "name", "query", "value"])
async def test_excessive_term_work_withholds_the_leg_without_changing_definitions(
    store: dict[str, Any], oversized: str
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.domain.definitions import definition_names

    value = {"term": "ARR", "aliases": [], "body": "annual recurring revenue"}
    query = "what is ARR"
    if oversized == "aliases":
        value["aliases"] = ["alias"] * 10000
    elif oversized == "name":
        value["aliases"] = ["x" * 100000]
    elif oversized == "query":
        query = "ARR " * 25000
    else:
        value["body"] = "x" * 1000000
    MemorySetInput(org="o", namespace="n", agent="-", memory_key="definition/arr", value=value)
    assert definition_names(value)
    row = _row("definition/arr", "ARR")
    row["value"] = json.dumps(value)
    store["rows"] = [row]
    before = row["value"]
    assert await _keys(query) == ("timed_out", [])
    assert row["value"] == before


@pytest.mark.parametrize("computed", [False, True])
async def test_actual_code_grounding_preserves_definition_priority_before_token_trimming(
    monkeypatch: pytest.MonkeyPatch, computed: bool
) -> None:
    from types import SimpleNamespace

    from server.code.app import reads
    from server.memory.app import code_grounding
    from server.memory.app.ask import pool

    async def target(*args: Any) -> tuple[str, str]:
        return "o", "code-test"

    async def facts(*args: Any) -> dict:
        return {117428: {"author": "writer", "title": "ARR calculation", "merged_at": NOW}}

    monkeypatch.setattr(code_grounding, "_authorized_code_target", target)
    monkeypatch.setattr(reads, "code_index", lambda: SimpleNamespace(prs_by_number=facts))
    monkeypatch.setattr(pool, "_drop_superseded", lambda _: False)
    governed = _governed("definition/arr")
    rows = [_hit("derived_aggregate", 1.0, source="computed"), governed] if computed else [governed]
    run = pool._AskRun(
        search_input=_input(),
        question="ARR in PR 117428",
        query_text="ARR in PR 117428",
        settings=SimpleNamespace(
            memory_ask_pr_temporal_gate=False,
            memory_ask_code_enrichment=False,
            memory_ask_pr_grounding=True,
            memory_ask_code_staleness=False,
        ),
        target_k=len(rows),
        window=len(rows),
        recency_weight=0.0,
        ranked=list(rows),
        synth_pool=list(rows),
    )
    await pool._ground_and_cut(run, None)
    assert any(h.source == "code_index" for h in run.synth_pool)
    await pool._settle_window(run, token_budget=1)
    assert run.memories == [rows[0]]


async def test_repromotion_cannot_bypass_the_name_work_budget(store: dict[str, Any]) -> None:
    store["listed"] = [_row("definition/arr", "ARR")]
    store["rows"] = [_row("definition/arr", "ARR", [""] * 10000)]
    assert await _keys("what is ARR") == ("timed_out", [])
    assert store["checked"] == []


@pytest.mark.parametrize("sizes", [(60000,), (12000, 12000, 12000, 12000)])
async def test_name_work_is_bounded_per_definition_before_normalization(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch, sizes: tuple[int, ...]
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.domain.definitions import definition_names

    row = _row("definition/arr", "ARR", ["\ufdfa" * size for size in sizes])
    value = json.loads(row["value"])
    MemorySetInput(org="o", namespace="n", agent="-", memory_key="definition/arr", value=value)
    assert definition_names(value)
    store["rows"] = [row]
    original = term_leg._candidate
    prepared = []

    def candidate(*args: Any) -> Any:
        prepared.append(args[0]["memory_key"])
        return original(*args)

    monkeypatch.setattr(term_leg, "_candidate", candidate)
    result = await _keys("what shipped")
    assert prepared == []
    assert result == ("timed_out", [])
    assert store["checked"] == [] and store["values_read"] == []
    assert json.loads(row["value"]) == value


async def test_changed_source_budget_exhaustion_withholds_the_entire_leg(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import definitions

    store["rows"] = [_row("definition/arr", "ARR")]

    async def oversized_source(*args: Any, **kwargs: Any) -> list[Any]:
        assert 0 < kwargs["max_value_chars"] < 1000000
        raise TimeoutError

    monkeypatch.setattr(memory_repo, "get_many_by_keys", oversized_source)
    monkeypatch.setattr(term_leg, "source_reasons", definitions.source_reasons)
    assert await _keys("what is ARR") == ("timed_out", [])


@pytest.mark.parametrize("ordinary_level", ["low", "medium"])
@pytest.mark.parametrize("governed_first", [False, True])
async def test_parked_oversized_source_is_not_hydrated_by_the_shared_label_check(
    store: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    ordinary_level: str,
    governed_first: bool,
) -> None:
    from server.memory.app import definitions, search_pages
    from server.memory.app.search import output
    from server.memory.store import reads

    store["rows"] = [_row("definition/arr", "ARR")]
    leg = await term_leg._read("o", "n", "what is ARR", set(), None, "m")
    ordinary = _hit("definition/ordinary", 0.5)
    ordinary.namespace = "n"
    ordinary._row_identity = ("_shared", "-", "-")
    tail = [leg.hits[0], ordinary] if governed_first else [ordinary, leg.hits[0]]
    page = json.loads(
        search_pages.encode_page(
            query="what is ARR",
            top_k=2,
            candidates_total=2,
            tail=tail,
            namespaces_searched=["n"],
            namespaces_skipped=[],
        )
    )
    hydrated: list[str] = []
    original = reads._hydrate

    def hydrate(row: dict[str, Any]) -> Any:
        hydrated.append(row["memory_key"])
        return original(row)

    async def heads(identity: Any, keys: list[str], **kwargs: Any) -> dict[str, Any]:
        return {
            key: {
                "memory_id": "00000000-0000-0000-0000-000000000001",
                "org": "o",
                "namespace": "n",
                "agent": "-",
                "user_id": "-",
                "session_id": "-",
                "memory_key": key,
                "memory_type": "definition",
                "value": json.dumps({"body": "x" * 500000 if key.endswith("/arr") else "x"}),
                "confidence": 1.0,
                "version": 1,
                "pinned": False,
                "created_at": NOW,
                "sensitivity": ordinary_level if key.endswith("/ordinary") else "low",
            }
            for key in keys
        }

    async def fresh(*args: Any) -> list[bool]:
        return [False] * len(args[-1])

    monkeypatch.setattr(reads.pg, "enabled", lambda: True)
    monkeypatch.setattr(reads.pg_memory, "heads_for_identity", heads)
    monkeypatch.setattr(reads, "_hydrate", hydrate)
    monkeypatch.setattr(term_leg, "source_reasons", definitions.source_reasons)
    monkeypatch.setattr(output, "_stale_flags", fresh)
    hits, next_offset, dropped = await output._parked_slice(_input(top_k=2), page, 0, {"n"})
    assert "definition/arr" not in hydrated
    assert "definition/ordinary" in hydrated
    assert [hit.memory_key for hit in hits] == (
        ["definition/ordinary"] if ordinary_level == "low" else []
    )
    assert next_offset == -1 and dropped == 0


@pytest.mark.parametrize(
    "query, expected",
    [
        ("what is Metric42X this quarter", ["definition/metric-42"]),
        ("how did revenue grow this quarter", []),
    ],
)
async def test_term_budget_serves_the_definition_ceiling_with_normal_aliases(
    store: dict[str, Any], query: str, expected: list[str]
) -> None:
    store["rows"] = [
        _row(f"definition/metric-{i}", f"Metric{i}X", [f"metric number {i}"]) for i in range(500)
    ]
    assert await _keys(query) == ("complete", expected)


@pytest.mark.parametrize("mentioned", [False, True], ids=["absent", "over-budget"])
async def test_an_expanding_alias_needs_a_matching_work_budget_before_preparation(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mentioned: bool
) -> None:
    alias = "\ufdfa" * (3000 if mentioned else 12000)
    store["rows"] = [_row("definition/arr", "ARR", [alias])]
    original = term_leg.Name.of
    prepared: list[str] = []

    def prepare(cls: type, name: str) -> Any:
        prepared.append(name)
        return original(name)

    monkeypatch.setattr(term_leg.Name, "of", classmethod(prepare))
    expected = ("timed_out", []) if mentioned else ("complete", ["definition/arr"])
    assert await _keys(alias if mentioned else "what is ARR") == expected
    # Normalization expands the alias eighteenfold; its pattern needs an admitted match.
    assert alias not in prepared


async def test_cancellation_stops_definition_scoring_between_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def vectors(*args: Any, **kwargs: Any) -> dict:
        return {ident: [1.0, 0.0] for ident in args[2]}

    scored = 0

    def cosine(left: list[float], right: list[float]) -> float:
        nonlocal scored
        scored += 1
        if scored == 1:
            task = asyncio.current_task()
            assert task is not None
            asyncio.get_running_loop().call_soon(task.cancel)
        return 1.0

    monkeypatch.setattr(term_leg, "live_vectors", vectors)
    monkeypatch.setattr(term_leg, "cosine", cosine)
    hits = [_governed(f"definition/{i}") for i in range(6)]
    task = asyncio.create_task(term_leg._score(hits, "o", "n", [1.0, 0.0], "m"))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert scored == 1
