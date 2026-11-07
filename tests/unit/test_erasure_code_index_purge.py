"""A namespace-scope erasure of a code namespace purges the
repository index. The step reads the code tables from the live schema and
runs them through the saga's count-then-mutate (DELETE in every mode,
lightweight on the two content tables, awaited by tag); the two content
tables are census rows the step owns, the rest are its step-only tables, and
the static tuple is held to the schema. CH-free."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from server.code.app.index import retire
from server.erasure.app import saga_steps
from server.erasure.store import census as erasure_census
from server.erasure.store import mutations as erasure_mutations
from server.erasure.store import steps_memory as memory_steps

ROOT = Path(__file__).resolve().parents[2]
_CREATE = re.compile(
    r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?`?loom`?\.`?(code_\w+)`?[^\n]*\n(.*?)\n\)", re.S
)


class _StubCH:
    """system.columns answers the table scan; count() answers `count`;
    every mutation is recorded; system.mutations reports done at once."""

    def __init__(self, tables: list[str], count: int = 1) -> None:
        self.tables = tables
        self.count = count
        self.counts: list[tuple[str, dict[str, Any]]] = []
        self.commands: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        if "system.columns" in sql:
            return [{"table": t, "cols": ["org", "namespace", "x"]} for t in self.tables]
        return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    async def query_one(self, sql, params=None):
        self.counts.append((sql, params or {}))
        return {"n": self.count}

    async def execute(self, sql, params=None):
        self.commands.append((sql, params or {}))


def _purge(org: str, namespace: Any, *, ch: Any) -> Any:
    return memory_steps.purge_code_index_by_namespace(
        org,
        namespace,
        code_tables=retire.code_tables_keyed_by_namespace,
        refresh_views=retire.refresh_code_views,
        ch=ch,
    )


def _schema_code_tables_keyed_by_namespace() -> set[str]:
    text = (ROOT / "schemas" / "schema.sql").read_text()
    out: set[str] = set()
    for name, body in _CREATE.findall(text):
        cols = set(re.findall(r"^\s*`(\w+)`", body, re.M))
        if {"org", "namespace"} <= cols:
            out.add(f"loom.{name}")
    return out - retire.KEEP_TABLES - {"loom.code_repos"}


def test_the_step_purges_every_code_table_the_schema_keys_by_namespace() -> None:
    ch = _StubCH(["code_chunks", "code_symbols", "code_summaries", "code_edges"], count=4)
    n = asyncio.run(_purge("o", ["a", "b"], ch=ch))
    assert n == 16
    tables = [
        sql.split("FROM ")[1].split()[0]
        if sql.lstrip().startswith("DELETE FROM")
        else sql.split("ALTER TABLE")[1].split()[0]
        for sql, _ in ch.commands
    ]
    assert tables == [
        "loom.code_chunks",
        "loom.code_symbols",
        "loom.code_summaries",
        "loom.code_edges",
    ]
    # the two wide content tables: lightweight DELETE; the narrow ones: a plain mutation
    kinds = [
        "lightweight" if s.lstrip().startswith("DELETE FROM") else "mutation"
        for s, _ in ch.commands
    ]
    assert kinds == ["lightweight", "mutation", "lightweight", "mutation"]
    for sql, params in ch.commands:
        assert "DELETE" in sql and "UPDATE" not in sql and "tombstoned_at" not in sql
        assert "(org = {org:String} AND namespace IN {ns:Array(String)})" in sql
        assert params["ns"] == ["a", "b"]
    for sql, _ in ch.counts:
        assert " FINAL" not in sql  # no live predicate: every row is the repository's


def test_a_memory_namespace_pays_one_count_per_table_and_issues_nothing() -> None:
    ch = _StubCH(["code_chunks", "code_symbols"], count=0)
    n = asyncio.run(_purge("o", "memory-ns", ch=ch))
    assert n == 0 and ch.commands == [] and len(ch.counts) == 2
    assert all(params["ns"] == "memory-ns" for _, params in ch.counts)
    assert all("namespace = {ns:String}" in sql for sql, _ in ch.counts)


def test_the_step_is_in_the_namespace_saga_and_owns_the_content_tables() -> None:
    names = [name for name, _ in saga_steps._LATE_STEPS_NS]
    assert names[-1] == "code_index.purge"
    assert "code_index.purge" not in {name for name, _ in saga_steps._LATE_STEPS_USER}
    owners = erasure_census.step_owners("namespace")
    assert set(owners["code_index.purge"]) == {"loom.code_chunks", "loom.code_summaries"}
    assert "code_index.purge" not in erasure_census.step_owners(
        "user_id"
    )  # no user axis on the index
    assert "loom.code_chunks" not in erasure_census.census_tables("user_id")
    # residue in a content table re-opens the step on resume
    assert erasure_census.steps_to_reopen({"loom.code_chunks": 3}, "namespace") == {
        "code_index.purge"
    }
    assert erasure_census.steps_to_reopen({"loom.code_summaries": 1}, "namespace") == {
        "code_index.purge"
    }


def test_the_static_table_set_matches_the_schema() -> None:
    """The step reads its tables from system.columns; the registry classifies
    the same set statically. A code table added to the schema without a
    classification fails here, the day it lands."""
    censused = {s.table for s in erasure_census._CENSUS if s.ns_step == "code_index.purge"}
    static = censused | set(erasure_census.STEP_ONLY_TABLES["code_index.purge"])
    assert static == _schema_code_tables_keyed_by_namespace(), (
        static ^ _schema_code_tables_keyed_by_namespace()
    )
    # what stays out is configuration, telemetry or the answer cache, classified apart
    assert "loom.code_repos" in erasure_census.NO_MEMORY_CONTENT
    answers = {s.table: s for s in erasure_census._CENSUS}["loom.code_answers"]
    assert answers.ns_step == answers.user_step == "code_answers.delete"
    for kept in ("loom.code_search_trace", "loom.code_search_outcome"):
        assert kept in {s.table for s in erasure_census._CENSUS}
    # the content tables are the lightweight-delete ones; none of the rest is
    assert censused <= erasure_mutations.LIGHTWEIGHT_PURGE_TABLES
    assert (
        not set(erasure_census.STEP_ONLY_TABLES["code_index.purge"])
        & erasure_mutations.LIGHTWEIGHT_PURGE_TABLES
    )


def test_a_purge_that_removed_rows_refreshes_the_derived_code_tables(monkeypatch) -> None:
    """code_term_df is rebuilt by its view, not mutated: after a purge the view is
    refreshed so the erased namespace's rows leave now, not at the next hour."""
    started: list[int] = []

    async def fake_refresh(ch):
        started.append(1)
        return ["code_term_df_refresh"]

    monkeypatch.setattr(retire, "refresh_code_views", fake_refresh)
    asyncio.run(_purge("o", "a", ch=_StubCH(["code_chunks"], count=2)))
    assert started == [1]
    started.clear()
    asyncio.run(_purge("o", "a", ch=_StubCH(["code_chunks"], count=0)))
    assert started == []  # nothing removed, nothing to rebuild


def test_the_saga_step_reads_the_code_tables_through_retire(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_purge(org, namespace, *, code_tables, refresh_views, ch=None):
        seen.update(
            org=org, namespace=namespace, code_tables=code_tables, refresh_views=refresh_views
        )
        return 7

    monkeypatch.setattr(memory_steps, "purge_code_index_by_namespace", fake_purge)
    assert asyncio.run(saga_steps._step_purge_code_index_by_ns("o", "ns")) == 7
    assert seen == {
        "org": "o",
        "namespace": "ns",
        "code_tables": retire.code_tables_keyed_by_namespace,
        "refresh_views": retire.refresh_code_views,
    }
