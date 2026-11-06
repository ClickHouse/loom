from datetime import UTC, datetime, timedelta

from server.memory.app.health import footprint, summarise

_NOW = datetime.now(UTC).replace(tzinfo=None)


def _r(key, t="semantic", rc=0, uc=0, age=0, last=None, hc=0):
    return {
        "memory_key": key,
        "memory_type": t,
        "recall_count": rc,
        "used_count": uc,
        "harm_count": hc,
        "created_at": _NOW - timedelta(days=age),
        "last_recalled_at": last,
    }


def test_summarise_counts_silent_by_type_and_ranks_top() -> None:
    rows = [
        _r("a", rc=5, uc=2, age=40, last=_NOW),
        _r("b", rc=1, age=2),
        _r("c", age=45),
        _r("d", age=3),
        _r("e", t="preference", age=90),
    ]
    out = summarise(rows, silent_after_days=30, top_k=1)
    assert (out["live"], out["recalled_ever"], out["used_ever"]) == (5, 2, 1)
    assert out["silent"] == 3 and out["silent_older"] == 2 and out["silent_share"] == 0.6
    assert out["by_type"][0] == {"memory_type": "semantic", "live": 4, "recalled": 2}
    assert out["by_type"][1] == {"memory_type": "preference", "live": 1, "recalled": 0}
    assert [t["memory_key"] for t in out["top_recalled"]] == ["a"]
    assert out["top_recalled"][0]["used_count"] == 2


def test_summarise_counts_harmed_rows_and_carries_harm_on_top_rows() -> None:
    """A row relied on in a failed run shows up as harmed —
    visible while the harm weight is still 0."""
    rows = [_r("a", rc=5, uc=2, hc=1, last=_NOW), _r("b", rc=1), _r("c", hc=2)]
    out = summarise(rows, silent_after_days=30, top_k=2)
    assert out["harmed_ever"] == 2
    assert out["top_recalled"][0]["harm_count"] == 1
    assert out["top_recalled"][1]["harm_count"] == 0


def test_summarise_empty_namespace() -> None:
    out = summarise([], silent_after_days=30, top_k=5)
    assert out["live"] == 0 and out["silent_share"] == 0.0 and out["top_recalled"] == []


def test_memory_health_is_registered_read_only() -> None:
    from server.operations.base import Role
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    op = OPERATIONS_BY_NAME["memory.health"]
    assert op.required_role == Role.VIEWER and op.idempotent
    assert "memory.health" in TOOL_DESCRIPTIONS


def test_staleness_distribution_buckets_ages_and_counts_validity_expiry_pinned() -> None:
    """A staleness distribution, not just a silent count: how many active
    rows carry a validity end becomes visible here."""
    from server.memory.app.health import staleness

    rows = [
        {**_r("a", age=1), "valid_to": _NOW + timedelta(days=3)},
        {**_r("b", age=10), "invalid_at": _NOW - timedelta(days=1), "pinned": 1},
        {**_r("c", age=45), "expires_at": _NOW + timedelta(days=2)},
        {
            **_r("d", age=200),
            "expires_at": _NOW + timedelta(days=60),
            "last_reinforced_at": _NOW - timedelta(days=100),
        },
        {**_r("e", age=7)},
    ]
    out = staleness(rows)
    assert out["by_age"] == {"lt7d": 1, "d7_30": 2, "d30_90": 1, "gt90d": 1}
    assert out["with_validity_end"] == 2 and out["with_validity_end_share"] == 0.4
    assert out["with_expiry"] == 2 and out["expiring_7d"] == 1
    assert out["pinned"] == 1
    assert out["days_since_reinforced_p50"] == 100  # the only row with a reinforcement stamp


def test_staleness_of_empty_namespace_is_all_zero() -> None:
    from server.memory.app.health import staleness

    out = staleness([])
    assert out["by_age"] == {"lt7d": 0, "d7_30": 0, "d30_90": 0, "gt90d": 0}
    assert out["with_validity_end_share"] == 0.0 and out["days_since_reinforced_p50"] == 0


def test_summarise_carries_the_staleness_section() -> None:
    out = summarise([_r("a", age=40)], silent_after_days=30, top_k=1)
    assert out["staleness"]["by_age"]["d30_90"] == 1
    from server.memory.api.models_admin import MemoryHealthOutput

    MemoryHealthOutput(**out)  # the wire model accepts the section


def test_footprint_sums_value_chars_and_estimates_tokens() -> None:
    """The report carries tokens stored as chars/4 over the live rows, so a bench
    reads it from one health call instead of paging memory.list per identity."""
    rows = [dict(_r("a"), value_chars=400), dict(_r("b"), value_chars=41), _r("c")]
    fp = footprint(rows)
    assert fp == {"value_chars": 441, "value_tokens_est": 110, "tokens_per_row_est": 36.8}
    assert footprint([]) == {"value_chars": 0, "value_tokens_est": 0, "tokens_per_row_est": 0.0}
    assert summarise(rows, silent_after_days=30, top_k=1)["footprint"] == fp


def test_a_key_served_to_an_agent_is_not_silent_on_any_of_its_copies() -> None:
    """A context pack or ask writes `served`, not a recall credit, and an agent read puts its
    event on the newest copy of the key: every live copy of that user's key counts as read."""
    rows = [
        {**_r("k", age=40), "served_count": 3},  # the newest copy, served by a context pack
        _r("k", age=40),  # another session's copy of the same key, no event of its own
        _r("credited", rc=1, age=40),
        _r("never", age=40),
        # another user's row of the same key: its own reads, none here
        {**_r("k", age=40), "user_id": "u2"},
    ]
    out = summarise(rows, silent_after_days=30, top_k=5)
    assert out["recalled_ever"] == 3
    assert (out["silent"], out["silent_older"]) == (2, 2)
    assert out["by_type"][0] == {"memory_type": "semantic", "live": 5, "recalled": 3}
    # the ranking list stays the credited rows
    assert [t["memory_key"] for t in out["top_recalled"]] == ["credited"]


def test_the_served_read_is_bounded_to_the_silent_window(monkeypatch) -> None:
    """A context pack writes a `served` event per key it returns, every turn; the health
    read takes only the last `silent_after_days` days of them."""
    import asyncio

    from server.memory.api.models_admin import MemoryHealthInput
    from server.memory.app import health

    seen: dict = {}

    class _CH:
        async def query_all(self, sql, params=None):
            seen["sql"], seen["params"] = sql, params
            return []

    monkeypatch.setattr(health, "get_client", lambda: _CH())
    asyncio.run(
        health.handle_memory_health(MemoryHealthInput(org="o", namespace="n", silent_after_days=14))
    )
    served = seen["sql"][seen["sql"].index("event_type = 'served'") :]
    assert "ts > now() - toIntervalDay({days:UInt32})" in served
    assert seen["params"] == {"org": "o", "ns": "n", "days": 14}
