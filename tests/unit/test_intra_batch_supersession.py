"""Templated substitutions inside one write supersede each other."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.memory.domain.conflicts import intra_batch_supersessions
from tests.unit.test_memory_import import _IDENT, _StoreCH, harness  # noqa: F401


def test_later_row_of_a_templated_pair_supersedes_the_earlier() -> None:
    values = [
        "The capital of Soviet Union is Moscow.",
        "Basketball was created in the country of United States of America.",
        "The capital of Soviet Union is Russellville.",
        "Basketball was created in the country of Soviet Union.",
        "Alice likes tea.",
    ]
    assert intra_batch_supersessions(values) == [(0, 2), (1, 3)]
    # two subjects sharing an object inside one write are not a pair
    assert (
        intra_batch_supersessions(
            [
                "Basketball was created in the country of Soviet Union.",
                "Club Nouveau was created in the country of Soviet Union.",
                "The capital of Great Britain is London.",
                "The capital of United Kingdom is London.",
            ]
        )
        == []
    )
    # a different memory type never pairs
    assert intra_batch_supersessions(
        values, ["semantic", "semantic", "episodic", "semantic", "semantic"]
    ) == [(1, 3)]
    assert intra_batch_supersessions([]) == []
    assert intra_batch_supersessions(["same", "same"]) == []


def test_stamping_keeps_every_row_on_one_key_set() -> None:
    """`ch.insert` derives the columns from row 0: a key present on some rows
    only fails the batch (every chunk of a fresh ingest 500'd)."""
    from datetime import datetime

    from server.memory.domain.conflicts import stamp_intra_batch_supersessions

    rows = [
        {
            "memory_id": "a",
            "memory_key": "k1",
            "value": "The capital of Soviet Union is Moscow.",
            "status": "active",
            "valid_at": None,
        },
        {
            "memory_id": "b",
            "memory_key": "k2",
            "value": "Alice likes tea.",
            "status": "active",
            "valid_at": None,
        },
        {
            "memory_id": "c",
            "memory_key": "k3",
            "value": "The capital of Soviet Union is Russellville.",
            "status": "active",
            "valid_at": datetime(2024, 1, 2),
        },
    ]
    embs = [
        {"memory_key": "k1", "status": "active", "invalid_at": None},
        {"memory_key": "k3", "status": "active", "invalid_at": None},
    ]
    got = stamp_intra_batch_supersessions(rows, embs, [(0, 2)], datetime(2026, 1, 1))
    assert got == [("a", "c")]
    assert rows[0]["status"] == "superseded" and rows[0]["superseded_by"] == "c"
    assert rows[0]["invalid_at"] == datetime(2024, 1, 2)
    assert embs[0]["status"] == "superseded" and embs[0]["invalid_at"] == datetime(2024, 1, 2)
    assert len({tuple(sorted(r.keys())) for r in rows}) == 1  # one key set for the insert
    assert rows[1]["status"] == "active" and rows[1]["superseded_by"] == ""


def test_parenthetical_aside_does_not_make_an_update_a_restatement() -> None:
    from server.memory.domain.conflicts import (
        same_content_words,
        strip_parentheticals,
        templated_substitution,
    )

    old = "Basketball was created in the country of United States of America."
    new = "Basketball was created in the country of Soviet Union (updated from United States of America)."
    assert strip_parentheticals(new) == "Basketball was created in the country of Soviet Union."
    assert not same_content_words(old, new)
    assert templated_substitution(old, new)
    assert same_content_words("PR 1 has a bug", "PR 1 has a bug (see the null check)")


def test_sweep_lead_in_is_stripped_from_values() -> None:
    from server.memory.app.extract.sweep import strip_statement_lead_in

    assert (
        strip_statement_lead_in("User stated that the capital of Italy is Duluth.")
        == "The capital of Italy is Duluth."
    )
    assert (
        strip_statement_lead_in("The user said that Thomas Arne was born in Bengaluru.")
        == "Thomas Arne was born in Bengaluru."
    )
    assert (
        strip_statement_lead_in("Thomas Arne was born in Bengaluru.")
        == "Thomas Arne was born in Bengaluru."
    )
    assert strip_statement_lead_in("User stated that no.") == "User stated that no."


def test_two_facts_of_a_many_valued_attribute_never_pair() -> None:
    import json

    assert intra_batch_supersessions(["I have a dog.", "I have a cat."]) == []
    # The rule knows English function words only, so it reads English sentences only.
    russian = ["У меня есть собака.", "У меня есть кошка."]
    assert intra_batch_supersessions(russian) == []
    assert intra_batch_supersessions([json.dumps(v) for v in russian]) == []
    french = ["Wang Xiaoming est âgé de 28 ans.", "Wang Xiaoming est ingénieur logiciel."]
    assert intra_batch_supersessions(french) == []


def test_two_predicates_of_one_object_never_pair() -> None:
    lives_and_works = ["The user currently lives in London.", "The user currently works in London."]
    assert intra_batch_supersessions(lives_and_works) == []


def test_a_key_written_twice_is_never_superseded_by_itself() -> None:
    """Two entries of one key are two versions of one row, and FINAL keeps the
    later: stamping the pair would retire the live value's vector and record the
    row as superseded by itself. A stamp lands on its own version's vector."""
    from datetime import datetime

    from server.memory.domain.conflicts import stamp_intra_batch_supersessions

    def row(key: str, version: int, value: str) -> dict:
        return {"memory_id": key, "memory_key": key, "version": version, "value": value}

    rows = [
        row("cap", 1, "The capital of Soviet Union is Moscow."),
        row("cap", 2, "The capital of Soviet Union is Russellville."),
    ]
    embs = [{"memory_key": "cap", "version": v, "status": "active"} for v in (1, 2)]
    pairs = intra_batch_supersessions([r["value"] for r in rows])
    assert pairs == [(0, 1)]
    assert stamp_intra_batch_supersessions(rows, embs, pairs, datetime(2026, 1, 1)) == []
    assert [r["status"] for r in rows + embs] == ["active"] * 4

    # k1 kept at its second version: the stamp lands on that version's vector only.
    rows = [
        row("k1", 1, "Alice likes tea."),
        row("k1", 2, "The capital of Soviet Union is Moscow."),
        row("k2", 3, "The capital of Soviet Union is Russellville."),
    ]
    embs = [
        {"memory_key": r["memory_key"], "version": r["version"], "status": "active"} for r in rows
    ]
    stamp_intra_batch_supersessions(rows, embs, [(1, 2)], datetime(2026, 1, 1))
    assert [e["status"] for e in embs] == ["active", "superseded", "active"]


# ---- which statement of a pair is newer ------------------------------------

_CAPITALS = [
    "The capital of Soviet Union is Moscow.",
    "The capital of Soviet Union is Russellville.",
    "The capital of Soviet Union is Paris.",
]
_NEW, _OLD = datetime(2024, 6, 1, tzinfo=UTC), datetime(2021, 1, 1)
_NOW = datetime(2026, 1, 1)


@pytest.mark.parametrize(
    ("valid_at", "survivor"),
    [
        ([_NEW, _OLD], 0),  # the later valid_at wins, naive and aware compared as UTC
        ([_OLD, _NEW], 1),
        ([None, None], 1),  # undated: the later place in the batch
        ([_OLD, None], 1),  # an undated statement is said now, after a past date
        ([None, _OLD], 0),
        ([_NEW, None, _OLD], 1),  # one order for the whole attribute, so no cycle
        ([_OLD, _NEW, None], 2),
    ],
)
def test_exactly_the_newest_statement_of_an_attribute_survives(
    valid_at: list[datetime | None], survivor: int
) -> None:
    from server.memory.domain.conflicts import stamp_intra_batch_supersessions

    rows = [
        {"memory_id": f"m{i}", "memory_key": f"k{i}", "value": v, "valid_at": at}
        for i, (v, at) in enumerate(zip(_CAPITALS, valid_at, strict=False))
    ]
    pairs = intra_batch_supersessions(
        [r["value"] for r in rows], valid_at=valid_at, written_at=_NOW
    )
    stamp_intra_batch_supersessions(rows, [], pairs, _NOW)
    assert [r["status"] == "active" for r in rows] == [i == survivor for i in range(len(rows))]
    # Each retired row points at a newer one, so following the chain ends at the survivor.
    by_id = {r["memory_id"]: r for r in rows}
    for row in rows:
        seen = set()
        while row["status"] == "superseded" and row["memory_id"] not in seen:
            seen.add(row["memory_id"])
            row = by_id[row["superseded_by"]]
        assert row["memory_id"] == f"m{survivor}"


async def _newest_wins(*_: Any, **__: Any) -> str:
    return "newest_wins"


@pytest.mark.asyncio
async def test_a_bulk_write_passes_the_rows_dates_and_its_write_time(monkeypatch) -> None:
    """Every bulk caller, memory.set_from_messages too: a past-dated statement does not
    retire an undated one written now."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState
    from tests.unit.bulk_stubs import StubCH

    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", _newest_wins)
    state = _BulkWriteState()
    state.memory_rows = [
        {"memory_id": "paris", "memory_key": "k0", "value": _CAPITALS[2], "valid_at": _OLD},
        {"memory_id": "moscow", "memory_key": "k1", "value": _CAPITALS[0], "valid_at": None},
    ]
    for row in state.memory_rows:
        row.update(memory_type="semantic", version=1)
    bulk = MemoryBulkSetInput(
        org="o", namespace="n", agent="a", entries=[{"memory_key": "k0", "value": "x"}]
    )
    assert await bulk_set._bulk_intra_batch_pairs(StubCH(), bulk, state) == [("paris", "moscow")]


@pytest.mark.asyncio
async def test_a_bulk_set_handler_keeps_the_later_dated_statement(
    harness,  # noqa: F811 — the import's fixture
    monkeypatch,
) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import bulk_set

    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", _newest_wins)
    ch = _StoreCH()
    harness(ch)
    bulk = MemoryBulkSetInput(
        **_IDENT,
        entries=[
            {"memory_key": "capital_now", "value": _CAPITALS[0], "valid_at": _NEW},
            {"memory_key": "capital_then", "value": _CAPITALS[1], "valid_at": _OLD},
        ],
    )
    await bulk_set.handle_memory_bulk_set(bulk)
    status = {r["memory_key"]: r["status"] for r in ch.rows_for("loom.memory")}
    assert status == {"capital_now": "active", "capital_then": "superseded"}
