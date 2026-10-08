"""The usage fold's reinforcement lives in
loom.memory_reinforcement and the fact leg reads it after its fetch.

The builder mirrors every column of the table (the RMT rule), the
Python boost is the SQL rank term, the post-fetch fold re-scores only the
rows that have a fold row and only within the fetched shortlist, and the
leg fails open when the table cannot be read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.core.vectors.quantized_setting import set_quantized_codes_supported
from server.memory.app import reinforcement as R
from server.memory.app.retrieval import vector_search
from tests.unit.schema_ddl import column_names
from tests.unit.test_memory_search_service import StubCH, _q


def _ddl_columns() -> list[str]:
    return column_names("memory_reinforcement", materialized=False)


def test_builder_mirrors_every_column_of_the_table() -> None:
    assert set(R.REINFORCEMENT_PERSISTED_COLS) == set(_ddl_columns())
    row = R.build_reinforcement_row(
        org="o", namespace="n", agent="a", memory_key="k", reinforcement_count=5
    )
    assert set(row) == set(_ddl_columns())
    assert row["user_id"] == "-" and row["reinforcement_count"] == 5
    assert (
        isinstance(row["last_reinforced_at"], datetime) and row["last_reinforced_at"].tzinfo is None
    )
    assert row["version"] > 0
    with pytest.raises(ValueError):
        R.build_reinforcement_row(memory_key="k", reinforcment_count=1)  # typo'd column


def test_lookup_statement_is_a_keyed_group_by_over_one_namespace() -> None:
    sql = R.reinforcement_lookup_sql()
    assert f"FROM {R.TABLE}" in sql
    for fragment in (
        "org = {org:String}",
        "namespace = {ns:String}",
        "memory_key IN {keys:Array(String)}",
        "argMax(reinforcement_count, version)",
        "argMax(last_reinforced_at, version)",
        "GROUP BY agent, user_id, memory_key",
    ):
        assert fragment in sql
    assert "FINAL" not in sql  # argMax by version is the collapse


@pytest.mark.asyncio
async def test_fetch_dedups_keys_and_maps_by_fold_identity() -> None:
    ch = StubCH()
    t = datetime(2026, 9, 1, 12, 0, 0)
    ch.query_all_returns.append(
        [
            {
                "agent": "bot",
                "user_id": "-",
                "memory_key": "k",
                "reinforcement_count": 9,
                "last_reinforced_at": t,
            },
            {
                "agent": "bot",
                "user_id": "-",
                "memory_key": "z",
                "reinforcement_count": 2,
                "last_reinforced_at": "not a datetime",
            },
        ]
    )
    out = await R.fetch_reinforcement(ch, org="o", namespace="n", memory_keys=["k", "k", "", "z"])
    assert out == {("bot", "-", "k"): (9, t)}
    _, params = ch.query_all_calls[0]
    assert params == {"org": "o", "ns": "n", "keys": ["k", "z"]}
    assert await R.fetch_reinforcement(ch, org="o", namespace="n", memory_keys=[]) == {}


def _row(
    key: str, *, base: int = 1, rank: float = 0.8, sql_boost: float = 0.0, agent: str = "bot"
) -> dict[str, Any]:
    return {
        "memory_key": key,
        "agent": agent,
        "user_id": "-",
        "rank_score": rank,
        "reinforcement_sql": sql_boost,
        "reinforcement_base": base,
        "last_reinforced_base": datetime(2026, 9, 1),
        "created_at": "2026-08-01 00:00:00",
    }


def test_apply_takes_the_greater_of_the_fold_and_the_write_time_base() -> None:
    now = datetime(2026, 9, 14)
    rows = [_row("a", base=1), _row("b", base=1), _row("c", base=50, sql_boost=0.2)]
    folded = {
        ("bot", "-", "a"): (6, datetime(2026, 9, 10)),
        ("bot", "-", "c"): (3, datetime(2026, 8, 1)),  # below the write-time base: base wins
        ("other", "-", "b"): (9, now),  # a different agent's row: not this one
    }
    n = R.apply_folded_reinforcement(rows, folded, weight=0.15, half_life_days=90.0, now=now)
    # c (count 50) leads, a (count 6) next, b (count 1, never reinforced) is the pool's zero
    assert (
        rows[2]["reinforcement_sql"] > rows[0]["reinforcement_sql"] > rows[1]["reinforcement_sql"]
    )
    assert abs(sum(r["reinforcement_sql"] for r in rows)) < 1e-9
    assert rows[2]["rank_score"] == pytest.approx(0.8 - 0.2 + rows[2]["reinforcement_sql"])
    assert n == 3
    assert R.apply_folded_reinforcement(rows, folded, weight=0.0, half_life_days=90.0) == 0


def _fact_hit(key: str, rank: float) -> dict[str, Any]:
    return {
        "memory_key": key,
        "memory_type": "preference",
        "content_excerpt": key,
        "distance": 0.2,
        "rank_score": rank,
        "agent": "bot",
        "user_id": "-",
        "reinforcement_sql": 0.0,
        "reinforcement_base": 1,
        "last_reinforced_base": datetime(2026, 9, 1),
        "created_at": "2026-08-01 00:00:00",
    }


@pytest.fixture(autouse=True)
def _codes_probe_off():
    set_quantized_codes_supported(False)
    yield
    set_quantized_codes_supported(None)


@pytest.mark.asyncio
async def test_fact_leg_selects_the_fold_identity_and_reads_the_side_table_after_the_fetch() -> (
    None
):
    ch = StubCH()
    ch.query_all_returns.append([_fact_hit("a", 0.8), _fact_hit("b", 0.7)])
    ch.query_all_returns.append(
        [  # the keyed read: b was folded to 6
            {
                "agent": "bot",
                "user_id": "-",
                "memory_key": "b",
                "reinforcement_count": 6,
                "last_reinforced_at": datetime.now(UTC).replace(tzinfo=None),
            },
        ]
    )
    out = await vector_search(
        _q(reinforcement_weight=0.15, execution_planes=["fact"], key_axis=False), ch=ch
    )
    fact_sql, _ = ch.query_all_calls[0]
    for col in (
        "e.agent AS agent",
        "e.user_id AS user_id",
        "e.reinforcement_count AS reinforcement_base",
        "e.last_reinforced_at AS last_reinforced_base",
    ):
        assert col in fact_sql
    side_sql, side_params = ch.query_all_calls[1]
    assert f"FROM {R.TABLE}" in side_sql and side_params["keys"] == ["a", "b"]
    by_key = {h.memory_key: h for h in out}
    # pool_centred (the default): the folded row gains, the unfolded
    # row loses the same amount — the pool's net lift is zero.
    assert by_key["b"].rank_score > 0.7 and by_key["a"].rank_score < 0.8
    assert by_key["b"].score_components["reinforcement"] > 0.0
    assert by_key["a"].score_components["reinforcement"] < 0.0
    assert (
        abs(
            by_key["a"].score_components["reinforcement"]
            + by_key["b"].score_components["reinforcement"]
        )
        < 1e-9
    )


@pytest.mark.asyncio
async def test_fact_leg_skips_the_side_table_when_the_boost_is_off() -> None:
    ch = StubCH()
    ch.query_all_returns.append([_fact_hit("a", 0.8)])
    await vector_search(
        _q(reinforcement_weight=0.0, execution_planes=["fact"], key_axis=False), ch=ch
    )
    assert not any(R.TABLE in sql for sql, _ in ch.query_all_calls)


@pytest.mark.asyncio
async def test_fact_leg_fails_open_when_the_side_table_cannot_be_read() -> None:
    """A replica that cannot serve the side-table read keeps the SQL's write-time base."""

    class _Failing(StubCH):
        async def query_all(self, sql, params=None):
            if R.TABLE in sql:
                raise RuntimeError("Code: 159. DB::Exception: Timeout exceeded")
            return await super().query_all(sql, params)

    ch = _Failing()
    ch.query_all_returns.append([_fact_hit("a", 0.8)])
    out = await vector_search(
        _q(reinforcement_weight=0.15, execution_planes=["fact"], key_axis=False), ch=ch
    )
    assert [h.rank_score for h in out] == [0.8]
