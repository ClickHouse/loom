"""The backfill's second edge pass and dialect folder pass."""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from server.code.domain.extract import lang_sql_dialect as ld


def test_folder_vote_is_unanimous_within_the_directory() -> None:
    decided = [
        ("examples/authors/postgresql/schema.sql", "postgres", 0.9),
        ("examples/booktest/mysql/schema.sql", "mysql", 0.9),
        ("mixed/a.sql", "postgres", 0.9),
        ("mixed/b.sql", "mysql", 0.6),
        ("leaned/x.sql", "tsql", 0.5),  # a folder-inherited row never votes
    ]
    undecided = [
        "examples/authors/postgresql/query.sql",
        "examples/booktest/mysql/query.sql",
        "examples/booktest/sqlite/query.sql",  # nothing decided beside it
        "mixed/c.sql",
        "leaned/y.sql",
        "examples/authors/README.sql",  # the parent directory decides nothing
    ]
    assert ld.folder_dialects(decided, undecided) == {
        "examples/authors/postgresql/query.sql": "postgres",
        "examples/booktest/mysql/query.sql": "mysql",
    }
    # the stricter arms of the measurement still exist as parameters
    assert ld.folder_dialects(decided, undecided, min_files=2) == {}
    assert ld.folder_dialects(decided, undecided, min_share=0.5, min_files=1) == {
        "examples/authors/postgresql/query.sql": "postgres",
        "examples/booktest/mysql/query.sql": "mysql",
        "mixed/c.sql": "postgres",
    }


class _Repo:
    def __init__(self, live: list[dict[str, Any]]) -> None:
        self.live = live
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def live_facets_for_path(self, org: str, ns: str, path: str) -> list[dict[str, Any]]:
        return list(self.live)

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserted.append((table, rows))


def _live_dialect(conf: float) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "facet_kind": "dialect",
        "name": "postgres",
        "path": "q.sql",
        "ordinal": 0,
        "line": 1,
        "confidence": conf,
        "indexed_commit": "a" * 40,
        "tombstoned_at": None,
        "created_at": None,
        "version": 1,
    }


@pytest.mark.asyncio
async def test_reconcile_keeps_a_folder_inherited_dialect_until_the_parse_decides() -> None:
    from server.code.app.index.ingest_reconcile import _reconcile_facets
    from server.code.domain.extract.lang_sql_model import Facet

    # the parse yields no dialect: the folder's row (0.5) stays, a text-decided
    # row (0.9) that the parse no longer produces is tombstoned like any facet
    repo = _Repo([_live_dialect(ld.CONF_FOLDER)])
    wrote = await _reconcile_facets(
        repo, "o", "n", "q.sql", [Facet("statement_kind", "SELECT", 1, 1, 1.0)], "b" * 40
    )
    assert wrote and [r["facet_kind"] for r in repo.inserted[0][1]] == ["statement_kind"]
    repo = _Repo([_live_dialect(ld.CONF_CLEAR)])
    await _reconcile_facets(repo, "o", "n", "q.sql", [], "b" * 40)
    assert repo.inserted[0][1][0]["tombstoned_at"] is not None
    # the parse now decides the dialect itself: the folder's row is replaced
    repo = _Repo([_live_dialect(ld.CONF_FOLDER)])
    await _reconcile_facets(
        repo, "o", "n", "q.sql", [Facet("dialect", "mysql", 0, 1, 0.9)], "b" * 40
    )
    kinds = sorted((r["name"], r["tombstoned_at"] is not None) for r in repo.inserted[0][1])
    assert kinds == [("mysql", False), ("postgres", True)]


@pytest.mark.asyncio
async def test_dialect_folder_pass_writes_the_inherited_rows_once() -> None:
    from server.cli.code_backfill import _dialect_folder_pass

    class Repo(_Repo):
        async def live_dialect_facets(self, org: str, ns: str) -> list[dict[str, Any]]:
            return [{"path": "db/pg/schema.sql", "name": "postgres", "confidence": 0.9}]

        async def live_paths_for_lang(self, org: str, ns: str, lang: str) -> list[str]:
            assert lang == "sql"
            return ["db/pg/schema.sql", "db/pg/query.sql", "db/other/query.sql"]

    repo = Repo([])
    out = await _dialect_folder_pass(repo, "o", "n", "c" * 40)
    assert out == {"undecided": 2, "inherited": 1}
    table, rows = repo.inserted[0]
    assert table == "code_test_facets" and len(rows) == 1
    row = rows[0]
    assert (row["path"], row["name"], row["facet_kind"], row["ordinal"], row["confidence"]) == (
        "db/pg/query.sql",
        "postgres",
        "dialect",
        0,
        ld.CONF_FOLDER,
    )


def test_backfill_accepts_the_two_pass_flags() -> None:
    from server.cli.code import register_code_cli

    parser = argparse.ArgumentParser()
    register_code_cli(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(
        ["code", "backfill", "--edges-second-pass", "--dialect-folder-pass", "--no-fetch"]
    )
    assert args.edges_second_pass is True and args.dialect_folder_pass is True
    plain = parser.parse_args(["code", "backfill"])
    assert plain.edges_second_pass is False and plain.dialect_folder_pass is False
