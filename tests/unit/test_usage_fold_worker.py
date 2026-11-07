"""Usage fold-back worker — runs against a stub CH client.

The stub answers queries in order: probe (query_one) → cold-start
watermark derivation (query_one, first pass only) → per batch: the
pending-mutations probe (query_one, unseeded reads as 0), totals (query_all),
then one identity read (query_all) per size chunk of its keys.
A batch below the cap ends the pass; a full batch is followed by another
batch, from its pending-mutations probe.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from clickhouse_connect.driver.binding import format_bind_value

from server.core import ch_params
from server.memory.workers import usage_fold
from server.memory.workers.usage_fold import UsageFoldConfig, run_pass
from server.settings import settings
from server.workers.status import get_registry


class StubCH:
    def __init__(self) -> None:
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.fail_execute_prefixes: list[str] = []

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, list(rows), settings or {}))

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))
        for prefix in self.fail_execute_prefixes:
            if sql.startswith(prefix):
                raise RuntimeError(
                    getattr(self, "fail_execute_message", None)
                    or "Code: 344. DB::Exception: lightweight UPDATE unsupported"
                )


@pytest.fixture
def pg_folds(monkeypatch) -> list[list[Any]]:
    """Postgres on, with each pg_memory.fold_usage call's rows recorded."""
    from server.core import pg
    from server.memory.store import pg_memory

    calls: list[list[Any]] = []

    async def _fold(rows: Any) -> None:
        calls.append(list(rows))

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "fold_usage", _fold)
    return calls


@pytest.fixture(autouse=True)
def _fresh_worker_state(monkeypatch):
    # The `acted` derivation reads its own two statements
    # ahead of the fold's probe; the fold tests script the fold's answers
    # only, so it is stubbed here and exercised in test_usage_fold_acted.py.
    async def _no_acted(ch, result):
        return None

    monkeypatch.setattr(usage_fold, "acted_pass", _no_acted)
    usage_fold.reset_watermark()
    get_registry().reset()
    yield
    usage_fold.reset_watermark()
    get_registry().reset()


T0 = datetime(2026, 6, 11, 12, 0, 0)  # naive, as the driver returns
MID = "6b47c9b7-0000-0000-0000-000000000001"
MID2 = "6b47c9b7-0000-0000-0000-000000000002"


def _totals_row(
    *,
    recall: int = 3,
    evidence: int = 0,
    used: int = 0,
    harm: int = 0,
    last_recalled: datetime | None = T0,
    bumped: datetime = T0,
    memory_id: str = MID,
    namespace: str = "ns",
) -> dict[str, Any]:
    return {
        "org": "clickhouse",
        "namespace": namespace,
        "memory_id": memory_id,
        "recall_total": recall,
        "evidence_total": evidence,
        "used_total": used,
        "harm_total": harm,
        "last_recalled": last_recalled,
        "bumped": bumped,
    }


def _ident_row(
    *,
    reinforcement_count: int = 1,
    memory_id: str = MID,
    memory_key: str = "k",
    namespace: str = "ns",
) -> dict[str, Any]:
    # The worker selects `toString(memory_id) AS mid` — the renamed alias.
    return {
        "mid": memory_id,
        "org": "clickhouse",
        "namespace": namespace,
        "agent": "bot",
        "user_id": "-",
        "memory_key": memory_key,
        "reinforcement_count": reinforcement_count,
    }


def _seed_active_pass(
    ch: StubCH,
    *,
    totals: list[dict[str, Any]],
    idents: list[dict[str, Any]],
    hwm: datetime = T0,
    first_unfolded: datetime | None = T0,
    cold_start: bool = True,
) -> None:
    ch.query_one_returns.append({"hwm": hwm})
    if cold_start:
        ch.query_one_returns.append({"first_unfolded": first_unfolded})
    ch.query_all_returns.append(totals)
    ch.query_all_returns.append(idents)


def _updates(ch: StubCH, table: str) -> list[tuple[str, dict[str, Any]]]:
    return [(sql, p) for sql, p in ch.execute_calls if f" {table} " in sql]


REINF = "loom.memory_reinforcement"


def _reinf_batches(ch: StubCH) -> list[list[dict[str, Any]]]:
    """The fold's INSERT statements into loom.memory_reinforcement."""
    return [rows for t, rows, _ in ch.insert_calls if t == REINF]


def _reinf_rows(ch: StubCH) -> list[dict[str, Any]]:
    return [r for rows in _reinf_batches(ch) for r in rows]


def _rc(ch: StubCH, key: str = "k", agent: str = "bot") -> int:
    """The folded reinforcement_count inserted for one fold identity."""
    rows = [r for r in _reinf_rows(ch) if r["memory_key"] == key and r["agent"] == agent]
    assert len(rows) == 1, rows
    return int(rows[0]["reinforcement_count"])


def _key(memory_id: str, namespace: str = "ns") -> tuple[str, str, str]:
    """A memory's key in the loom.memory UPDATE: its org, namespace and id."""
    return ("clickhouse", namespace, memory_id)


def _pick(params: dict[str, Any], key_list: str, key: tuple[str, str, str], values: str) -> Any:
    """The per-row target a transform() array carries for one key."""
    return params[values][params[key_list].index(key)]


async def test_noop_when_no_usage_at_all() -> None:
    """Empty totals → single probe query, no derivation, no GROUP BY, no mutations."""
    ch = StubCH()
    ch.query_one_returns.append({"hwm": None})
    result = await run_pass(ch=ch)
    assert result.candidates == 0
    assert result.folded == 0
    assert len(ch.query_one_calls) == 1
    assert "max(bumped_at)" in ch.query_one_calls[0][0]
    assert ch.query_all_calls == []
    assert ch.execute_calls == []


async def test_noop_when_watermark_already_at_high_water() -> None:
    """Second pass with no new events stops at the probe (no re-derivation)."""
    ch = StubCH()
    _seed_active_pass(ch, totals=[_totals_row()], idents=[_ident_row()])
    await run_pass(ch=ch)
    assert usage_fold.current_watermark() == T0.replace(tzinfo=UTC)

    ch2 = StubCH()
    ch2.query_one_returns.append({"hwm": T0})
    result = await run_pass(ch=ch2)
    assert result.folded == 0
    assert len(ch2.query_one_calls) == 1
    assert ch2.query_all_calls == []
    assert ch2.execute_calls == []


async def test_identity_lookup_does_not_shadow_memory_id() -> None:
    """`AS memory_id` would make ClickHouse compare the String alias to the
    UUID array and fold nothing."""
    ch = StubCH()
    _seed_active_pass(ch, totals=[_totals_row()], idents=[_ident_row()])
    await run_pass(ch=ch)
    ident_sql = ch.query_all_calls[1][0]
    assert "toString(m.memory_id) AS mid" in ident_sql
    assert "AS memory_id" not in ident_sql
    assert "memory_id IN {ids:Array(UUID)}" in ident_sql


async def test_fold_writes_absolute_reinforcement_base_plus_live() -> None:
    """embeddings.reinforcement_count := write-time base + recall + evidence,
    carried as a transform() array keyed by memory_key."""
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=3, evidence=2)],
        idents=[_ident_row(reinforcement_count=1)],
    )
    result = await run_pass(ch=ch)
    assert result.candidates == 1
    assert result.folded == 1
    assert result.statements == 2

    # The reinforcement fold is an INSERT into the side table,
    # one complete row per fold identity (org, namespace, agent, user_id,
    # memory_key) — never a lightweight UPDATE on loom.memory_embeddings.
    assert _updates(ch, "loom.memory_embeddings") == []
    batches = _reinf_batches(ch)
    assert len(batches) == 1 and len(batches[0]) == 1
    (row,) = batches[0]
    from server.memory.app.reinforcement import REINFORCEMENT_PERSISTED_COLS

    assert set(row) == set(REINFORCEMENT_PERSISTED_COLS)  # every column, the RMT rule
    assert (row["org"], row["namespace"], row["agent"], row["user_id"], row["memory_key"]) == (
        "clickhouse",
        "ns",
        "bot",
        "-",
        "k",
    )
    assert row["reinforcement_count"] == 1 + 3 + 2  # base + recall + evidence
    assert row["last_reinforced_at"] == T0  # naive, as ClickHouse stores it
    assert row["version"] > 0
    settings_used = [st for t, _, st in ch.insert_calls if t == REINF][0]
    assert settings_used == {"async_insert": 0, "wait_for_async_insert": 1}

    mem = _updates(ch, "loom.memory")
    assert len(mem) == 1
    sql, params = mem[0]
    # +1 write-time evidence credit (schema DEFAULT 1); what the SQL does with these
    # values is checked on clickhouse-local (test_usage_fold_sql.py).
    assert _pick(params, "keys", _key(MID), "evs") == 1 + 2
    assert _pick(params, "keys", _key(MID), "rcls") == 3
    assert _pick(params, "keys", _key(MID), "lras") == T0.replace(tzinfo=UTC)


async def test_never_recalled_row_keeps_last_recalled_at_null() -> None:
    """Evidence-only activity: the DateTime array carries the epoch sentinel
    and the SET leaves last_recalled_at alone for that row."""
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=0, evidence=2, last_recalled=None)],
        idents=[_ident_row()],
    )
    await run_pass(ch=ch)
    sql, params = _updates(ch, "loom.memory")[0]
    assert "= toDateTime(0), last_recalled_at" in sql  # sentinel → keep column
    assert _pick(params, "keys", _key(MID), "lras") == datetime(1970, 1, 1, tzinfo=UTC)


async def test_refold_converges_to_same_absolute_targets() -> None:
    """Re-folding the same totals produces identical targets — no double-counting."""
    first = StubCH()
    _seed_active_pass(
        first,
        totals=[_totals_row(recall=5, evidence=4)],
        idents=[_ident_row(reinforcement_count=7)],
    )
    await run_pass(ch=first)

    usage_fold.reset_watermark()  # simulate restart
    second = StubCH()
    _seed_active_pass(
        second,
        totals=[_totals_row(recall=5, evidence=4)],
        idents=[_ident_row(reinforcement_count=7)],
    )
    await run_pass(ch=second)

    assert first.execute_calls == second.execute_calls
    assert _rc(second) == 7 + 5 + 4  # base + recall + evidence
    # the same absolute value twice: the RMT(version) side table collapses to it
    assert _rc(first) == _rc(second)


async def test_import_seeded_base_preserved_above_live_total() -> None:
    """Seeded base survives: embeddings add live recalls on top, counters use greatest()."""
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=3, evidence=0)],
        idents=[_ident_row(reinforcement_count=500)],
    )
    await run_pass(ch=ch)

    assert _rc(ch) == 500 + 3
    (sql, params) = _updates(ch, "loom.memory")[0]
    # worker supplies only the live total; greatest() keeps the seeded value.
    assert "greatest(recall_count, transform(" in sql
    assert params["rcls"] == [3]


async def test_evidence_only_activity_feeds_embeddings_reinforcement() -> None:
    """Positive-outcome (evidence) events fold onto the search-side
    reinforcement signal too — so memory.outcome drives ranking, not just the
    promote gate. last_reinforced_at advances off bumped_at (the decay clock)."""
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=0, evidence=2, last_recalled=None)],
        idents=[_ident_row()],
    )
    result = await run_pass(ch=ch)
    assert result.folded == 1
    rows = _reinf_rows(ch)
    assert len(rows) == 1
    assert rows[0]["reinforcement_count"] == 1 + 0 + 2  # base + recall + evidence
    assert rows[0]["last_reinforced_at"] == T0  # bumped_at
    # evidence_count still folds onto loom.memory for the promote gate.
    (sql, params) = _updates(ch, "loom.memory")[0]
    assert params["evs"] == [1 + 2]


async def test_used_events_reinforce_by_weight_not_by_exposure(monkeypatch) -> None:
    """`used` (the agent relied on the row) folds onto the search-side
    reinforcement at LOOM_MEMORY_USED_REINFORCEMENT_WEIGHT recall-equivalents;
    it never touches evidence_count (the promote gate is about outcomes)."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_used_reinforcement_weight", 3)
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=1, evidence=0, used=2)],
        idents=[_ident_row(reinforcement_count=1)],
    )
    result = await run_pass(ch=ch)
    assert result.folded == 1
    assert _rc(ch) == 1 + 1 + 0 + 3 * 2  # base + recall + evidence + w*used
    (sql, params) = _updates(ch, "loom.memory")[0]
    assert params["evs"] == [1]  # untouched by `used`
    assert params["rcls"] == [1]


async def test_used_only_activity_still_folds() -> None:
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=0, evidence=0, used=1, last_recalled=None)],
        idents=[_ident_row()],
    )
    result = await run_pass(ch=ch)
    assert result.folded == 1
    assert len(_reinf_rows(ch)) == 1
    # the totals query sums the new column
    totals_sql = ch.query_all_calls[0][0]
    assert "sum(used_count)" in totals_sql and "AS used_total" in totals_sql


async def test_harm_subtracts_by_weight_and_floors_at_the_write_time_base(monkeypatch) -> None:
    """`harm` (relied on, run ended badly) takes back earned reinforcement at
    LOOM_MEMORY_HARM_REINFORCEMENT_WEIGHT recall-equivalents; the floor is the
    write-time base, so a much-harmed row lands on its cosine rank, never
    below it, and evidence_count is untouched."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_used_reinforcement_weight", 2)
    monkeypatch.setattr(settings, "memory_harm_reinforcement_weight", 2)
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=4, evidence=1, used=2, harm=1)],
        idents=[_ident_row(reinforcement_count=3)],
    )
    await run_pass(ch=ch)
    assert _rc(ch) == 3 + 4 + 1 + 2 * 2 - 2 * 1  # base + recall + evidence + w·used − w·harm
    (_, params) = _updates(ch, "loom.memory")[0]
    assert params["evs"] == [2]  # 1 + evidence_total, harm never touches the promote gate

    # Floor: harm beyond the earned reinforcement stops at the base.
    usage_fold.reset_watermark()
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=1, evidence=0, used=0, harm=10)],
        idents=[_ident_row(reinforcement_count=3)],
    )
    await run_pass(ch=ch)
    assert _rc(ch) == 3


async def test_harm_is_charged_at_the_used_weight_by_default(monkeypatch) -> None:
    """Default weight None = the `used` weight: the totals
    query sums harm_count and the fold subtracts it, floored at the base."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_harm_reinforcement_weight", None)
    monkeypatch.setattr(settings, "memory_used_reinforcement_weight", 2)
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=2, harm=5)],
        idents=[_ident_row(reinforcement_count=1)],
    )
    await run_pass(ch=ch)
    totals_sql = ch.query_all_calls[0][0]
    assert "sum(harm_count)" in totals_sql and "AS harm_total" in totals_sql
    assert _rc(ch) == 1  # 1 + 2 - 2*5 floors at the base


async def test_harm_weight_zero_counts_without_charging(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_harm_reinforcement_weight", 0)
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=2, harm=5)],
        idents=[_ident_row(reinforcement_count=1)],
    )
    await run_pass(ch=ch)
    assert _rc(ch) == 1 + 2


async def test_harm_only_activity_still_folds() -> None:
    """A row that was only ever harmed still gets its embeddings row folded,
    so the floor is applied rather than skipped as 'no activity'."""
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=0, evidence=0, used=0, harm=1, last_recalled=None)],
        idents=[_ident_row(reinforcement_count=2)],
    )
    result = await run_pass(ch=ch)
    assert result.folded == 1
    assert len(_reinf_rows(ch)) == 1


async def test_no_activity_at_all_skips_the_reinforcement_insert() -> None:
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=0, evidence=0, used=0, last_recalled=None)],
        idents=[_ident_row()],
    )
    result = await run_pass(ch=ch)
    assert result.folded == 1
    assert len(_updates(ch, "loom.memory")) == 1
    assert _reinf_batches(ch) == []


async def test_batch_is_one_statement_per_table_not_per_identity_group() -> None:
    """Three memories, two fold identity prefixes, one (org, ns):
    1 loom.memory UPDATE + 1 loom.memory_reinforcement INSERT — not 6, and
    not 3 (one per identity group)."""
    mid3 = "6b47c9b7-0000-0000-0000-000000000003"
    ch = StubCH()
    idents = [
        _ident_row(memory_id=MID, memory_key="a"),
        _ident_row(memory_id=MID2, memory_key="b"),
        {**_ident_row(memory_id=mid3, memory_key="c"), "agent": "other-bot"},
    ]
    _seed_active_pass(
        ch,
        totals=[
            _totals_row(memory_id=MID, recall=1),
            _totals_row(memory_id=MID2, recall=2),
            _totals_row(memory_id=mid3, recall=3),
        ],
        idents=idents,
    )
    result = await run_pass(ch=ch)
    assert result.folded == 3
    mem = _updates(ch, "loom.memory")
    assert len(mem) == 1
    assert sorted(mem[0][1]["keys"]) == sorted(_key(m) for m in (MID, MID2, mid3))
    assert _pick(mem[0][1], "keys", _key(MID2), "rcls") == 2
    batches = _reinf_batches(ch)
    assert len(batches) == 1
    assert [(r["agent"], r["memory_key"], r["reinforcement_count"]) for r in batches[0]] == [
        ("bot", "a", 1 + 1),
        ("bot", "b", 1 + 2),
        ("other-bot", "c", 1 + 3),
    ]
    assert result.statements == 2


async def test_update_chunk_bounds_the_arrays() -> None:
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(memory_id=MID), _totals_row(memory_id=MID2)],
        idents=[
            _ident_row(memory_id=MID, memory_key="a"),
            _ident_row(memory_id=MID2, memory_key="b"),
        ],
    )
    await run_pass(cfg=UsageFoldConfig(update_chunk_rows=1), ch=ch)
    assert len(_updates(ch, "loom.memory")) == 2
    assert len(_reinf_batches(ch)) == 2


async def test_totals_for_erased_memory_are_skipped() -> None:
    """Orphaned usage rows: no mutations, but the watermark still advances."""
    ch = StubCH()
    _seed_active_pass(ch, totals=[_totals_row()], idents=[])
    result = await run_pass(ch=ch)
    assert result.folded == 0
    assert result.skipped_no_memory == 1
    assert ch.execute_calls == []
    assert usage_fold.current_watermark() == T0.replace(tzinfo=UTC)


async def test_cold_start_watermark_is_derived_not_epoch() -> None:
    """A restart starts just before the earliest unfolded totals row instead
    of re-walking history from 1970 at the batch cap."""
    first_unfolded = T0 - timedelta(days=3)
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row()],
        idents=[_ident_row()],
        hwm=T0,
        first_unfolded=first_unfolded,
    )
    await run_pass(ch=ch)
    derive_sql, _ = ch.query_one_calls[1]
    assert "min(t.bumped) AS first_unfolded" in derive_sql
    assert "t.recall_total > m.recall_count" in derive_sql
    assert "(1 + t.evidence_total) > m.evidence_count" in derive_sql
    # The totals query was bounded by the derived watermark (first_unfolded − 1 s).
    _, params = ch.query_all_calls[0]
    assert params["wm"] == (first_unfolded - timedelta(seconds=1)).replace(tzinfo=UTC)


async def test_cold_start_with_nothing_unfolded_jumps_to_high_water() -> None:
    ch = StubCH()
    ch.query_one_returns.append({"hwm": T0})
    ch.query_one_returns.append({"first_unfolded": None})
    result = await run_pass(ch=ch)
    assert result.candidates == 0
    assert ch.query_all_calls == []
    assert usage_fold.current_watermark() == T0.replace(tzinfo=UTC)


async def test_watermark_advances_to_max_bumped_of_batch() -> None:
    later = T0 + timedelta(minutes=2)
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[
            _totals_row(bumped=T0),
            _totals_row(memory_id=MID2, bumped=later),
        ],
        idents=[_ident_row(), _ident_row(memory_id=MID2, memory_key="k2")],
        hwm=later,
    )
    result = await run_pass(ch=ch)
    assert result.folded == 2
    assert usage_fold.current_watermark() == later.replace(tzinfo=UTC)
    # Filter AFTER aggregation: a row-level predicate undercounts unmerged parts.
    totals_sql = ch.query_all_calls[0][0]
    assert "HAVING bumped > {wm:DateTime}" in totals_sql
    assert "GROUP BY org, namespace, memory_id" in totals_sql


async def test_full_batch_steps_watermark_back_and_keeps_going() -> None:
    """At the batch cap the pass takes another batch (stepping the watermark
    back 1 s to re-fold tied boundary rows) until a short batch drains it."""
    later = T0 + timedelta(minutes=5)
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[
            _totals_row(bumped=T0),
            _totals_row(memory_id=MID2, bumped=later),
        ],
        idents=[_ident_row(), _ident_row(memory_id=MID2, memory_key="k2")],
        hwm=later,
    )
    # Second batch: the boundary row again (tie re-fold), short, so the pass ends.
    ch.query_all_returns.append([_totals_row(memory_id=MID2, bumped=later)])
    ch.query_all_returns.append([_ident_row(memory_id=MID2, memory_key="k2")])
    result = await run_pass(cfg=UsageFoldConfig(max_memories_per_pass=2), ch=ch)
    assert result.batches == 2
    assert result.candidates == 3
    assert len(ch.query_all_calls) == 4
    # Batch 2 started from later − 1 s, then the short batch pinned the watermark.
    assert ch.query_all_calls[2][1]["wm"] == (later - timedelta(seconds=1)).replace(tzinfo=UTC)
    assert usage_fold.current_watermark() == later.replace(tzinfo=UTC)


async def test_time_budget_ends_a_pass_between_batches() -> None:
    later = T0 + timedelta(minutes=5)
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(bumped=T0), _totals_row(memory_id=MID2, bumped=later)],
        idents=[_ident_row(), _ident_row(memory_id=MID2, memory_key="k2")],
        hwm=later,
    )
    result = await run_pass(
        cfg=UsageFoldConfig(max_memories_per_pass=2, pass_time_budget_seconds=0.0), ch=ch
    )
    assert result.batches == 1  # full batch, but the budget was already spent
    assert len(ch.query_all_calls) == 2


async def test_failed_fold_leaves_watermark_for_idempotent_retry() -> None:
    """A mid-batch failure raises without advancing the watermark past the
    derived cold-start value."""

    class ExplodingCH(StubCH):
        async def execute(self, sql, params=None):
            raise RuntimeError("boom")

    ch = ExplodingCH()
    _seed_active_pass(ch, totals=[_totals_row()], idents=[_ident_row()])
    with pytest.raises(RuntimeError):
        await run_pass(ch=ch)
    assert usage_fold.current_watermark() == (T0 - timedelta(seconds=1)).replace(tzinfo=UTC)
    status = get_registry().snapshot()["usage_fold"]
    assert status["consecutive_failures"] == 1


async def test_falls_back_to_alter_update_per_table() -> None:
    """No lightweight UPDATE support (code 344 SUPPORT_IS_DISABLED) → ALTER
    UPDATE mutation path — for loom.memory only; the reinforcement fold is an
    INSERT and never touches the mutation path."""
    ch = StubCH()
    ch.fail_execute_prefixes = ["UPDATE "]
    ch.fail_execute_message = "Code: 344. DB::Exception: Lightweight updates are not allowed"
    _seed_active_pass(ch, totals=[_totals_row(recall=1)], idents=[_ident_row()])
    result = await run_pass(ch=ch)
    assert result.folded == 1
    alters = [sql for sql, _ in ch.execute_calls if sql.startswith("ALTER TABLE ")]
    assert len(alters) == 1  # loom.memory
    assert all("SETTINGS mutations_sync = 0" in sql for sql in alters)
    assert "loom.memory_embeddings" not in " ".join(sql for sql, _ in ch.execute_calls)
    assert len(_reinf_rows(ch)) == 1


# ---- mutation storm guards ----------------------------------


@pytest.mark.asyncio
async def test_one_statement_per_table_across_many_namespaces() -> None:
    """60 memories in 60 namespaces are one statement per table (chunk 500),
    not 60 + 60."""
    ch = StubCH()
    mids = [f"6b47c9b7-0000-0000-0000-0000000000{i:02d}" for i in range(10, 70)]
    _seed_active_pass(
        ch,
        totals=[
            _totals_row(memory_id=m, namespace=f"ns-{i}", recall=1) for i, m in enumerate(mids)
        ],
        idents=[
            _ident_row(memory_id=m, namespace=f"ns-{i}", memory_key=f"k{i}")
            for i, m in enumerate(mids)
        ],
    )
    result = await run_pass(ch=ch)
    assert result.folded == 60 and result.statements == 2
    mem = _updates(ch, "loom.memory")
    batches = _reinf_batches(ch)
    assert len(mem) == 1 and len(batches) == 1
    assert len(mem[0][1]["pairs"]) == 60 and len({r["namespace"] for r in batches[0]}) == 60


@pytest.mark.asyncio
async def test_pending_mutation_cap_skips_the_pass_and_holds_the_watermark() -> None:
    ch = StubCH()
    ch.query_one_returns += [{"hwm": T0}, {"first_unfolded": T0}]
    # the backpressure probe (loom.memory, the one mutation target left) is
    # the query_one after the watermark probes
    ch.query_one_returns.append({"n": settings.memory_usage_fold_max_pending_mutations + 1})
    result = await run_pass(ch=ch)
    assert result.skipped_pending_mutations == settings.memory_usage_fold_max_pending_mutations + 1
    assert result.folded == 0 and result.statements == 0
    assert not [sql for sql, _ in ch.execute_calls if sql.startswith(("UPDATE ", "ALTER "))]
    assert ch.insert_calls == []
    probe_sql, probe_params = ch.query_one_calls[-1]
    assert "system.mutations" in probe_sql and probe_params["t"] == "memory"
    assert ch.query_all_calls == []
    assert usage_fold.current_watermark() == (T0 - timedelta(seconds=1)).replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_read_only_replica_does_not_fall_back_to_alter() -> None:
    """Code 242 (or 999, 341) is a transient: the pass fails closed and the
    next one retries from the same watermark; no heavy mutation is queued."""
    ch = StubCH()
    ch.fail_execute_prefixes = ["UPDATE "]
    ch.fail_execute_message = "Code: 242. DB::Exception: Table is in readonly mode"
    _seed_active_pass(ch, totals=[_totals_row(recall=1)], idents=[_ident_row()])
    with pytest.raises(RuntimeError):
        await run_pass(ch=ch)
    assert not [sql for sql, _ in ch.execute_calls if sql.startswith("ALTER TABLE ")]


async def test_one_memory_id_in_two_namespaces_folds_into_each() -> None:
    """A namespace restored from another's snapshot holds the same memory id: each
    namespace's usage lands on its own row, not on whichever identity was read last."""
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[
            _totals_row(namespace="a", recall=2),
            _totals_row(namespace="b", recall=7),
        ],
        idents=[
            _ident_row(namespace="a", memory_key="ka"),
            _ident_row(namespace="b", memory_key="kb"),
        ],
    )
    result = await run_pass(ch=ch)
    assert result.folded == 2
    by_ns = {r["namespace"]: r for r in _reinf_rows(ch)}
    assert by_ns["a"]["memory_key"] == "ka" and by_ns["b"]["memory_key"] == "kb"
    assert _rc(ch, "ka") == 1 + 2 and _rc(ch, "kb") == 1 + 7
    [(_, params)] = _updates(ch, "loom.memory")
    assert _pick(params, "keys", _key(MID, "a"), "rcls") == 2
    assert _pick(params, "keys", _key(MID, "b"), "rcls") == 7


async def test_a_total_whose_namespace_holds_no_live_row_is_skipped() -> None:
    """Usage for a namespace whose copy is gone folds onto nothing, not onto another
    namespace's row of the same id."""
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(namespace="b", recall=4)],
        idents=[_ident_row(namespace="a")],
    )
    result = await run_pass(ch=ch)
    assert (result.folded, result.skipped_no_memory) == (0, 1)
    assert _updates(ch, "loom.memory") == [] and _reinf_rows(ch) == []


async def test_two_keys_holding_one_id_in_a_namespace_are_both_folded(pg_folds) -> None:
    """A namespace restored into itself under another agent holds the id under two
    keys; the totals cannot tell them apart, so both keys take them."""
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(recall=3)],
        idents=[_ident_row(memory_key="k1"), _ident_row(memory_key="k2")],
    )
    result = await run_pass(ch=ch)
    assert (result.folded, result.skipped_no_memory) == (1, 0)
    assert _rc(ch, "k1") == _rc(ch, "k2") == 1 + 3
    # one memory: the loom.memory UPDATE carries its key once
    [(_, params)] = _updates(ch, "loom.memory")
    assert params["keys"] == [_key(MID)]
    assert pg_folds == [[(*_key(MID), 1, 3, T0.replace(tzinfo=UTC))]]


async def test_under_postgres_the_fold_raises_the_heads_too(monkeypatch, pg_folds) -> None:
    """The fold's counters reach the Postgres heads as well; a memory never recalled
    sends no last-recall time."""
    from server.core.datetimes import UNIX_EPOCH

    async def _update(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(usage_fold, "_apply_update", _update)
    seen = datetime(2026, 5, 1, tzinfo=UTC)

    def _target(mid: str, last: datetime) -> Any:
        return usage_fold._Target(
            memory_id=mid,
            org="o",
            namespace="n",
            agent="a",
            user_id="-",
            memory_key="k",
            evidence=2,
            recall=3,
            last_recalled=last,
            reinforcement=None,
            last_activity=seen,
        )

    await usage_fold._fold_memory_rows(None, [_target("m1", UNIX_EPOCH), _target("m2", seen)], 10)
    assert pg_folds == [[("o", "n", "m1", 2, 3, None), ("o", "n", "m2", 2, 3, seen)]]


async def test_keys_cut_by_size_keep_each_memory_with_its_own_counters(pg_folds) -> None:
    """Namespaces long enough that no two keys fit one parameter: every UPDATE pairs
    its keys with their own counters, and the Postgres heads take the same rows."""
    long = ["n" * 40_000 + str(i) for i in range(3)]
    targets = [
        usage_fold._Target(
            memory_id=MID,
            org="o",
            namespace=ns,
            agent="a",
            user_id="-",
            memory_key="k",
            evidence=10 + i,
            recall=i,
            last_recalled=T0 + timedelta(hours=i),
            reinforcement=None,
            last_activity=T0,
        )
        for i, ns in enumerate(long)
    ]
    ch = StubCH()
    assert await usage_fold._fold_memory_rows(ch, targets, 500) == 3
    mem = _updates(ch, "loom.memory")
    assert [p["keys"] for _, p in mem] == [[("o", ns, MID)] for ns in long]
    hours = [T0 + timedelta(hours=i) for i in range(3)]
    assert [(p["evs"], p["rcls"], p["lras"]) for _, p in mem] == [
        ([10 + i], [i], [hours[i]]) for i in range(3)
    ]
    assert pg_folds == [[("o", ns, MID, 10 + i, i, hours[i])] for i, ns in enumerate(long)]


async def test_the_identity_read_is_cut_by_size_too() -> None:
    """Namespaces long enough that no two fit one parameter: the identity read takes
    them in chunks, each parameter within one field, and every memory still folds."""
    long = ["n" * 40_000 + str(i) for i in range(3)]
    ch = StubCH()
    _seed_active_pass(
        ch,
        totals=[_totals_row(namespace=ns) for ns in long],
        idents=[_ident_row(namespace=long[0])],
    )
    ch.query_all_returns += [[_ident_row(namespace=ns)] for ns in long[1:]]
    result = await run_pass(ch=ch)
    assert result.folded == 3
    reads = [p for sql, p in ch.query_all_calls if sql == usage_fold.IDENTITY_STATEMENT]
    assert [p["keys"] for p in reads] == [[("clickhouse", ns, MID)] for ns in long]
    for p in reads:
        for value in p.values():
            assert len(format_bind_value(value, top_level=False).encode()) <= ch_params.FIELD_BYTES
