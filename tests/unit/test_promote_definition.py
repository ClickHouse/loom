"""memory.promote of a definition: from its namespace-wide row to its own key, with the
hash an admin approves, refused for a sensitive value, a value with no term, a namespace
at its ceiling or a name another shared definition holds; promoted by the token's actor."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.core.errors import PreconditionFailedError
from server.core.hashing import served_content_sha256
from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryPromoteInput
from server.memory.app import promote
from server.memory.domain.definitions import definition_names, fold_name
from server.memory.store import MemoryRow, shared
from server.settings import settings

KEY = "definition/arr"
VALUE = {"term": "ARR", "aliases": ["annual recurring revenue"], "body": "Yearly revenue."}


def _row(**over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "o",
        "namespace": "n",
        "agent": "-",
        "user_id": "-",
        "session_id": "-",
        "memory_key": KEY,
        "memory_type": "definition",
        "value": VALUE,
        "confidence": 1.0,
        "version": 1,
        "pinned": False,
        "created_at": datetime(2026, 9, 12, tzinfo=UTC),
        "expires_at": None,
    }
    return MemoryRow(**{**base, **over})


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The source row, the live shared definitions (key -> value), what was written."""
    state: dict[str, Any] = {"row": _row(), "shared": {}, "written": []}

    async def get_by_key(*_a: Any, **_k: Any) -> MemoryRow:
        return state["row"]

    async def count(*_a: Any, **_k: Any) -> int:
        return state.get("count", len(state["shared"]))

    async def definitions(org: str, namespace: str, limit: int, **_k: Any) -> list[dict]:
        rows = [{"memory_key": k, "value": v} for k, v in sorted(state["shared"].items())]
        return rows[:limit]

    async def write(row: dict[str, Any]) -> None:
        state["written"].append(row)

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(promote, "get_embedder", lambda: None)
    monkeypatch.setattr(promote, "writer_actor", lambda fallback="": "user:admin")
    monkeypatch.setattr(shared, "count_shared_definitions", count)
    monkeypatch.setattr(shared, "shared_definitions", definitions)
    monkeypatch.setattr(shared, "insert_shared_row", write)
    return state


def _input(**over: Any) -> MemoryPromoteInput:
    base = {
        "org": "o",
        "namespace": "n",
        "agent": "-",
        "memory_key": KEY,
        "approved_sha256": served_content_sha256(VALUE),
    }
    return MemoryPromoteInput(**{**base, **over})


async def _refusal(store: dict[str, Any], **over: Any) -> str:
    with pytest.raises(HTTPException) as e:
        await promote.handle_memory_promote(_input(**over))
    assert store["written"] == []
    return str(e.value.detail["code"])


def test_names_fold_format_characters_space_and_case() -> None:
    assert fold_name("\ufeffSAT ") == fold_name("Sat") == fold_name("sat") == "sat"
    assert fold_name("Churn  Rate") == "churn rate"
    assert definition_names(json.dumps(VALUE)) == {"arr", "annual recurring revenue"}
    for value in (
        "plain text",
        {"body": "no term"},
        {"term": " "},
        {"term": "ARR", "aliases": "x"},
    ):
        assert definition_names(value) is None


async def test_an_approved_definition_is_promoted_by_the_tokens_actor(
    store: dict[str, Any],
) -> None:
    out = await promote.handle_memory_promote(
        _input(approver="someone", promoted_from_candidate_id="c1")
    )
    (row,) = store["written"]
    assert (row["memory_key"], row["promoted_by"]) == (KEY, "user:admin")
    assert row["promoted_from_candidate_id"] is None
    assert sorted(out.ignored_args) == ["approver", "promoted_from_candidate_id"]


async def test_a_definition_promotion_needs_its_approved_hash(store: dict[str, Any]) -> None:
    assert await _refusal(store, approved_sha256=None) == "VALIDATION_FAILED"
    with pytest.raises(PreconditionFailedError):
        await promote.handle_memory_promote(_input(approved_sha256="0" * 64))
    assert store["written"] == []


@pytest.mark.parametrize(
    "over",
    [
        {"agent": "bot"},  # a writer's own row
        {"session_id": "s1"},
        {"memory_key": "arr_note"},  # not under definition/
    ],
)
async def test_a_definition_comes_from_its_namespace_wide_row_only(
    store: dict[str, Any], over: dict[str, Any]
) -> None:
    store["row"] = _row(**over)
    assert await _refusal(store, memory_key=store["row"].memory_key) == (
        "DEFINITION_PROMOTION_REFUSED"
    )


async def test_a_definition_lands_on_its_own_key_and_a_definition_key_needs_one(
    store: dict[str, Any],
) -> None:
    assert await _refusal(store, target_key="definition/other") == "DEFINITION_PROMOTION_REFUSED"
    store["row"] = _row(memory_type="semantic", memory_key="arr_note")
    refused = await _refusal(store, memory_key="arr_note", target_key=KEY)
    assert refused == "DEFINITION_PROMOTION_REFUSED"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"sensitivity": "medium"}, "SENSITIVE_DEFINITION_REFUSED"),
        (
            {"value": {"term": "ARR", "body": "call 415-555-0134 for it"}},
            "SENSITIVE_DEFINITION_REFUSED",
        ),
        ({"value": {"body": "no term"}}, "VALIDATION_FAILED"),
    ],
)
async def test_a_sensitive_or_termless_value_is_not_promoted(
    store: dict[str, Any], over: dict[str, Any], code: str
) -> None:
    store["row"] = _row(**over)
    sha = served_content_sha256(store["row"].value)
    assert await _refusal(store, approved_sha256=sha) == code


async def test_the_value_is_graded_with_autodetect_off(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # An imported row carries no label; its value is read whatever the setting.
    monkeypatch.setattr(settings, "memory_sensitivity_autodetect", False)
    key = "AKIA" + "IOSFODNN7EXAMPLE"  # split so the secrets gate does not read it as one
    store["row"] = _row(value={"term": "ARR", "body": f"key {key}"})
    sha = served_content_sha256(store["row"].value)
    assert await _refusal(store, approved_sha256=sha) == "SENSITIVE_DEFINITION_REFUSED"


async def test_a_name_another_shared_definition_holds_is_refused(store: dict[str, Any]) -> None:
    store["shared"] = {"definition/revenue": {"term": "Annual Recurring Revenue"}}
    assert await _refusal(store) == "DEFINITION_NAME_TAKEN"
    # Its own shared row (a revision) holds the names already.
    store["shared"] = {KEY: VALUE}
    await promote.handle_memory_promote(_input())
    assert len(store["written"]) == 1


async def test_the_ceiling_refuses_a_new_key_but_not_a_revision(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_term_leg_max_definitions", 1)
    store["shared"] = {"definition/mrr": {"term": "MRR"}}
    assert await _refusal(store) == "DEFINITION_CEILING"
    store["shared"] = {KEY: VALUE}
    await promote.handle_memory_promote(_input())
    assert [r["memory_key"] for r in store["written"]] == [KEY]
    # Past the ceiling, even a revision waits for a demotion.
    store["written"].clear()
    store["shared"] = {KEY: VALUE, "definition/mrr": {"term": "MRR"}}
    assert await _refusal(store) == "DEFINITION_CEILING"


async def test_a_definition_skips_the_close_shared_row_check(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import conflicts

    class _Embedder:
        model_name, dim = "m", 2

        async def embed(self, text: str) -> list[float]:
            return [0.6, 0.8]

    async def check_against_shared(**kw: Any) -> list[dict[str, Any]]:
        raise AssertionError("a definition's names decide, not a close shared row")

    async def no_embedding(row: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(promote, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(conflicts, "check_against_shared", check_against_shared)
    monkeypatch.setattr(shared, "insert_shared_embedding", no_embedding)
    await promote.handle_memory_promote(_input())
    assert len(store["written"]) == 1


async def test_another_row_keeps_the_old_promotion_rules(store: dict[str, Any]) -> None:
    store["row"] = _row(memory_type="semantic", memory_key="arr_note", agent="bot")
    out = await promote.handle_memory_promote(
        _input(memory_key="arr_note", agent="bot", approved_sha256=None, approver="lead")
    )
    (row,) = store["written"]
    assert row["promoted_by"] == "lead" and out.ignored_args == []


@pytest.mark.parametrize("source_kind", ["memory_supporters", "conversation_seeder"])
async def test_review_promotes_no_definition_candidate(
    monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings
    from server.memory.store import learnings as learnings_repo

    async def find(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "candidate_id": "c1",
            "memory_key": KEY,
            "memory_type": "definition",
            "proposed_value": json.dumps(VALUE),
            "supporters": [f"o/n/-/-/-/{KEY}"],
            "source_kind": source_kind,
            "status": "pending",
        }

    async def must_not(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a refused review writes nothing")

    monkeypatch.setattr(learnings_repo, "find_candidate", find)
    monkeypatch.setattr(learnings_repo, "insert_row", must_not)
    monkeypatch.setattr(learnings, "_do_promote", must_not)
    review = LearningsReviewInput(org="o", namespace="n", candidate_id="c1", decision="promote")
    with pytest.raises(HTTPException) as e:
        await learnings.handle_learnings_review(review)
    assert e.value.detail["code"] == "VALIDATION_FAILED"


async def test_a_promotion_landed_after_the_count_is_caught_by_the_read(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_term_leg_max_definitions", 2)
    store["shared"] = {"definition/mrr": {"term": "MRR"}, "definition/nrr": {"term": "NRR"}}
    store["count"] = 1  # the count ran before the second one landed
    assert await _refusal(store) == "DEFINITION_CEILING"


async def test_another_rows_promotion_names_an_approved_hash_it_ignores(
    store: dict[str, Any],
) -> None:
    store["row"] = _row(memory_type="semantic", memory_key="arr_note", agent="bot")
    out = await promote.handle_memory_promote(_input(memory_key="arr_note", agent="bot"))
    assert out.ignored_args == ["approved_sha256"]


async def test_lineage_matches_no_candidate_by_time_for_a_definition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app import dedup

    async def fetch(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "value": json.dumps(VALUE),
            "memory_type": "definition",
            "promoted_from": [f"o/n/-/-/-/{KEY}"],
            "promotion_event": "",
            "promoted_from_candidate_id": None,
            "promoted_by": "user:admin",
            "rationale": "",
            "promoted_at": datetime(2026, 9, 12),
            "reversible_until": datetime(2026, 9, 19),
            "tombstoned_at": None,
        }

    async def by_time(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AssertionError("a definition records no candidate to match by time")

    monkeypatch.setattr(shared, "fetch_shared", fetch)
    monkeypatch.setattr(shared, "find_candidate_for_promotion", by_time)
    out = await dedup.handle_memory_lineage(
        MemoryLineageInput(org="o", namespace="n", agent="-", memory_key=KEY)
    )
    assert out.candidate is None
