"""Bridge completion's repository read."""

from __future__ import annotations

from server.memory.store.lookups import _KEY_NORM_SQL, normalize_key_prefix


def test_connectives_fold_on_both_sides() -> None:
    assert normalize_key_prefix("user_serve_and_connect") == "serve_connect"
    assert normalize_key_prefix("bank_of_the_west") == "bank_west"
    assert normalize_key_prefix("success_gala") == "success_gala"
    assert normalize_key_prefix("nolan") == "nolan"
    # a leading `user_` names the speaker, not the entity (`user_italy_capital`)
    assert normalize_key_prefix("user_italy_capital") == "italy_capital"
    assert normalize_key_prefix("user_mom") == "mom"
    assert "^(user|the)_" in _KEY_NORM_SQL
    # the SQL side folds the same connectives, without lookaround (RE2)
    assert "(?=" not in _KEY_NORM_SQL and "_(and|the|of|for|at|in|a|an)_" in _KEY_NORM_SQL


def test_rows_are_shared_out_per_prefix() -> None:
    """Each prefix gets its own share of the rows: one alphabetical cap over
    twelve prefixes would starve the entities late in the alphabet."""
    import asyncio

    from server.memory import store as memory_repo

    seen: dict = {}

    class _CH:
        async def query_all(self, sql, params=None):
            seen["sql"] = sql
            seen.update(params or {})
            return []

    asyncio.run(
        memory_repo.find_by_key_prefix(
            org="o",
            namespace="ns",
            prefixes=["user_launchfest", "user_run_for_good"],
            limit=48,
            per_prefix=6,
            ch=_CH(),
        )
    )
    assert (
        "LIMIT {per:UInt32} BY multiIf(" in seen["sql"] and seen["per"] == 6 and seen["lim"] == 48
    )
