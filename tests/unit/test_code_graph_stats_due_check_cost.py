"""The graph-stats due check reads what decides "due", and nothing else.

`graph_stats_state` runs for every configured namespace at each supervisor
start and again at the top of each pass. It answers from three existence
reads and the summaries the pass published as `code_index_state` rows: no
FINAL count over `code_unit_risk` (operator progress, not a due condition)
or `code_symbol_stats`. A namespace with no published summary yet reads the
stats table without FINAL, so a first pass on an empty table is still due,
and a namespace whose cheap legs finished (and published) is not owed them
again: a pass cut short restarts nothing it already did.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.store.index import CodeRepo, summary_stamp
from server.code.workers import graph_stats as cgs

WEEK = 7 * 24 * 3600
NOW = datetime.now(UTC).replace(microsecond=0)


class _CH:
    """A ClickHouse stand-in holding `code_index_state` and `code_symbol_stats`
    rows per namespace; every statement is kept for the assertions."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.state: dict[tuple[str, str], dict[str, str]] = {}
        self.stats: dict[str, list[datetime]] = {}  # ns -> computed_at of rows with callers

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert table == "loom.code_index_state"
        for r in rows:
            self.state.setdefault((r["org"], r["namespace"]), {})[r["state_key"]] = r["state_value"]

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.sql.append(sql)
        assert "code_index_state" in sql
        rows = self.state.get((params["org"], params["ns"]), {})
        return [{"state_key": k, "value": v} for k, v in rows.items()]

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.sql.append(sql)
        stamps = self.stats.get(params["ns"], [])
        if "count() AS n" in sql and "code_symbol_stats" in sql:  # the pass's own publish
            return {"n": len(stamps), "newest_at": max(stamps) if stamps else None}
        if "code_unit_risk" in sql:
            return {"n": 0, "newest_at": None}
        if "code_symbol_stats" in sql:
            return {"newest_at": max(stamps)} if stamps else None
        return {"present": 1}  # files, ranks, test map


def _final_counts(sql: list[str]) -> list[str]:
    """Statements that read the two stats tables under FINAL."""
    return [s for s in sql if re.search(r"FROM loom\.code_(unit_risk|symbol_stats) FINAL", s)]


def _due(state: dict[str, Any]) -> str:
    return cgs.pass_due(state, interval_seconds=WEEK, now=NOW)


def _publish(ch: _CH, ns: str, key: str, n: int, at: datetime | None) -> None:
    ch.state.setdefault(("o", ns), {})[key] = json.dumps({"n": n, "at": summary_stamp(at)})


async def test_the_due_check_issues_no_final_count_and_never_reads_the_risk_table() -> None:
    ch = _CH()
    _publish(ch, "n", "symbol_stats_summary", 97_508, NOW - timedelta(hours=1))
    _publish(ch, "n", "unit_risk_summary", 389_915, NOW - timedelta(hours=2))
    state = await CodeRepo(ch).graph_stats_state("o", "n")  # type: ignore[arg-type]
    assert _final_counts(ch.sql) == []
    assert not any("code_unit_risk" in s or "code_symbol_stats" in s for s in ch.sql)
    assert state["symbol_stats_rows"] == 97_508
    assert state["symbol_stats_at"] == NOW - timedelta(hours=1)
    # operator fields: the published row, not a recount
    assert (state["unit_risk_rows"], state["unit_risk_at"]) == (389_915, NOW - timedelta(hours=2))
    assert _due(state) == ""


async def test_an_empty_table_with_nothing_published_is_still_the_first_pass() -> None:
    ch = _CH()
    state = await CodeRepo(ch).graph_stats_state("o", "n")  # type: ignore[arg-type]
    assert _due(state) == "symbol_stats_empty"
    assert (state["unit_risk_rows"], state["unit_risk_at"]) == (0, None)
    # the fallback read is plain WHERE, and the risk table is not read at all
    assert _final_counts(ch.sql) == []
    assert [s for s in ch.sql if "code_symbol_stats" in s]
    assert not any("code_unit_risk" in s for s in ch.sql)


async def test_rows_without_a_published_summary_are_judged_by_their_newest_stamp() -> None:
    ch = _CH()
    ch.stats["fresh"] = [NOW - timedelta(days=9), NOW - timedelta(days=1)]
    ch.stats["stale"] = [NOW - timedelta(days=9)]
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    assert _due(await repo.graph_stats_state("o", "fresh")) == ""
    assert _due(await repo.graph_stats_state("o", "stale")) == "symbol_stats_stale"
    assert _final_counts(ch.sql) == []


async def test_a_published_summary_decides_empty_and_stale_like_the_count_did() -> None:
    ch = _CH()
    _publish(ch, "empty", "symbol_stats_summary", 0, None)
    _publish(ch, "stale", "symbol_stats_summary", 5, NOW - timedelta(days=8))
    ch.state[("o", "junk")] = {"symbol_stats_summary": "not json"}  # reads as unpublished
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    assert _due(await repo.graph_stats_state("o", "empty")) == "symbol_stats_empty"
    assert _due(await repo.graph_stats_state("o", "stale")) == "symbol_stats_stale"
    assert _due(await repo.graph_stats_state("o", "junk")) == "symbol_stats_empty"
    assert _final_counts(ch.sql) == []


class _Cfg:
    def __init__(self, ns: str) -> None:
        self.org, self.namespace, self.label = "o", ns, f"o/{ns}"


async def test_a_namespace_whose_cheap_legs_published_is_not_owed_them_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.domain.index.repos as repos_mod
    import server.code.store.index as cr

    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    monkeypatch.setattr(repos_mod, "configured_repos", lambda: [_Cfg("code-a"), _Cfg("code-b")])
    monkeypatch.setattr(cgs, "_utcnow", lambda: NOW)
    assert await cgs.due_namespaces() == {
        "o/code-a": "symbol_stats_empty",
        "o/code-b": "symbol_stats_empty",
    }
    # a pass cut after code-a's cheap legs: its stats rows and the summary
    # the leg publishes (the pass's own count, once) are what the next check sees
    ch.stats["code-a"] = [NOW - timedelta(minutes=5)]
    await repo.refresh_stats_summaries("o", "code-a")
    ch.sql.clear()
    assert await cgs.first_pass_due() is True
    assert await cgs.due_namespaces() == {"o/code-b": "symbol_stats_empty"}
    assert _final_counts(ch.sql) == []
