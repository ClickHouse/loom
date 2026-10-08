"""A worker restart reruns only the graph-stats work that is owed.

Two costs pinned here. A namespace whose test-map leg ran and found no
tests to map publishes that (`test_map_summary`: rows 0, stamp, the commit
it ran at), so its empty test map is not due again until the interval
passes or a new commit lands; a namespace whose leg never ran stays due.
And `refresh_stats_summaries` counts only the tables the leg wrote to: a
leg that wrote no row reuses the published summary instead of a FINAL
count.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.code.store.index import CodeRepo, summary_stamp
from server.code.workers import graph_stats as cgs

WEEK = 7 * 24 * 3600
NOW = datetime.now(UTC).replace(microsecond=0)
HEAD = "a" * 40


def _state(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "files": True,
        "ranks": True,
        "test_map": False,
        "symbol_stats_rows": 10,
        "symbol_stats_at": NOW - timedelta(hours=1),
        "test_map_ran_at": NOW - timedelta(hours=1),
        "test_map_ran_commit": HEAD,
        "git_head": HEAD,
    }
    base.update(over)
    return base


def _due(state: dict[str, Any]) -> str:
    return cgs.pass_due(state, interval_seconds=WEEK, now=NOW)


# ---- the pure rule -----------------------------------------------------------


def test_an_empty_test_map_the_leg_found_at_this_commit_is_not_due() -> None:
    assert _due(_state()) == ""
    # a naive stamp is UTC
    assert _due(_state(test_map_ran_at=(NOW - timedelta(days=2)).replace(tzinfo=None))) == ""


def test_an_empty_test_map_the_leg_never_built_is_due() -> None:
    assert _due(_state(test_map_ran_at=None, test_map_ran_commit="")) == "test_map_empty"


def test_a_new_commit_or_the_interval_makes_the_empty_test_map_due_again() -> None:
    assert _due(_state(git_head="b" * 40)) == "test_map_empty"
    assert _due(_state(test_map_ran_at=NOW - timedelta(days=8))) == "test_map_empty"


# ---- the pass over a repo that keeps its state rows ----------------------------


class _CH:
    """`code_index_state` in a dict; the existence reads answer by table."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, str], dict[str, str]] = {}
        self.sql: list[str] = []

    async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        assert table == "loom.code_index_state"
        for r in rows:
            self.state.setdefault((r["org"], r["namespace"]), {})[r["state_key"]] = r["state_value"]

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.sql.append(sql)
        assert "code_index_state" in sql
        rows = self.state.get((params["org"], params["ns"]), {})
        keys = params.get("keys") or list(rows)
        return [{"state_key": k, "value": v} for k, v in rows.items() if k in keys]

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        self.sql.append(sql)
        if "code_index_state" in sql:  # get_state
            v = self.state.get((params["org"], params["ns"]), {}).get(params["key"])
            return None if v is None else {"state_value": v}
        if "code_test_map" in sql:
            return None  # the leg writes none: nothing to map
        if "code_symbol_stats" in sql:
            return {"newest_at": NOW - timedelta(hours=1)}
        return {"present": 1}


class _Repo(CodeRepo):
    """The real state reads and publishes over `_CH`; the leg's inputs stubbed."""

    def __init__(self) -> None:
        self.fake = _CH()
        super().__init__(self.fake)  # type: ignore[arg-type]
        self.counts: list[str] = []
        self.inserted: list[str] = []

    async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
        return {"packages/web/index.ts": "h", "packages/web/util.ts": "h"}

    async def include_edges(self, org: str, ns: str) -> list[tuple[str, str]]:
        return []

    async def pr_file_pairs(self, org: str, ns: str) -> list[dict[str, Any]]:
        return []

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append(table)

    async def symbol_stats_summary(self, org: str, ns: str) -> tuple[int, datetime | None]:
        self.counts.append("symbol_stats")
        return 10, NOW - timedelta(hours=1)

    async def unit_risk_summary(self, org: str, ns: str) -> tuple[int, datetime | None]:
        self.counts.append("unit_risk")
        return 7, NOW - timedelta(hours=2)


class _Cfg:
    def __init__(self, ns: str) -> None:
        self.org, self.namespace, self.label = "o", ns, f"o/{ns}"


class _Embedder:
    model_name = "m"


def _wire(monkeypatch: pytest.MonkeyPatch, repo: _Repo, namespaces: list[str]) -> None:
    import server.code.domain.index.repos as repos_mod
    import server.code.store.index as cr
    import server.embedding.embedder as emb

    monkeypatch.setattr(cr, "CodeRepo", lambda: repo)
    monkeypatch.setattr(emb, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(repos_mod, "configured_repos", lambda: [_Cfg(ns) for ns in namespaces])
    monkeypatch.setattr(cgs, "_utcnow", lambda: NOW)

    async def _no_stats(*_a: Any, **_k: Any) -> None:
        return None

    # the symbol-stats aggregation is not this file's subject
    monkeypatch.setattr(cgs, "_write_symbol_stats", _no_stats)


def _set(repo: _Repo, ns: str, key: str, value: str) -> None:
    repo.fake.state.setdefault(("o", ns), {})[key] = value


async def test_a_leg_that_found_no_tests_publishes_it_and_the_next_check_is_not_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _wire(monkeypatch, repo, ["code-ts"])
    _set(repo, "code-ts", "git_head", HEAD)
    state = await repo.graph_stats_state("o", "code-ts")
    assert _due(state) == "test_map_empty"  # never ran: due
    await cgs._run_repo("o", "code-ts", cgs.CodeGraphStatsResult())
    assert "code_test_map" not in repo.inserted
    published = json.loads(repo.fake.state[("o", "code-ts")]["test_map_summary"])
    assert published == {"n": 0, "at": summary_stamp(NOW), "commit": HEAD}
    state = await repo.graph_stats_state("o", "code-ts")
    assert state["test_map"] is False and state["test_map_ran_commit"] == HEAD
    assert _due(state) == ""
    # a new commit ingested: the leg is owed again
    _set(repo, "code-ts", "git_head", "b" * 40)
    assert _due(await repo.graph_stats_state("o", "code-ts")) == "test_map_empty"


async def test_a_published_summary_with_rows_does_not_excuse_an_empty_test_map() -> None:
    repo = _Repo()
    _set(repo, "n", "git_head", HEAD)
    _set(
        repo,
        "n",
        "test_map_summary",
        json.dumps({"n": 40, "at": summary_stamp(NOW), "commit": HEAD}),
    )
    # the rows it counted are gone (tombstoned since): due
    assert _due(await repo.graph_stats_state("o", "n")) == "test_map_empty"
    _set(repo, "n", "test_map_summary", "not json")
    assert _due(await repo.graph_stats_state("o", "n")) == "test_map_empty"


async def test_a_restart_after_the_pass_finds_the_empty_by_design_namespace_not_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _wire(monkeypatch, repo, ["code-ts", "code-new"])
    _set(repo, "code-ts", "git_head", HEAD)
    _set(repo, "code-ts", "symbol_stats_summary", json.dumps({"n": 10, "at": summary_stamp(NOW)}))
    assert set(await cgs.due_namespaces()) == {"o/code-ts", "o/code-new"}
    await cgs._run_repo("o", "code-ts", cgs.CodeGraphStatsResult())
    # the boot after: only the namespace whose leg never ran is owed it
    assert await cgs.due_namespaces() == {"o/code-new": "test_map_empty"}


# ---- summaries: no count for a table the leg did not write -------------------------


async def test_refresh_reuses_the_published_row_of_a_table_the_leg_wrote_nothing_to() -> None:
    repo = _Repo()
    _set(repo, "n", "unit_risk_summary", json.dumps({"n": 389_915, "at": "x"}))
    _set(repo, "n", "symbol_stats_summary", json.dumps({"n": 10, "at": "y"}))
    everything = frozenset({"unit_risk_summary", "symbol_stats_summary"})
    out = await repo.refresh_stats_summaries("o", "n", unchanged=everything)
    assert repo.counts == []
    assert out == {"unit_risk_summary": (389_915, "x"), "symbol_stats_summary": (10, "y")}
    # a table the leg wrote to is counted and published
    out = await repo.refresh_stats_summaries(
        "o", "n", unchanged=frozenset({"symbol_stats_summary"})
    )
    assert repo.counts == ["unit_risk"]
    assert out["unit_risk_summary"] == (7, summary_stamp(NOW - timedelta(hours=2)))
    # nothing published yet: counted even when unchanged, so status has a row
    fresh = _Repo()
    await fresh.refresh_stats_summaries("o", "n", unchanged=everything)
    assert fresh.counts == ["symbol_stats", "unit_risk"]


@pytest.mark.parametrize(("written", "counted"), [(0, []), (12, ["unit_risk"])])
async def test_the_unit_risk_leg_counts_its_table_only_when_it_wrote_rows(
    monkeypatch: pytest.MonkeyPatch, written: int, counted: list[str]
) -> None:
    repo = _Repo()
    _wire(monkeypatch, repo, ["n"])
    _set(repo, "n", "unit_risk_summary", json.dumps({"n": 5, "at": "x"}))
    _set(repo, "n", "symbol_stats_summary", json.dumps({"n": 10, "at": "y"}))

    async def _write(_repo: Any, _org: str, _ns: str, _model: str, result: Any, **_k: Any) -> None:
        result.unit_risk_rows += written

    monkeypatch.setattr(cgs, "_write_unit_risk", _write)
    await cgs._run_unit_risk("o", "n", cgs.CodeGraphStatsResult(), deadline=0.0)
    assert repo.counts == counted  # the stats table is never recounted by this leg


async def test_the_cheap_legs_never_recount_the_risk_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo()
    _wire(monkeypatch, repo, ["n"])
    _set(repo, "n", "unit_risk_summary", json.dumps({"n": 5, "at": "x"}))
    await cgs._run_repo("o", "n", cgs.CodeGraphStatsResult())
    assert repo.counts == ["symbol_stats"]
