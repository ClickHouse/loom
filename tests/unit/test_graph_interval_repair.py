from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app.projections import (
    _repair_contradictory_edges,
    _repair_contradictory_triples,
    write_entity_edges,
)

# The repair's transaction time, in whole seconds as ClickHouse stores it.
_T = datetime(2026, 9, 12, 10, 0, 0)
_SEEN_BEFORE = datetime(2026, 9, 1)


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql: str, params: dict[str, Any] | None = None):
        self.execute_calls.append((sql, params or {}))

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ):
        self.insert_calls.append((table, rows, settings or {}))


def _bulk_input() -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="clickhouse",
        namespace="agent-memory",
        agent="validator",
        entries=[
            _BulkSetEntry(
                memory_key="user_employer",
                value="User works at ClickHouse.",
                relations=[("user", "works_at", "clickhouse")],
                valid_at=datetime(2025, 1, 1),
            )
        ],
    )


@pytest.mark.asyncio
async def test_contradictory_triple_repair_returns_interval_for_edge_close() -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2023, 1, 1),
                "valid_to": None,
                "last_seen": _SEEN_BEFORE,
            }
        ]
    )

    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2025, 1, 1),
            }
        ],
        ch=ch,
        at=_T,
    )

    assert len(repairs) == 1
    assert repairs[0].old_object == "acme"
    assert repairs[0].valid_from == datetime(2023, 1, 1)
    sql, params = ch.execute_calls[0]
    assert "ALTER TABLE loom.entity_triples" in sql
    assert "UPDATE valid_to = greatest(arrayElement({valid_tos:Array(DateTime)}" in sql
    assert params["valid_tos"] == [datetime(2025, 1, 1)]
    assert params["triples"] == [("user", "works_at", "acme")]


@pytest.mark.asyncio
async def test_contradictory_edge_repair_closes_old_pair_only() -> None:
    ch = StubCH()
    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2025, 1, 1),
            }
        ],
        ch=ch,
        at=_T,
    )
    assert repairs == []

    from server.memory.domain.graph_quality import IntervalRepair

    repairs = [
        IntervalRepair(
            subject="user",
            relation="works_at",
            old_object="acme",
            new_object="clickhouse",
            valid_to=datetime(2025, 1, 1),
            valid_from=datetime(2023, 1, 1),
        )
    ]
    await _repair_contradictory_edges(_bulk_input(), repairs, ch=ch)

    sql, params = ch.execute_calls[-1]
    assert "ALTER TABLE loom.entity_edges" in sql
    # one UPDATE per write: the pair and its valid_from travel in the key
    import calendar

    vf = calendar.timegm(datetime(2023, 1, 1).timetuple())
    assert (
        "IN {keys:Array(String)}" in sql
        and "valid_from < arrayElement({valid_tos:Array(DateTime)}" in sql
    )
    assert params["keys"] == [f"acme\x1fuser\x1f{vf}"]
    assert params["valid_tos"] == [datetime(2025, 1, 1)]


@pytest.mark.asyncio
async def test_entity_edges_use_namespace_canonical_resolution() -> None:
    ch = StubCH()
    bulk = MemoryBulkSetInput(
        org="clickhouse",
        namespace="agent-memory",
        agent="validator",
        entries=[
            _BulkSetEntry(
                memory_key="northwind_private",
                value="Northwind AI inherited ClickGap context.",
                entities=["Northwind AI", "ClickGap"],
            )
        ],
    )

    await write_entity_edges(
        bulk,
        [type("Result", (), {"memory_key": "northwind_private"})()],
        ch=ch,
        entity_map={"northwind ai": "northwindai"},
    )

    edge_rows = ch.insert_calls[0][1]
    assert edge_rows[0]["entity_a"] == "clickgap"
    assert edge_rows[0]["entity_b"] == "northwindai"


def test_single_valued_gate_matches_stored_lemma_forms() -> None:
    """The write path stores LEMMATIZED verbs ("works_at" -> "work_at");
    the single-valued gate must match those stored forms, not just the
    surface forms listed in _SINGLE_VALUED_RELATIONS — a surface-only set
    would silently disable interval repair for every listed relation."""
    from server.memory.domain.graph_quality import is_single_valued_relation

    for stored in ("work_at", "live_in", "employ_by", "base_in"):
        assert is_single_valued_relation(stored), stored
    for surface in ("works_at", "lives_in", "employed_by", "based_in"):
        assert is_single_valued_relation(surface), surface
    assert not is_single_valued_relation("mentions")


def test_interval_repair_fires_for_stored_lemma_relation() -> None:
    """triple_interval_repairs receives triples exactly as stored (lemma
    forms) — it must produce a closure for them."""
    from datetime import datetime

    from server.memory.domain.graph_quality import triple_interval_repairs

    repairs = triple_interval_repairs(
        existing=[
            {
                "subject": "user",
                "relation": "work_at",
                "object": "acme",
                "valid_from": datetime(2020, 1, 1),
                "valid_to": None,
            }
        ],
        new_subject="user",
        new_relation="work_at",
        new_object="clickhouse",
        new_valid_at=datetime(2024, 1, 1),
    )
    assert len(repairs) == 1
    assert repairs[0].old_object == "acme"
    assert repairs[0].valid_to == datetime(2024, 1, 1)


# ---------------------------------------------------------------------------
# Declared cardinality, undated facts, ties, invalid_at.
# ---------------------------------------------------------------------------


@pytest.fixture
def _declared(monkeypatch: pytest.MonkeyPatch):
    import server.identity.app.namespace_ontology as onto

    holder: dict[str, dict[str, str]] = {"declared": {}}

    async def _load(org: str, namespace: str) -> dict[str, str]:
        return holder["declared"]

    monkeypatch.setattr(onto, "load_relation_cardinality", _load)
    return holder


@pytest.mark.asyncio
async def test_repair_stamps_invalid_at_and_closes_equal_date_tie(_declared) -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2025, 1, 1),
                "valid_to": None,
                "last_seen": _SEEN_BEFORE,
            },
        ]
    )
    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2025, 1, 1),
            }
        ],
        ch=ch,
        at=_T,
    )
    assert [r.old_object for r in repairs] == ["acme"]
    sql, params = ch.execute_calls[0]
    assert "invalid_at = arrayElement({invalid_ats:Array(DateTime)}" in sql
    assert isinstance(params["invalid_ats"][0], datetime)
    assert params["valid_tos"] == [datetime(2025, 1, 1)]


@pytest.mark.asyncio
async def test_undated_single_valued_fact_orders_by_arrival(_declared) -> None:
    """An undated new `status` value still closes the open one: the repair
    orders by arrival instead of skipping it."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "task-1",
                "relation": "status",
                "object": "in progress",
                "valid_from": datetime(2026, 9, 3),
                "valid_to": None,
                "last_seen": _SEEN_BEFORE,
            },
        ]
    )
    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [{"subject": "task-1", "relation": "status", "object": "live", "valid_from": None}],
        ch=ch,
        at=_T,
    )
    assert [r.old_object for r in repairs] == ["in progress"]
    # The SELECT still scoped to the undated triple's subject + relation.
    _, qparams = ch.query_all_calls[0]
    assert qparams["subjects"] == ["task-1"] and qparams["relations"] == ["status"]
    assert isinstance(repairs[0].valid_to, datetime)


@pytest.mark.asyncio
async def test_declared_cardinality_wins_over_seed_list(_declared) -> None:
    ch = StubCH()
    existing = [
        {
            "subject": "user",
            "relation": "mentors",
            "object": "alice",
            "valid_from": datetime(2024, 1, 1),
            "valid_to": None,
            "last_seen": _SEEN_BEFORE,
        },
    ]
    # Undeclared "mentors" is multi-valued: nothing closes.
    ch.query_all_returns.append(list(existing))
    new = [
        {
            "subject": "user",
            "relation": "mentors",
            "object": "bob",
            "valid_from": datetime(2025, 1, 1),
        }
    ]
    assert await _repair_contradictory_triples(_bulk_input(), new, ch=ch, at=_T) == []
    # Declared "one": alice closes.
    _declared["declared"] = {"mentor": "one"}
    ch.query_all_returns.append(list(existing))
    repairs = await _repair_contradictory_triples(_bulk_input(), new, ch=ch, at=_T)
    assert [r.old_object for r in repairs] == ["alice"]
    # Declared "many" on a seed-list verb: nothing closes.
    _declared["declared"] = {"work_at": "many"}
    ch.query_all_returns.append(
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2023, 1, 1),
                "valid_to": None,
                "last_seen": _SEEN_BEFORE,
            }
        ]
    )
    assert (
        await _repair_contradictory_triples(
            _bulk_input(),
            [
                {
                    "subject": "user",
                    "relation": "works_at",
                    "object": "clickhouse",
                    "valid_from": datetime(2025, 1, 1),
                }
            ],
            ch=ch,
            at=_T,
        )
        == []
    )


@pytest.mark.asyncio
async def test_last_value_in_a_batch_survives(_declared) -> None:
    """One write carrying the whole employment history must leave exactly the
    newest value open, not close every row against every other."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2020, 1, 1),
                "valid_to": None,
                "last_seen": _SEEN_BEFORE,
            },
            {
                "subject": "user",
                "relation": "works_at",
                "object": "globex",
                "valid_from": datetime(2022, 6, 1),
                "valid_to": None,
                "last_seen": _SEEN_BEFORE,
            },
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2024, 3, 1),
                "valid_to": None,
                "last_seen": _SEEN_BEFORE,
            },
        ]
    )
    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2020, 1, 1),
            },
            {
                "subject": "user",
                "relation": "works_at",
                "object": "globex",
                "valid_from": datetime(2022, 6, 1),
            },
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2024, 3, 1),
            },
        ],
        ch=ch,
        at=_T,
    )
    closed = {(r.old_object, r.valid_to) for r in repairs}
    assert closed == {("acme", datetime(2022, 6, 1)), ("globex", datetime(2024, 3, 1))}


@pytest.mark.asyncio
async def test_many_repairs_fold_into_one_mutation_per_table(monkeypatch) -> None:
    """A write closes every interval it supersedes with one
    UPDATE per table (two when only some repairs carry a transaction time)."""
    from server.memory.app import projections as proj
    from server.memory.domain.graph_quality import IntervalRepair

    ch = StubCH()
    repairs = [
        IntervalRepair(
            subject="user",
            relation="works_at",
            old_object="acme",
            new_object="globex",
            valid_to=datetime(2024, 1, 1),
            valid_from=datetime(2023, 1, 1),
            invalid_at=datetime(2026, 9, 12),
        ),
        IntervalRepair(
            subject="user",
            relation="lives_in",
            old_object="paris",
            new_object="rome",
            valid_to=datetime(2025, 6, 1),
            valid_from=None,
            invalid_at=datetime(2026, 9, 12),
        ),
        IntervalRepair(
            subject="user",
            relation="drives",
            old_object="volvo",
            new_object="saab",
            valid_to=datetime(2025, 7, 1),
            valid_from=None,
            invalid_at=None,
        ),
    ]
    n = await proj.close_triples(
        ch,
        org="o",
        namespace="ns",
        closes=[(r.subject, r.relation, r.old_object, r.valid_to, r.invalid_at) for r in repairs],
    )
    assert n == 2 and len(ch.execute_calls) == 2
    with_inv, without = ch.execute_calls
    assert with_inv[1]["triples"] == [("user", "works_at", "acme"), ("user", "lives_in", "paris")]
    assert with_inv[1]["valid_tos"] == [datetime(2024, 1, 1), datetime(2025, 6, 1)]
    assert with_inv[1]["invalid_ats"] == [datetime(2026, 9, 12)] * 2
    assert "invalid_at" not in without[0] and without[1]["triples"] == [("user", "drives", "volvo")]
    # the row's own close is looked up by position; the IN keeps the mutation to the keyed rows
    assert (
        "arrayElement({valid_tos:Array(DateTime)}, indexOf({triples:Array(Tuple(String, String, String))}, (subject, relation, object)))"
        in with_inv[0]
    )
    assert "AND valid_to IS NULL" in with_inv[0]

    ch2 = StubCH()
    await _repair_contradictory_edges(_bulk_input(), repairs, ch=ch2)
    assert len(ch2.execute_calls) == 2
    keys = ch2.execute_calls[0][1]["keys"]
    assert len(keys) == 2 and keys[1] == "paris\x1fuser\x1f" or keys[1] == "user\x1fparis\x1f"
    assert not await proj.close_triples(ch, org="o", namespace="ns", closes=[])


# ---------------------------------------------------------------------------
# The re-check after a close: a value stated again while the repair ran.
# ---------------------------------------------------------------------------


class _Triples(StubCH):
    """entity_triples rows the reads filter as ClickHouse would; `on_close` lands
    as the repair's close runs (a racing write)."""

    def __init__(self, rows: list[dict[str, Any]], on_close: list[dict[str, Any]] = ()) -> None:
        super().__init__()
        self.rows, self.on_close = list(rows), list(on_close)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        params = params or {}
        self.query_all_calls.append((sql, params))
        keys = {tuple(t) for t in params.get("triples", [])}
        out = [
            r
            for r in self.rows
            if (not keys or (r["subject"], r["relation"], r["object"]) in keys)
            and (r["valid_to"] is None or ("invalid_at = {" in sql and r.get("invalid_at") == _T))
        ]
        if "AS landed" in sql:
            states: dict[tuple[str, str, str], dict[str, Any]] = {}
            for r in out:
                k = (r["subject"], r["relation"], r["object"])
                st = states.setdefault(k, {"subject": k[0], "relation": k[1], "object": k[2],
                                           "landed": False, "seen_since": False, "open": False,
                                           "raced": []})  # fmt: skip
                stamped = r.get("invalid_at") == params["at"]
                st["landed"] |= stamped
                st["seen_since"] |= r["valid_to"] is None and r["last_seen"] > params["at"]
                st["open"] |= r["valid_to"] is None
                if stamped and r["last_seen"] > params["at"]:
                    st["raced"].append((r["valid_from"], r["last_seen"]))
            return list(states.values())
        if "count()" in sql:
            n: dict[tuple[str, str, str], int] = {}
            for r in out:
                k = (r["subject"], r["relation"], r["object"])
                n[k] = n.get(k, 0) + 1
            return [
                {"subject": s, "relation": rel, "object": o, "n": c} for (s, rel, o), c in n.items()
            ]
        return out

    async def execute(self, sql: str, params: dict[str, Any] | None = None):
        await super().execute(sql, params)
        self.rows += self.on_close
        self.on_close = []


def _row(obj: str, valid_from: datetime | None, **closed: Any) -> dict[str, Any]:
    return {
        "subject": "user",
        "relation": "works_at",
        "object": obj,
        "valid_from": valid_from,
        "valid_to": closed.get("valid_to"),
        "invalid_at": closed.get("invalid_at"),
        "last_seen": closed.get("last_seen", _SEEN_BEFORE),
    }


async def _repair_to_paris(ch: _Triples) -> list[Any]:
    new = [
        {
            "subject": "user",
            "relation": "works_at",
            "object": "paris",
            "valid_from": datetime(2026, 1, 1),
        }
    ]
    return await _repair_contradictory_triples(_bulk_input(), new, ch=ch, at=_T)


def _reopened(ch: _Triples) -> bool:
    return any("UPDATE valid_to = NULL" in sql for sql, _ in ch.execute_calls)


@pytest.mark.asyncio
async def test_a_row_another_repair_closed_this_second_is_no_race(_declared) -> None:
    # Another repair closed the first London row in this second; this one closes the second.
    ch = _Triples(
        [
            _row("london", datetime(2024, 1, 1), valid_to=datetime(2025, 1, 1), invalid_at=_T),
            _row("london", datetime(2025, 1, 1)),
        ]
    )
    repairs = await _repair_to_paris(ch)
    assert [r.old_object for r in repairs] == ["london"] and not _reopened(ch)


@pytest.mark.parametrize(
    ("restated_from", "undone"),
    [(datetime(2024, 1, 1), False), (datetime(2026, 2, 1), True), (None, True)],
)
@pytest.mark.asyncio
async def test_only_a_restatement_not_older_than_the_new_value_undoes_its_close(
    _declared, restated_from, undone
) -> None:
    ch = _Triples([_row("london", datetime(2025, 1, 1))], on_close=[_row("london", restated_from)])
    repairs = await _repair_to_paris(ch)
    assert _reopened(ch) is undone
    assert [r.old_object for r in repairs] == ([] if undone else ["london"])


@pytest.mark.asyncio
async def test_a_same_date_value_that_arrived_later_closes_the_new_row(_declared) -> None:
    # Two writers racing on one date: this one's London arrived before the other's Paris.
    paris = {**_row("paris", datetime(2026, 1, 1)), "last_seen": datetime(2026, 9, 12, 9, 0, 2)}
    ch = _Triples([paris])
    new = [{"subject": "user", "relation": "works_at", "object": "london",
            "valid_from": datetime(2026, 1, 1), "last_seen": datetime(2026, 9, 12, 9, 0, 1)}]  # fmt: skip
    repairs = await _repair_contradictory_triples(_bulk_input(), new, ch=ch, at=_T)
    assert [(r.old_object, r.new_object) for r in repairs] == [("london", "paris")]


@pytest.mark.asyncio
async def test_the_later_of_two_same_date_values_in_one_write_stays_current(_declared) -> None:
    # One batch states Zurich, then Amsterdam, both on the session date: Zurich closes.
    day = datetime(2026, 9, 12)
    batch = [
        {
            "subject": "user",
            "relation": "works_at",
            "object": obj,
            "valid_from": day,
            "last_seen": _T,
        }  # fmt: skip
        for obj in ("zurich", "amsterdam")
    ]
    ch = _Triples([{**_row(t["object"], day), "last_seen": _T} for t in batch])
    repairs = await _repair_contradictory_triples(_bulk_input(), batch, ch=ch, at=_T)
    assert [(r.old_object, r.new_object) for r in repairs] == [("zurich", "amsterdam")]


_CLOSED_AT_T = {
    "valid_to": datetime(2026, 1, 1),
    "invalid_at": _T,
    "last_seen": _SEEN_BEFORE,
}


@pytest.mark.parametrize(
    ("london", "closed_again", "finished"),
    [
        # The close never landed: London's rows are the ones the write closed.
        ([datetime(2026, 9, 1)], True, True),
        # A row of this very write (an earlier value in its batch).
        ([_T], True, True),
        # Stated again after the write: that write's repair decides.
        ([datetime(2026, 9, 12, 10, 0, 5)], False, False),
        # The close landed and London was stated again in the same second.
        ([_T, _CLOSED_AT_T], False, False),
        # The close landed and nothing is open: only the edges and rows are left.
        ([_CLOSED_AT_T], False, True),
    ],
)
@pytest.mark.asyncio
async def test_a_replay_closes_a_recorded_value_again_only_when_its_close_never_landed(
    monkeypatch, london, closed_again, finished
) -> None:
    from server.memory.app import projections as proj
    from server.memory.domain.graph_quality import IntervalRepair

    done: list[Any] = []

    async def finish(input, repairs, *rest, ch):
        done.extend(repairs)

    monkeypatch.setattr(proj, "_repair_contradictory_edges", finish)
    monkeypatch.setattr(proj, "_retire_memories_behind_repairs", finish)
    made = IntervalRepair("user", "works_at", "london", "paris", datetime(2026, 1, 1), None, _T)
    rows = [
        {**_row("london", None), **(r if isinstance(r, dict) else {"last_seen": r})} for r in london
    ]
    ch = _Triples(rows)
    await proj._repair_triples(_bulk_input(), [], [], at=_T, replay=True, repaired=[made], ch=ch)
    closes = [p for sql, p in ch.execute_calls if "UPDATE valid_to = greatest" in sql]
    assert [c["triples"] for c in closes] == (
        [[("user", "works_at", "london")]] if closed_again else []
    )
    assert done == ([made, made] if finished else [])


@pytest.mark.parametrize("paris_since", [False, True])
@pytest.mark.parametrize("back_from", [None, datetime(2026, 9, 12)])
@pytest.mark.asyncio
async def test_a_replay_opens_a_value_its_close_took_from_a_write_since(
    _declared, monkeypatch, paris_since, back_from
) -> None:
    # "Back to London" (undated, or dated by its session day) landed while an undated
    # Rome's close ran, and the close took its row too. A Paris stated after it found
    # London closed: London, opened again, closes behind it.
    from server.memory.app import projections as proj
    from server.memory.domain.graph_quality import IntervalRepair

    done: list[Any] = []

    async def finish(input, repairs, *rest, ch):
        done.extend(repairs)

    monkeypatch.setattr(proj, "_repair_contradictory_edges", finish)
    monkeypatch.setattr(proj, "_retire_memories_behind_repairs", finish)
    made = IntervalRepair("user", "works_at", "london", "rome", _T, None, _T)
    back_seen = datetime(2026, 9, 12, 10, 0, 1)
    back = {**_row("london", back_from), **_CLOSED_AT_T, "last_seen": back_seen}
    rows = [{**_row("london", None), **_CLOSED_AT_T}, back]
    paris_seen = datetime(2026, 9, 12, 10, 0, 2)
    if paris_since:
        rows.append({**_row("paris", None), "last_seen": paris_seen})
    ch = _Triples(rows)
    await proj._repair_triples(_bulk_input(), [], [], at=_T, replay=True, repaired=[made], ch=ch)
    calls = [(sql, p) for sql, p in ch.execute_calls if "entity_triples" in sql]
    # London is judged while still closed (that close takes no row), then reopened.
    (reopen,) = [p for sql, p in calls if "UPDATE valid_to = NULL" in sql]
    assert reopen["triples"] == [("user", "works_at", "london")] and reopen["closed_at"] == _T
    after = calls[[p for _, p in calls].index(reopen) + 1 :]
    lost = [(r.old_object, r.new_object, r.valid_to) for r in done]
    if paris_since:
        # Closed again after the reopen, dated when Paris was seen; its edges close
        # and its rows retire.
        ((_, close),) = after
        assert close["triples"] == [("user", "works_at", "london")]
        assert close["valid_tos"] == [paris_seen] and close["invalid_ats"] == [back_seen]
        assert lost == [("london", "paris", paris_seen)] * 2
    else:
        assert len(calls) == 1 and done == []


@pytest.mark.parametrize("back_since", [False, True])
@pytest.mark.asyncio
async def test_a_replay_leaves_closed_a_value_stated_again_with_an_older_date(
    _declared, monkeypatch, back_since
) -> None:
    # "I lived in London in 2019" landed while the close ran: Rome, from 2026, is still
    # the newer, as the write's own re-check would have found. A "back in London"
    # written since is open: its own write repaired London, so the replay leaves it.
    from server.memory.app import projections as proj
    from server.memory.domain.graph_quality import IntervalRepair

    done: list[Any] = []

    async def finish(input, repairs, *rest, ch):
        done.extend(repairs)

    monkeypatch.setattr(proj, "_repair_contradictory_edges", finish)
    monkeypatch.setattr(proj, "_retire_memories_behind_repairs", finish)
    rome_from = datetime(2026, 1, 1)
    made = IntervalRepair("user", "works_at", "london", "rome", rome_from, None, _T, rome_from)
    back = {**_CLOSED_AT_T, "last_seen": datetime(2026, 9, 12, 10, 0, 1)}
    rows = [
        {**_row("london", None), **_CLOSED_AT_T},
        {**_row("london", datetime(2019, 1, 1)), **back},
    ]
    if back_since:
        rows.append(_row("london", None, last_seen=datetime(2026, 9, 12, 10, 0, 3)))
    ch = _Triples(rows)
    await proj._repair_triples(_bulk_input(), [], [], at=_T, replay=True, repaired=[made], ch=ch)
    assert not [p for sql, p in ch.execute_calls if "UPDATE valid_to" in sql]
    assert done == ([] if back_since else [made, made])


# The second London, seen last, is dated before Rome (not current), after it but before
# the first, or undated: then it starts when seen, after the first, and is the newest.
@pytest.mark.parametrize(
    ("second_from", "reopened_with"),
    [
        (datetime(2019, 1, 1), "first"),
        (datetime(2026, 3, 1), "first"),
        (None, "second"),
    ],
)
@pytest.mark.asyncio
async def test_a_replay_judges_every_row_stated_again_while_the_close_ran(
    _declared, monkeypatch, second_from, reopened_with
) -> None:
    # Two Londons raced Rome's close. The replay reopens with the one the interval
    # rule ranks newest.
    from server.memory.app import projections as proj
    from server.memory.domain.graph_quality import IntervalRepair

    reopened: list[Any] = []
    done: list[Any] = []

    async def reopen(input, repair, **kw):
        reopened.append((kw["valid_from"], kw["seen"]))

    async def finish(input, repairs, *rest, ch):
        done.extend(repairs)

    monkeypatch.setattr(proj, "_reopen_restated", reopen)
    monkeypatch.setattr(proj, "_repair_contradictory_edges", finish)
    monkeypatch.setattr(proj, "_retire_memories_behind_repairs", finish)
    rome_from = datetime(2026, 1, 1)
    made = IntervalRepair("user", "works_at", "london", "rome", rome_from, None, _T, rome_from)
    seen_1, seen_2 = datetime(2026, 9, 12, 10, 0, 1), datetime(2026, 9, 12, 10, 0, 2)
    rows = [
        {**_row("london", None), **_CLOSED_AT_T},
        {**_row("london", datetime(2026, 8, 1)), **_CLOSED_AT_T, "last_seen": seen_1},
        {**_row("london", second_from), **_CLOSED_AT_T, "last_seen": seen_2},
    ]
    ch = _Triples(rows)
    await proj._repair_triples(_bulk_input(), [], [], at=_T, replay=True, repaired=[made], ch=ch)
    first, second = (datetime(2026, 8, 1), seen_1), (second_from, seen_2)
    assert reopened == [first if reopened_with == "first" else second] and done == []


@pytest.mark.asyncio
async def test_a_close_that_failed_is_recorded_for_the_replay(_declared) -> None:
    # A timeout on an ALTER the server may have queued: the replay closes it again.
    class _Timeout(_Triples):
        async def execute(self, sql: str, params: dict[str, Any] | None = None):
            raise TimeoutError("close timed out")

    from server.memory.app.projection_errors import TriplesRepairFailed

    with pytest.raises(TriplesRepairFailed) as raised:
        await _repair_to_paris(_Timeout([_row("london", datetime(2025, 1, 1))]))
    assert [r.old_object for r in raised.value.repairs] == ["london"]


_SEEN = datetime(2026, 9, 12, 10, 0, 1)


@pytest.mark.parametrize(
    ("closer_from", "restated_from", "current"),
    [
        # An undated Rome closed London at the write; London dated by its session day.
        (None, datetime(2026, 9, 12), True),
        (None, None, True),
        # A dated Rome: a London dated before it stays closed, one after it is current.
        (datetime(2026, 6, 1), datetime(2019, 1, 1), False),
        (datetime(2026, 6, 1), datetime(2026, 8, 1), True),
        # An undated London is dated when it was seen, after Rome.
        (datetime(2026, 6, 1), None, True),
    ],
)
def test_a_restatement_is_current_when_the_interval_repair_says_it_is_newer(
    closer_from, restated_from, current
) -> None:
    from server.memory.domain.graph_quality import IntervalRepair, restates_current

    # An undated closer ends the value it closes at the write.
    to = closer_from or _T
    closed = IntervalRepair("user", "works_at", "london", "rome", to, None, _T, closer_from)
    assert restates_current(closed, restated_from, _SEEN, closed_at=_T, declared={}) is current


def test_a_restatement_is_judged_with_the_namespace_cardinality() -> None:
    # Single-valued only because the namespace declares it so.
    from server.memory.domain.graph_quality import IntervalRepair, restates_current

    closed = IntervalRepair("user", "mentors", "alice", "bob", _T, None, _T, None)
    args = (closed, datetime(2026, 9, 12), _SEEN)
    assert restates_current(*args, closed_at=_T, declared={"mentor": "one"})
    assert not restates_current(*args, closed_at=_T, declared={})
