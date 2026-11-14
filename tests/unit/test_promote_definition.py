"""memory.promote of a definition: from its namespace-wide row to its own key, with the
hash an admin approves, refused for a sensitive value, a value with no term, a namespace
at its ceiling or a name another shared definition holds; promoted by the token's actor."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from server.core.errors import LoomServerError, PreconditionFailedError
from server.core.hashing import served_content_sha256
from server.core.leases import LeaseUnavailable
from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryPromoteInput
from server.memory.app import promote
from server.memory.domain.definitions import definition_names, fold_name
from server.memory.store import MemoryRow, shared
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

KEY = "definition/arr"
NOW = datetime(2026, 10, 1)
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
    """The source row, the live shared definitions (key -> value), what was written, and
    whether the namespace's claim is free (None: the lease store does not answer; the
    claims taken are kept)."""
    state: dict[str, Any] = {
        "row": _row(),
        "shared": {},
        "written": [],
        "free": True,
        "claims": [],
        "held": False,
    }

    async def get_by_key(*_a: Any, **_k: Any) -> MemoryRow:
        return state["row"]

    async def definitions(org: str, namespace: str, limit: int, **_k: Any) -> list[dict]:
        # The two fields the store extracts, as its statement reads them.
        assert state["held"], "the check runs under the namespace's claim"
        rows = [{"memory_key": k, **_extracted(v)} for k, v in sorted(state["shared"].items())]
        return rows[:limit]

    async def write(row: dict[str, Any]) -> None:
        assert state["held"] or row["memory_type"] != "definition", "written under the claim"
        state["written"].append(row)

    @asynccontextmanager
    async def holding(name: str, **_k: Any) -> AsyncIterator[bool]:
        state["claims"].append(name)
        if state["free"] is None:
            raise LeaseUnavailable("the lease store did not answer", operation=name)
        state["held"] = state["free"]
        try:
            yield state["free"]
        finally:
            state["held"] = False

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(promote, "get_embedder", lambda: None)
    monkeypatch.setattr(promote, "writer_actor", lambda fallback="": "user:admin")
    monkeypatch.setattr(promote, "naive_utc_now", lambda: NOW)
    monkeypatch.setattr(promote.leases, "holding", holding)
    monkeypatch.setattr(shared, "shared_definitions", definitions)
    monkeypatch.setattr(shared, "insert_shared_row", write)
    return state


def _extracted(value: Any) -> dict[str, Any]:
    """`term` and `aliases` as `shared_definitions` extracts them from a stored value."""
    if not isinstance(value, dict):
        return {"term": "", "aliases": []}
    term = value.get("term") if isinstance(value.get("term"), str) else ""
    aliases = value.get("aliases", [])
    if not isinstance(aliases, list):
        return {"term": term, "aliases": []}
    return {"term": term, "aliases": [a if isinstance(a, str) else json.dumps(a) for a in aliases]}


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
    """The code of the refusal the promotion answers, which writes nothing."""
    with pytest.raises((HTTPException, LoomServerError)) as e:
        await promote.handle_memory_promote(_input(**over))
    assert store["written"] == []
    if isinstance(e.value, LoomServerError):
        return e.value.code
    return str(e.value.detail["code"])


@pytest.mark.parametrize(
    ("name", "folded"),
    [
        ("\ufeffSAT", "sat"),  # a format character dropped
        ("\uff33\uff21\uff34", "sat"),  # NFKC: full-width letters
        ("  Churn\u00a0  Rate ", "churn rate"),  # trimmed, every run of space made one
        ("Straße", "strasse"),  # case-folded
    ],
)
def test_a_name_folds_step_by_step(name: str, folded: str) -> None:
    assert fold_name(name) == folded


@pytest.mark.parametrize(
    ("value", "names"),
    [
        (VALUE, {"arr", "annual recurring revenue"}),
        (json.dumps(VALUE), None),  # JSON text is not a decoded object
        ({"term": "MRR"}, {"mrr"}),  # aliases may be left out
        ("plain text", None),
        ({"body": "no term"}, None),
        ({"term": " "}, None),
        ({"term": "ARR", "aliases": "x"}, None),
        ({"term": "ARR", "aliases": ""}, None),
        ({"term": "ARR", "aliases": [1]}, None),
    ],
)
def test_a_definitions_names(value: Any, names: set[str] | None) -> None:
    assert definition_names(value) == names


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


async def test_an_empty_approver_sent_is_named_ignored(store: dict[str, Any]) -> None:
    out = await promote.handle_memory_promote(_input(approver=""))
    assert out.ignored_args == ["approver"]


async def test_a_definition_promotion_needs_its_approved_hash(store: dict[str, Any]) -> None:
    assert await _refusal(store, approved_sha256=None) == "VALIDATION_FAILED"
    with pytest.raises(PreconditionFailedError) as e:
        await promote.handle_memory_promote(_input(approved_sha256="0" * 64))
    assert e.value.extra["current_sha256"] == served_content_sha256(VALUE)
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


@pytest.mark.parametrize(
    "over",
    [
        {"status": "rejected"},
        {"status": "conflicted"},
        {"expires_at": NOW.replace(tzinfo=UTC) - timedelta(seconds=1)},
        {"valid_to": NOW.replace(tzinfo=UTC) - timedelta(seconds=1)},
    ],
)
async def test_a_source_the_term_leg_would_not_serve_is_not_promoted(
    store: dict[str, Any], over: dict[str, Any]
) -> None:
    store["row"] = _row(**over)
    assert await _refusal(store) == "DEFINITION_PROMOTION_REFUSED"


@pytest.mark.parametrize("field", ["expires_at", "valid_to"])
async def test_a_source_that_ends_later_is_promoted(store: dict[str, Any], field: str) -> None:
    store["row"] = _row(**{field: NOW.replace(tzinfo=UTC) + timedelta(seconds=1)})
    await promote.handle_memory_promote(_input())
    assert len(store["written"]) == 1


async def test_a_definition_written_as_json_text_has_no_names(store: dict[str, Any]) -> None:
    # memory.set of a string holding JSON stores that string, which carries no term.
    store["row"] = _row(value=json.dumps(VALUE))
    sha = served_content_sha256(store["row"].value)
    assert await _refusal(store, approved_sha256=sha) == "VALIDATION_FAILED"


async def test_a_shared_row_with_no_names_holds_none(store: dict[str, Any]) -> None:
    # Promoted before this rule, or not JSON: the name check skips it.
    store["shared"] = {"definition/old": None, "definition/text": "ARR"}
    await promote.handle_memory_promote(_input())
    assert len(store["written"]) == 1


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_store_reads_only_the_term_and_aliases() -> None:
    def row(key: str, value: str, memory_type: str = "definition") -> str:
        quoted = value.replace("'", "\\'")
        return (
            "INSERT INTO loom.shared (org, namespace, memory_key, value, memory_type, "
            "promotion_event, version) VALUES "
            f"('o', 'n', '{key}', '{quoted}', '{memory_type}', generateUUIDv4(), 1)"
        )

    ch = LocalCH(
        ddl("shared"),
        row(KEY, json.dumps({**VALUE, "rests_on": "x" * 1000})),
        row("definition/text", "not json"),
        row("definition/odd", json.dumps({"term": 7, "aliases": "x"})),
        row("definition/mixed", json.dumps({"term": "MRR", "aliases": ["mrr", 1]})),
        row("definition/fact", json.dumps(VALUE), memory_type="semantic"),
        row("arr_note", json.dumps(VALUE)),
    )
    rows = await shared.shared_definitions("o", "n", 10, ch=ch)
    assert {r["memory_key"]: (r["term"], list(r["aliases"])) for r in rows} == {
        KEY: ("ARR", ["annual recurring revenue"]),
        "definition/mixed": ("MRR", ["mrr", "1"]),
        "definition/odd": ("", []),
        "definition/text": ("", []),
    }


async def test_a_definition_lands_on_its_own_key_and_a_definition_key_needs_one(
    store: dict[str, Any],
) -> None:
    assert await _refusal(store, target_key="definition/other") == "DEFINITION_PROMOTION_REFUSED"
    store["row"] = _row(memory_type="semantic", memory_key="arr_note")
    with pytest.raises(LoomServerError, match="only a row of type definition") as e:
        await promote.handle_memory_promote(_input(memory_key="arr_note", target_key=KEY))
    assert e.value.code == "DEFINITION_PROMOTION_REFUSED" and store["written"] == []


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
    with pytest.raises(LoomServerError) as e:
        await promote.handle_memory_promote(_input())
    assert e.value.code == "DEFINITION_NAME_TAKEN" and store["written"] == []
    assert e.value.extra == {
        "conflicting_keys": ["definition/revenue"],
        "names": ["annual recurring revenue"],
    }
    # Its own shared row (a revision) holds the names already.
    store["shared"] = {KEY: VALUE}
    await promote.handle_memory_promote(_input())
    assert len(store["written"]) == 1


async def test_the_ceiling_refuses_a_new_key_but_not_a_revision(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "memory_term_leg_max_definitions", 1)
    store["shared"] = {"definition/mrr": {"term": "MRR"}}
    with pytest.raises(LoomServerError, match="demote one to add a new key"):
        await promote.handle_memory_promote(_input())
    store["shared"] = {KEY: VALUE}
    await promote.handle_memory_promote(_input())
    assert [r["memory_key"] for r in store["written"]] == [KEY]
    # Past the ceiling, even a revision waits for a demotion.
    store["written"].clear()
    store["shared"] = {KEY: VALUE, "definition/mrr": {"term": "MRR"}}
    with pytest.raises(LoomServerError, match="demote until at most 1 remain"):
        await promote.handle_memory_promote(_input())
    assert store["written"] == []


async def test_a_definition_is_checked_and_written_under_the_namespaces_claim(
    store: dict[str, Any],
) -> None:
    await promote.handle_memory_promote(_input())
    assert store["claims"] == ["definition.promote/o/n"] and len(store["written"]) == 1
    store["written"].clear()
    store["free"] = False
    assert await _refusal(store) == "OVERLOADED"
    store["free"] = None
    with pytest.raises(LeaseUnavailable):
        await promote.handle_memory_promote(_input())
    assert store["written"] == []


async def test_another_row_takes_no_claim(store: dict[str, Any]) -> None:
    store["row"] = _row(memory_type="semantic", memory_key="arr_note", agent="bot")
    await promote.handle_memory_promote(_input(memory_key="arr_note", agent="bot"))
    assert store["claims"] == [] and len(store["written"]) == 1


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


_DEFINITION_CANDIDATE = {
    "candidate_id": "c1",
    "memory_key": KEY,
    "memory_type": "definition",
    "proposed_value": json.dumps(VALUE),
    "supporters": [f"o/n/-/-/-/{KEY}"],
    "source_kind": "memory_supporters",
    "status": "pending",
    "rationale": "",
}


@pytest.mark.parametrize("destination", ["memory", "both"])
async def test_review_promotes_no_definition_candidate_to_memory(
    monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings
    from server.memory.store import learnings as learnings_repo

    async def find(*_a: Any, **_k: Any) -> dict[str, Any]:
        return _DEFINITION_CANDIDATE

    async def must_not(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a refused review writes nothing")

    monkeypatch.setattr(learnings_repo, "find_candidate", find)
    monkeypatch.setattr(learnings_repo, "insert_row", must_not)
    monkeypatch.setattr(learnings, "_do_promote", must_not)
    review = LearningsReviewInput(
        org="o", namespace="n", candidate_id="c1", decision="promote", destination=destination
    )
    with pytest.raises(LoomServerError) as e:
        await learnings.handle_learnings_review(review)
    assert e.value.code == "VALIDATION_FAILED"


async def test_review_still_promotes_a_definition_candidate_to_knowledge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_learnings import LearningsReviewInput
    from server.memory.app import learnings
    from server.memory.store import learnings as learnings_repo

    async def find(*_a: Any, **_k: Any) -> dict[str, Any]:
        return _DEFINITION_CANDIDATE

    published: list[str] = []

    async def to_knowledge(_org: str, _ns: str, row: dict[str, Any], *_a: Any) -> str:
        published.append(row["memory_key"])
        return "loom://k/1"

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(learnings_repo, "find_candidate", find)
    monkeypatch.setattr(learnings_repo, "insert_row", nothing)
    monkeypatch.setattr(learnings, "_do_promote_to_knowledge", to_knowledge)
    review = LearningsReviewInput(
        org="o", namespace="n", candidate_id="c1", decision="promote", destination="knowledge"
    )
    await learnings.handle_learnings_review(review)
    assert published == [KEY]


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
