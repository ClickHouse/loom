"""A table a refreshable view replaces whole (`REFRESH ... TO`, no APPEND) is
never mutated: the refresh exchanges the table under a running mutation, so an
ALTER DELETE or UPDATE on it never finishes. The erasure purge of a
repository index therefore reads the code tables from `system.tables` and
leaves out every such target by the regex in `server/code/app/index/retire.py`.
An APPEND target is an ordinary table and is purged like one.

These tests run that regex, as ClickHouse receives it, over the dump's view
DDL: each non-APPEND target is a `code_*` table (the family the purge
filter covers), the filter excludes it, and no other erasure step or census
row names it.
"""

from __future__ import annotations

import re

from server.code.app.index import retire
from server.erasure.store import census
from tests.unit.schema_ddl import ddl, tables, views

# Every refreshable view in the dump: target table and whether it appends.
REFRESH_VIEWS = {
    "agent_cost_rollup_refresh": ("agent_cost_rollup_daily", True),
    "code_term_df_refresh": ("code_term_df", False),
    "memory_doc_stats_refresh": ("memory_doc_stats", True),
    "memory_exposure_breadth_hourly_refresh": ("memory_exposure_breadth_hourly", True),
    "memory_term_freq_refresh": ("memory_term_freq", True),
    "subject_last_seen_refresh": ("subject_last_seen", True),
}

_HEAD = re.compile(r"^REFRESH .*?\b(APPEND )?TO loom\.(\w+)$", re.M)


def _sql_regex(pattern: str) -> re.Pattern[str]:
    """The pattern after ClickHouse unescapes the string literal it sits in."""
    return re.compile(pattern.replace("\\\\", "\\"))


def _refresh_views() -> dict[str, tuple[str, bool]]:
    """`{view: (target table, APPEND)}` for every refreshable view in the dump."""
    out: dict[str, tuple[str, bool]] = {}
    for view in views():
        m = _HEAD.search(ddl(view).split("\n(", 1)[0])
        if m:
            out[view] = (m.group(2), bool(m.group(1)))
    return out


def _excluded_by_the_purge(view: str) -> str | None:
    """The table retire.py's filter leaves out for this view's DDL, or None."""
    query = ddl(view)
    target = _sql_regex(retire._REFRESH_TARGET).search(query)
    if target is None or _sql_regex(retire._REFRESH_APPEND).search(query):
        return None
    return target.group(1)


def test_the_refresh_views_are_the_ones_listed() -> None:
    assert _refresh_views() == REFRESH_VIEWS
    assert all(target in tables() for target, _a in REFRESH_VIEWS.values())


def test_a_replaced_target_is_a_code_table_the_purge_leaves_out() -> None:
    for view, (target, append) in _refresh_views().items():
        excluded = _excluded_by_the_purge(view)
        if append:
            assert excluded is None, (view, "an APPEND target is purged like any table")
        else:
            assert target.startswith("code_"), (view, target)
            assert excluded == target, (view, target, excluded)


def test_no_erasure_step_mutates_a_replaced_target() -> None:
    replaced = {f"loom.{t}" for t, append in _refresh_views().values() if not append}
    assert replaced
    assert not replaced & {sub.table for sub in census._CENSUS}
    for step, step_tables in census.STEP_ONLY_TABLES.items():
        if step != "code_index.purge":  # filters replaced targets out at run time
            assert not replaced & set(step_tables), step
