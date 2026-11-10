"""A contract column the source spells another way is an
alias — declared in the contract, verified like a column, reported apart,
and projected onto the table reference once, in `TrackerSource._rows`. The
fixture is the default source's shape after an upstream column rename, which
without aliases would take every tracker read dark."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import server.tracker.app.contract as ct
import server.tracker.store.source as src
from server.tracker.domain import contract as rc
from server.tracker.store.source import contract_reports as reports
from tests.unit.clickhouse_local import CLICKHOUSE, run
from tests.unit.test_tracker_sources import _full_schema

DB = "qa_intelligence"


def _live_described() -> rc.Described:
    """`system.columns` of the renamed source — the contract's tables only,
    types as the catalogue prints them."""
    issues = _full_schema()["github_issues"]
    del issues["severity_level"]
    del issues["has_test"]
    issues["severity"] = "LowCardinality(String)"
    issues["metadata"] = "JSON"
    prs = _full_schema()["github_prs"]
    for gone in ("severity_level", "has_test", "review_requests_test", "upstream_pr_number"):
        del prs[gone]
    prs.update(
        {
            "tests_added": "UInt16",
            "synced_from_pr_id": "UUID",
            "metadata": "JSON",
            "is_reverted": "UInt8",
            "merged_at": "Nullable(DateTime64(3))",
        }
    )  # ALIAS upstream
    incidents = _full_schema()["core_incidents"]
    del incidents["severity_level"]
    incidents["metadata"] = "JSON"
    return {
        **_full_schema(),
        "github_issues": issues,
        "github_prs": prs,
        "core_incidents": incidents,
        "qa_bugs": {
            "source_id": "String",
            "url": "String",
            "title": "String",
            "source": "String",
            "severity": "LowCardinality(String)",
            "primary_component": "LowCardinality(String)",
            "labels": "Array(String)",
            "metadata": "JSON",
            "created_at": "DateTime64(3)",
        },
    }


# ------------------------------------------------------------------ the diff


def test_the_live_rename_meets_the_contract_through_aliases() -> None:
    rep = rc.diff("qa", DB, _live_described())
    assert rep.ok, rep.summary()
    assert rep.missing_columns == {} and rep.missing_tables == []
    assert rep.aliased["github_issues"] == {
        "severity_level": "severity",
        "has_test": "toUInt8(metadata.has_test.:Bool)",
    }
    assert rep.aliased["github_prs"] == {
        "has_test": "toUInt8(metadata.has_test.:Bool)",
        "upstream_pr_number": "ifNull(toUInt32(metadata.original_pr_number.:Int64), 0)",
    }
    assert rep.defaulted["github_prs"] == {
        "severity_level": "''",
        "review_requests_test": "CAST(NULL AS Nullable(UInt8))",
    }
    # the optional incidents table is present again, with its default
    assert "core_incidents" in rep.optional_present
    assert rep.defaulted["core_incidents"] == {"severity_level": "''"}
    # a table outside the contract that a statement reads gets its aliases too
    assert rep.aliased["qa_bugs"] == {
        "severity_level": "severity",
        "has_test": "toUInt8(metadata.has_test.:Bool)",
    }
    s = rep.summary()
    assert s.startswith("ok; aliased ") and "github_prs.{has_test, upstream_pr_number}" in s
    assert "defaulted github_prs.{severity_level, review_requests_test}" in s
    d = rep.to_dict()
    assert d["aliased"]["github_issues"]["severity_level"] == "severity" and d["summary"] == s
    # every projected table names its live columns one by one (ALIAS columns included)
    assert set(rep.columns) == {"github_issues", "github_prs", "core_incidents", "qa_bugs"}
    assert "is_reverted" in rep.columns["github_prs"] and "merged_at" in rep.columns["github_prs"]
    assert d["projected_columns"]["github_prs"] == len(rep.columns["github_prs"])


def test_an_alias_is_verified_like_a_column_and_falls_back_in_order() -> None:
    described = _live_described()
    # no JSON column: `has_test` falls back to the count, `upstream_pr_number` to 0
    del described["github_prs"]["metadata"]
    rep = rc.diff("qa", DB, described)
    assert rep.ok and rep.aliased["github_prs"] == {"has_test": "toUInt8(tests_added > 0)"}
    assert rep.defaulted["github_prs"]["upstream_pr_number"] == "0"
    # the alias's own column with the wrong family is no alias
    described["github_prs"]["tests_added"] = "String"
    rep = rc.diff("qa", DB, described)
    assert not rep.ok and rep.missing_columns == {"github_prs": ["has_test"]}
    assert "missing columns github_prs.{has_test}" in rep.summary()
    # neither the column nor any alias: missing, as before
    described = _live_described()
    del described["github_issues"]["severity"]
    rep = rc.diff("qa", DB, described)
    assert not rep.ok and rep.missing_columns == {"github_issues": ["severity_level"]}


def test_a_source_with_the_contract_columns_has_no_aliases_and_the_statement_is_untouched():
    rep = rc.diff("cs", "loom_qa", _full_schema())
    assert rep.ok and rep.summary() == "ok" and not rep.aliased and not rep.defaulted
    sql = "SELECT title FROM loom_qa.github_prs FINAL WHERE has_test = 0"
    assert rep.rewrite(sql, "loom_qa") == sql
    assert rep.table_expr("loom_qa", "github_prs", final=True) == "loom_qa.github_prs FINAL"


def test_family_json_covers_the_json_column_types() -> None:
    assert rc.family_ok("JSON", "JSON") and rc.family_ok("JSON", "JSON(max_dynamic_paths=16)")
    assert rc.family_ok("JSON", "Object('json')") and not rc.family_ok("JSON", "String")


def test_every_alias_names_only_columns_the_contract_lacks_and_reads_families_it_knows():
    families = {"String", "UInt", "Int", "Float", "DateTime", "UUID", "JSON"}
    for table, cols in rc.ALIASES.items():
        contract_cols = {**rc.REQUIRED.get(table, {}), **rc.OPTIONAL.get(table, {})}
        for col, aliases in cols.items():
            if table in rc.REQUIRED or table in rc.OPTIONAL:
                assert col in contract_cols, f"{table}.{col} is not a contract column"
            assert aliases, f"{table}.{col} has no alias"
            for a in aliases:
                assert a.expr.strip()
                assert set(a.needs.values()) <= families, (table, col, a.needs)
    # a described table is asked for every alias table, or its aliases never resolve
    assert set(rc.ALIASES) <= set(ct._tables_of_interest())


# --------------------------------------------------------------- the rewrite


def test_rewrite_projects_the_aliases_onto_every_reference_and_keeps_final() -> None:
    rep = rc.diff("qa", DB, _live_described())
    sql = f"""
        SELECT source_id, severity_level FROM {DB}.github_issues FINAL WHERE is_private = 0
        UNION ALL
        SELECT source_id, severity_level FROM {DB}.github_prs FINAL WHERE ifNull(has_test, 0) = 0
        UNION ALL
        SELECT source_id, severity_level FROM {DB}.qa_bugs WHERE source IN {{s:Array(String)}}
    """
    out = rep.rewrite(sql, DB)
    issues = f"FROM (SELECT {', '.join(rep.columns['github_issues'])}, severity AS severity_level, toUInt8(metadata.has_test.:Bool) AS has_test FROM {DB}.github_issues FINAL) WHERE"
    assert issues in out and "severity_level, " not in issues.split(" AS ")[0]
    assert (
        f", toUInt8(metadata.has_test.:Bool) AS has_test, "
        f"ifNull(toUInt32(metadata.original_pr_number.:Int64), 0) AS upstream_pr_number, "
        f"'' AS severity_level, CAST(NULL AS Nullable(UInt8)) AS review_requests_test "
        f"FROM {DB}.github_prs FINAL) WHERE ifNull(has_test, 0) = 0"
    ) in out
    assert "SELECT *" not in out
    # a live column the projection defines is named once, as the alias
    prs_inner = out.split(f"FROM {DB}.github_prs FINAL)")[0].rsplit("(SELECT ", 1)[1]
    assert (
        prs_inner.count(" has_test") == 1
        and prs_inner.split(",")[0].strip() in rep.columns["github_prs"]
    )
    # the view is read without FINAL and stays that way
    assert (
        f", severity AS severity_level, toUInt8(metadata.has_test.:Bool) AS has_test FROM {DB}.qa_bugs) WHERE"
        in out
    )
    # untouched: the edge table, a table of another database, a longer name
    other = f"SELECT 1 FROM {DB}.edges JOIN other.github_prs FINAL USING x, {DB}.github_prs_stats"
    assert rep.rewrite(other, DB) == other


def test_the_reads_project_through_the_report_only_when_it_says_so() -> None:
    """The statements in the `source` package name the contract's columns;
    `rewrite` reaches every table reference they make — the f-strings say
    `{self.db}.<table>` and nothing else (a reference the rewrite cannot see
    would fail live)."""
    text = "".join(
        p.read_text(encoding="utf-8") for p in sorted(Path(src.__file__).parent.glob("*.py"))
    )
    refs = re.findall(r"\{self\.db\}\.([a-z_]+)", text)
    assert {"github_prs", "github_issues", "core_incidents", "qa_bugs"} <= set(refs)
    bad = re.findall(
        r"(?<!\{self\.db\}\.)\b(?:qa_intelligence|loom_qa)\.(?:github_prs|github_issues)\b", text
    )
    assert not bad, bad


@pytest.mark.asyncio
async def test_rows_rewrites_after_the_gate_cached_the_report(monkeypatch) -> None:
    """The funnel: `_admit` caches the report, `_rows` applies it — so the
    first read of a process already projects the aliases."""
    from tests.unit.test_tracker_sources import ORG, PUB, _with_sources

    _with_sources(monkeypatch)
    reports.reset_for_tests()
    src.reset_cache_for_tests()
    monkeypatch.setattr(
        "server.tracker.store.source.base.settings.tracker_live_serve", True, raising=False
    )
    monkeypatch.setattr(ct, "_enforce", True)
    seen: list[str] = []

    async def fake_query_rows(sql, params=None, *, credential="qa", **kw):
        if "system.columns" in sql:
            return [
                {"table": t, "name": c, "type": ty}
                for t, cols in _live_described().items()
                for c, ty in cols.items()
            ]
        seen.append(sql)
        return [{"n": 1}]

    monkeypatch.setattr("server.tracker.store.source.qa_intel.query_rows", fake_query_rows)
    monkeypatch.setattr("server.tracker.domain.sources.credential_configured", lambda c: True)
    s = src.TrackerSource(ORG, PUB)
    rows = await s._rows(
        "t",
        f"SELECT count() AS n FROM {s.db}.github_prs FINAL WHERE has_test = 1",
        {},
        cache_key={"k": 1},
    )
    assert rows == [{"n": 1}]
    assert len(seen) == 1 and "AS has_test," in seen[0] and "github_prs FINAL) WHERE" in seen[0]
    assert "SELECT *" not in seen[0]
    assert " is_reverted," in seen[0] and " merged_at," in seen[0]  # ALIAS columns, named


_TRIAGE = (
    "triage_bisect_outcome",
    "triage_reproduces_on",
    "triage_clean_on",
    "triage_introduced_commit",
    "triage_component",
    "triage_confidence",
)


def test_a_tracker_without_triage_columns_serves_them_empty() -> None:
    described = _full_schema()
    for col in _TRIAGE:
        del described["github_issues"][col]
    rep = rc.diff("cs", "loom_qa", described)
    assert rep.ok, rep.summary()
    assert rep.defaulted["github_issues"]["triage_bisect_outcome"] == "''"
    assert rep.defaulted["github_issues"]["triage_clean_on"] == "CAST([] AS Array(String))"
    assert not rep.has_column("github_issues", "triage_bisect_outcome")


def test_a_configured_rename_serves_a_contract_column_from_the_trackers_own() -> None:
    described = _full_schema()
    described["github_issues"]["bisect_result"] = described["github_issues"].pop(
        "triage_bisect_outcome"
    )
    rep = rc.diff(
        "cs",
        "loom_qa",
        described,
        {"github_issues": {"triage_bisect_outcome": "bisect_result"}},
    )
    assert rep.ok, rep.summary()
    assert rep.aliased["github_issues"] == {"triage_bisect_outcome": "`bisect_result`"}
    assert rep.has_column("github_issues", "triage_bisect_outcome")
    sql = rep.rewrite("SELECT triage_bisect_outcome FROM loom_qa.github_issues", "loom_qa")
    assert "`bisect_result` AS triage_bisect_outcome" in sql


def test_a_rename_is_checked_like_the_column_it_serves() -> None:
    described = _full_schema()
    described["github_issues"].pop("triage_reproduces_on")
    described["github_issues"]["repro"] = "UInt8"
    rep = rc.diff("cs", "loom_qa", described, {"github_issues": {"triage_reproduces_on": "repro"}})
    assert "triage_reproduces_on" in rep.type_mismatches.get("github_issues", {})


def test_a_tracker_without_pr_test_types_or_src_components_serves_them_empty() -> None:
    described = _full_schema()
    del described["github_prs"]["test_types"]
    del described["github_prs"]["src_components"]
    rep = rc.diff("qa", DB, described)
    assert rep.ok, rep.summary()
    assert rep.defaulted["github_prs"] == {
        "test_types": "CAST([] AS Array(String))",
        "src_components": "CAST([] AS Array(String))",
    }
    assert "defaulted github_prs.{test_types, src_components}" in rep.summary()
    sql = rep.rewrite(f"SELECT test_types, src_components FROM {DB}.github_prs FINAL", DB)
    assert "CAST([] AS Array(String)) AS test_types" in sql
    assert "CAST([] AS Array(String)) AS src_components" in sql


def test_a_tracker_without_the_bisect_columns_serves_them_empty() -> None:
    described = _full_schema()
    del described["github_prs"]["diff"]
    del described["github_prs"]["affected_file_paths"]
    rep = rc.diff("qa", DB, described)
    assert rep.ok, rep.summary()
    assert rep.defaulted["github_prs"] == {
        "diff": "''",
        "affected_file_paths": "CAST([] AS Array(String))",
    }
    assert rep.features["bisect"] is False and not rep.capabilities()["bisect"]
    assert not rep.has_column("github_prs", "affected_file_paths")
    assert "defaulted github_prs.{diff, affected_file_paths}" in rep.summary()


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_the_pr_file_reads_run_on_a_tracker_without_the_bisect_columns(monkeypatch) -> None:
    """The reads that name `affected_file_paths` and `diff`, run on ClickHouse against a
    `github_prs` without either column: each answers as for a PR that touched no file."""
    from tests.unit.test_tracker_sources import ORG, PUB, REPO, _with_sources

    _with_sources(monkeypatch)
    monkeypatch.setattr(
        "server.tracker.store.source.base.settings.tracker_live_serve", True, raising=False
    )
    described = _full_schema()
    del described["github_prs"]["diff"]
    del described["github_prs"]["affected_file_paths"]
    # read beside the contract's columns, as the live table holds them
    described["github_prs"].update({"is_reverted": "UInt8", "backport_required": "UInt8"})
    cols = ", ".join(f"`{c}` {t}" for c, t in described["github_prs"].items())
    setup = [
        f"CREATE DATABASE {DB}",
        f"CREATE TABLE {DB}.github_prs ({cols}) ENGINE = ReplacingMergeTree ORDER BY source_id",
        f"INSERT INTO {DB}.github_prs (source_id, pr_number, number, title, is_private,"
        f" is_merged, created_at) VALUES ('{REPO}#5', 5, 5, 'a fix', 0, 1, now64(3))",
    ]
    reports.reset_for_tests()
    src.reset_cache_for_tests()
    monkeypatch.setattr(ct, "_enforce", True)

    async def query_rows(sql, params=None, *, credential="qa", **kw):
        if "system.columns" in sql:
            return [
                {"table": t, "name": c, "type": ty}
                for t, tcols in described.items()
                for c, ty in tcols.items()
            ]
        return run(setup, sql, params or {})

    monkeypatch.setattr("server.tracker.store.source.qa_intel.query_rows", query_rows)
    monkeypatch.setattr("server.tracker.domain.sources.credential_configured", lambda c: True)
    s = src.TrackerSource(ORG, PUB)
    assert await s.pr_files(ORG, PUB, [5]) == {5: []}
    assert await s.prs_touching_paths(ORG, PUB, ["src/a.cpp"]) == []
    change = await s.pr_change(ORG, PUB, 5)
    assert change is not None and change["files"] == [] and change["hunk_contexts"] == []
