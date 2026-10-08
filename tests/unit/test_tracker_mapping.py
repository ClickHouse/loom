"""Loom reads any tracker schema through a per-tracker mapping
onto its logical model — no views, no hardcoded deployment — and a tracker
that exposes the logical tables directly (the default source's shape) is read
exactly as before."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from server.tracker.domain import automap, columns
from server.tracker.domain import contract as rc
from server.tracker.domain import mapping as m

ACME_LIVE = {
    "acme.issues": {
        "issue_id": "UInt32",
        "repo": "String",
        "title": "String",
        "description": "String",
        "status": "LowCardinality(String)",
        "labels": "Array(String)",
        "reporter": "String",
        "opened_at": "DateTime",
        "closed_at": "Nullable(DateTime)",
        "component": "String",
    },
    "acme.pull_requests": {
        "pr_id": "UInt32",
        "repo": "String",
        "title": "String",
        "description": "String",
        "merged": "UInt8",
        "author": "String",
        "created": "DateTime",
        "closed": "Nullable(DateTime)",
        "fixes_issue": "UInt32",
        "base": "String",
        "additions": "UInt32",
        "deletions": "UInt32",
    },
}


def _acme_mapping() -> m.SourceMapping:
    ref = {"concat": ["repo", {"lit": "#"}, "issue_id"]}
    pr_ref = {"concat": ["repo", {"lit": "#"}, "pr_id"]}
    return m.parse(
        {
            "tables": {
                "github_issues": {
                    "source": "acme.issues",
                    "columns": {
                        "source_id": ref,
                        "number": "issue_id",
                        "title": "title",
                        "body": "description",
                        "created_at": "opened_at",
                        "closed_at": "closed_at",
                        "labels": "labels",
                        "author": "reporter",
                        "primary_component": "component",
                    },
                },
                "github_prs": {
                    "source": "acme.pull_requests",
                    "columns": {
                        "source_id": pr_ref,
                        "pr_number": "pr_id",
                        "title": "title",
                        "created_at": "created",
                        "closed_at": "closed",
                        "is_merged": "merged",
                    },
                },
                "edges": {
                    "source": "acme.pull_requests",
                    "columns": {
                        "from_ref": pr_ref,
                        "to_ref": {"concat": ["repo", {"lit": "#"}, "fixes_issue"]},
                        "from_type": {"lit": "github_pr"},
                        "to_type": {"lit": "github_issue"},
                        "rel_type": {"lit": "fixes"},
                    },
                },
            }
        }
    )


# ---- the logical model ----------------------------------------------------------


def test_the_logical_model_covers_every_contract_column() -> None:
    for group in (rc.REQUIRED, rc.OPTIONAL):
        for table, cols in group.items():
            assert set(cols) <= set(columns.COLUMNS_READ[table]), table
    for tables in rc.FEATURES.values():
        for table, cols in tables.items():
            assert set(cols) <= set(columns.COLUMNS_READ[table]), table
    assert set(columns.CORE) <= columns.LOGICAL_TABLES
    assert "id" not in columns.mappable_columns("github_issues")
    assert set(columns.EDGE_REFS) <= set(columns.mappable_columns("edges"))


# ---- the grammar is data, never SQL ----------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"tables": {"github_issues": {"source": "acme.issues; DROP TABLE x", "columns": {}}}},
        {"tables": {"nope": {"source": "a.b", "columns": {}}}},
        {"tables": {}},
        {"version": 2, "tables": {"github_issues": {"source": "a.b"}}},
        "[]",
    ],
)
def test_a_mapping_that_is_not_data_does_not_parse_or_render(bad: Any) -> None:
    try:
        mp = m.parse(bad)
    except (m.MappingError, ValueError):
        return
    with pytest.raises(m.MappingError):
        for logical, t in mp.tables.items():
            m.table_sql(logical, t, "acme", {"acme.issues; DROP TABLE x": {}}, final=False)


@pytest.mark.parametrize(
    "bad",
    ["{not json", {"version": "x", "tables": {"github_issues": {"source": "a.b"}}}],
)
def test_unreadable_text_or_version_is_a_mapping_error(bad: Any) -> None:
    """The contract check turns a MappingError into its failed report; any other
    exception type would skip that report."""
    with pytest.raises(m.MappingError):
        m.parse(bad)


def test_identifiers_are_checked_and_literals_quoted() -> None:
    cols = {"title": "String", "state": "String"}
    with pytest.raises(m.MappingError):
        m.render("title`) FROM x --", cols)  # not a column of the table
    with pytest.raises(m.MappingError):
        m.render({"sql": "1"}, cols)  # no free-form form
    assert m.render({"lit": "O'Brien\\"}, cols) == "'O\\'Brien\\\\'"
    assert m.render({"eq": ["state", "merged"]}, cols) == "toUInt8(`state` = 'merged')"
    assert m.render({"in": ["state", ["a", "b"]]}, cols) == "toUInt8(`state` IN ('a', 'b'))"
    assert (
        m.render({"concat": ["title", {"lit": "#"}]}, cols)
        == "concat(toString(`title`), toString('#'))"
    )
    assert (
        m.render({"digits": "title"}, cols)
        == "toUInt64OrZero(extract(toString(`title`), '(\\d+)$'))"
    )
    assert m.render({"cast": ["title", "UInt"]}, cols) == "CAST(`title` AS UInt64)"
    with pytest.raises(m.MappingError):
        m.render({"cast": ["title", "Array(String)); DROP"]}, cols)


# ---- rendering a mapped table ------------------------------------------------------


def test_a_mapped_table_supplies_every_column_the_statements_read() -> None:
    mp = _acme_mapping()
    sql = m.table_sql("github_issues", mp.tables["github_issues"], "acme", ACME_LIVE, final=True)
    for col in columns.COLUMNS_READ["github_issues"]:
        assert f" AS `{col}`" in sql, col
    assert sql.startswith("(SELECT ") and sql.endswith(
        "FROM `acme`.`issues`)"
    )  # MergeTree: no FINAL
    assert "ifNull(toDateTime64(`closed_at`, 3), toDateTime64(0, 3)) AS `closed_at`" in sql
    # the id is derived, exactly as namespaces.doc_id computes it
    assert "6ba7b8109dad11d180b400c04fd430c8" == uuid.NAMESPACE_DNS.hex
    assert "'github_issue_private:', 'github_issue:')" in sql
    replacing = m.TableMap(
        source="acme.issues", columns=mp.tables["github_issues"].columns, final=True
    )
    assert m.table_sql("github_issues", replacing, "acme", ACME_LIVE, final=True).endswith(
        " FINAL)"
    )
    assert not m.table_sql("github_issues", replacing, "acme", ACME_LIVE, final=False).endswith(
        " FINAL)"
    )


def test_unmapped_columns_are_derived_where_the_mapping_says_enough() -> None:
    mp = _acme_mapping()
    prs = m.table_sql("github_prs", mp.tables["github_prs"], "acme", ACME_LIVE, final=False)
    assert "'merged', " in prs and "AS `state`" in prs  # state from merged / closed
    assert "if(ifNull(`merged`, toUInt64(0)) = 1" in prs and "AS `merged_at`" in prs
    assert "greatest(" in prs and "AS `updated_at`" in prs
    assert "ifNull(`pr_id`, toUInt64(0)) AS `number`" in prs  # number mirrors pr_number
    edges = m.table_sql("edges", mp.tables["edges"], "acme", ACME_LIVE, final=False)
    assert edges.count("toUUID(UUIDNumToString(") == 2  # from_id and to_id, derived


def test_the_check_names_what_is_missing_and_what_defaults() -> None:
    mp = m.parse(
        {
            "tables": {
                "github_issues": {
                    "source": "acme.issues",
                    "columns": {
                        "title": "title",
                        "number": "issue_id",
                        "created_at": "opened_at",
                        "body": "nope",
                    },
                }
            }
        }
    )
    chk = m.check(mp, "acme", ACME_LIVE)
    assert chk.core_missing == {"github_issues": ["source_id"]}
    assert chk.errors == ["github_issues.body: no column 'nope'"]
    assert (
        "url" in chk.defaulted["github_issues"]
        and "updated_at" not in chk.defaulted["github_issues"]
    )
    missing_table = m.parse({"tables": {"github_issues": {"source": "acme.gone", "columns": {}}}})
    assert m.check(missing_table, "acme", ACME_LIVE).errors == [
        "github_issues: 'acme.gone' is not a table this credential can read"
    ]


# ---- the report: mapped, stubbed, and the views case unchanged ------------------------


def test_a_mapped_report_rewrites_logical_tables_and_stubs_the_rest() -> None:
    rep = rc.diff_mapped("acme", "acme", _acme_mapping(), ACME_LIVE)
    assert (
        rep.ok
        and rep.mode == "mapping"
        and set(rep.mapped) == {"github_issues", "github_prs", "edges"}
    )
    assert (
        "core_incidents" in rep.stubbed
        and rep.provides("github_prs")
        and not rep.provides("checks")
    )
    sql = rep.rewrite(
        "SELECT title FROM acme.github_issues FINAL AS i JOIN acme.core_incidents c", "acme"
    )
    assert "FROM (SELECT " in sql and "`acme`.`issues`" in sql and "acme.github_issues" not in sql
    assert "FROM system.one WHERE 0" in sql  # the unmapped incidents table answers nothing
    caps = rep.capabilities()
    assert caps["items"] and caps["duplicate_check"] and caps["links"] and caps["pull_requests"]
    assert not caps["bisect"] and not caps["incidents"] and not caps["ci_checks"]
    d = rep.to_dict()
    assert d["mode"] == "mapping" and d["capabilities"] == caps
    assert json.loads(json.dumps(d))  # on the wire as is


def test_a_views_tracker_is_rewritten_exactly_as_before() -> None:
    """The default source's shape: no mapping, the contract aliases only — the
    statement comes back through the views path, byte for byte."""
    from tests.unit.test_tracker_source_add import _full_schema

    live = _full_schema()
    issues = dict(live["github_issues"])
    issues["severity"] = issues.pop("severity_level")
    live["github_issues"] = issues
    rep = rc.diff("qa", "qa_intelligence", live)
    assert rep.mode == "views" and not rep.mapped and not rep.stubbed
    stmt = "SELECT severity_level FROM qa_intelligence.github_issues FINAL WHERE number = 1"
    expected = stmt.replace(
        "qa_intelligence.github_issues FINAL",
        rep.table_expr("qa_intelligence", "github_issues", final=True),
    )
    assert rep.rewrite(stmt, "qa_intelligence") == expected
    untouched = "SELECT title FROM qa_intelligence.github_prs FINAL"
    assert rep.rewrite(untouched, "qa_intelligence") == untouched
    assert rep.capabilities()["items"] and rep.capabilities()["bisect"]


# ---- the auto-mapper ------------------------------------------------------------------


def _tables(live: dict[str, dict[str, str]], engine: str = "MergeTree") -> list[dict[str, Any]]:
    return [
        {
            "table": k.split(".")[1],
            "engine": engine,
            "rows": 3,
            "columns": [{"name": n, "type": t} for n, t in v.items()],
        }
        for k, v in live.items()
    ]


def test_the_proposal_for_a_github_shaped_team_schema() -> None:
    p = automap.propose(_tables(ACME_LIVE), database="acme", repos=["Acme/widgets"])
    assert p["mode"] == "mapping"
    assert p["roles"] == {
        "github_issues": "issues",
        "github_prs": "pull_requests",
        "edges": "pull_requests",
        "edges_derived_from": "pull_requests.fixes_issue",
    }
    f = p["fields"]["github_issues"]
    assert f["source_id"]["from"] == {"concat": ["repo", {"lit": "#"}, "issue_id"]}
    assert (f["title"]["from"], f["body"]["from"], f["created_at"]["from"]) == (
        "title",
        "description",
        "opened_at",
    )
    assert p["fields"]["github_prs"]["is_merged"]["from"] == "merged"
    rep = rc.diff_mapped("acme", "acme", m.parse(p["mapping"]), ACME_LIVE)
    assert rep.ok, rep.summary()


def test_the_proposal_for_a_helpdesk_with_string_keys() -> None:
    live = {
        "helpdesk.tickets": {
            "ticket_key": "String",
            "summary": "String",
            "details": "String",
            "priority": "LowCardinality(String)",
            "created": "DateTime",
            "resolved": "Nullable(DateTime)",
            "reporter": "String",
            "tags": "Array(String)",
        }
    }
    p = automap.propose(
        _tables(live, "ReplacingMergeTree"), database="helpdesk", repos=["Acme/helpdesk"]
    )
    f = p["fields"]["github_issues"]
    assert f["number"]["from"] == {"digits": "ticket_key"}
    assert f["source_id"]["from"] == {
        "concat": [{"lit": "Acme/helpdesk#"}, {"digits": "ticket_key"}]
    }
    assert (f["title"]["from"], f["closed_at"]["from"], f["severity_level"]["from"]) == (
        "summary",
        "resolved",
        "priority",
    )
    assert p["mapping"]["tables"]["github_issues"]["final"] is True  # a Replacing engine dedupes
    assert "github_prs" not in p["roles"]
    rep = rc.diff_mapped("hd", "helpdesk", m.parse(p["mapping"]), live)
    assert (
        rep.ok and rep.capabilities()["duplicate_check"] and not rep.capabilities()["pull_requests"]
    )


def test_views_are_recognised_and_need_no_mapping() -> None:
    tables = [
        {"table": t, "engine": "View", "rows": 0, "columns": [{"name": "x", "type": "String"}]}
        for t in rc.REQUIRED
    ]
    assert automap.propose(tables, database="qa", repos=[])["mode"] == "views"


# ---- storage and configuration ----------------------------------------------------------


def test_a_source_entry_carries_its_mapping_checks_and_conventions(monkeypatch) -> None:
    from server.settings import settings
    from server.tracker.domain import sources

    entry = {
        "sources": {
            "acme": {
                "host": "h.example",
                "database": "acme",
                "mapping": _acme_mapping().to_dict(),
                "checks": {"from": "acme.ci_runs", "repo": "Acme/widgets"},
                "bisect": {"code_prefixes": ["lib/", "bad prefix'); DROP"]},
            }
        },
        "namespaces": {"tracker-acme": {"source": "acme", "repos": ["Acme/widgets"]}},
    }
    monkeypatch.setattr(settings, "tracker_sources_json", json.dumps(entry), raising=False)
    ep = sources.endpoint("acme")
    assert ep is not None and ep.database == "acme"
    assert json.loads(ep.mapping_json) == _acme_mapping().to_dict()
    assert sources.mapping_for("acme").tables["github_issues"].source == "acme.issues"
    assert (ep.checks_from, ep.checks_repo) == ("acme.ci_runs", "Acme/widgets")
    from server.tracker.store.source import code_path_sql, code_prefixes_for

    assert code_prefixes_for("acme") == ["lib/"]  # a prefix that is not path text is dropped
    assert code_path_sql("f", ["lib/"]) == "arrayExists(p -> startsWith(f, p), ['lib/'])"
    assert code_path_sql("f", []).startswith("NOT match(f, '^(docs?|tests?|")


def test_no_endpoint_defaults_to_a_deployments_database() -> None:
    from server.tracker.domain.sources import TrackerEndpoint

    assert TrackerEndpoint(name="x", host="h").database == ""


@pytest.mark.asyncio
async def test_a_source_row_carries_its_mapping(monkeypatch) -> None:
    from server.tracker.app import registry_sources as rs
    from server.tracker.domain import sources

    inserted: list[dict] = []

    class _Ch:
        async def insert(self, table: str, rows: list[dict], *a: Any, **k: Any) -> None:
            inserted.extend(rows)

    monkeypatch.setattr(rs, "encrypt_password", lambda p: "enc")
    src = sources.DbSource(
        org="o", name="n", host="h", port=8443, secure=True, database="d", user="u", password="p"
    )
    await rs.insert_source(src, ch=_Ch())
    await rs.insert_source(
        sources.DbSource(**{**src.__dict__, "mapping_json": '{"x":1}'}), ch=_Ch()
    )
    assert inserted[0]["mapping_json"] == "" and inserted[1]["mapping_json"] == '{"x":1}'


def test_checks_follow_the_tracker_then_the_deployment(monkeypatch) -> None:
    from server.settings import settings
    from server.tracker.store import source as src

    base = {"host": "h", "database": "acme"}
    entry = lambda extra: json.dumps(  # noqa: E731
        {
            "sources": {"acme": {**base, **extra}},
            "namespaces": {"tracker-acme": {"source": "acme", "repos": ["Acme/w"]}},
        }
    )
    monkeypatch.setattr(settings, "tracker_org", "acme", raising=False)
    monkeypatch.setattr(settings, "tracker_checks_from", "", raising=False)
    monkeypatch.setattr(settings, "tracker_checks_repo", "", raising=False)
    monkeypatch.setattr(settings, "tracker_sources_json", entry({}), raising=False)
    assert src.checks_config("acme", "tracker-acme") == ("", "Acme/w")  # none configured
    monkeypatch.setattr(settings, "tracker_checks_from", "remote('x')", raising=False)
    assert src.checks_config("acme", "tracker-acme") == ("remote('x')", "Acme/w")
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        entry({"checks": {"from": "acme.ci", "repo": "Acme/w"}}),
        raising=False,
    )
    assert src.checks_config("acme", "tracker-acme") == (
        "acme.ci",
        "Acme/w",
    )  # the tracker's own wins


def test_an_error_on_a_longer_named_table_leaves_its_prefix_twin_mapped() -> None:
    """`version_history_private`'s mapping error is not `version_history`'s:
    the table whose own mapping is valid is still rendered."""
    live = {
        **ACME_LIVE,
        "acme.versions": {"sha": "String", "ver": "String", "started": "DateTime", "pr": "UInt32"},
    }
    mapping = m.parse(
        {
            "tables": {
                "github_issues": {
                    "source": "acme.issues",
                    "columns": {
                        "source_id": {"concat": ["repo", {"lit": "#"}, "issue_id"]},
                        "number": "issue_id",
                        "title": "title",
                        "created_at": "opened_at",
                    },
                },
                "version_history": {
                    "source": "acme.versions",
                    "columns": {
                        "commit_sha": "sha",
                        "version": "ver",
                        "check_start_time": "started",
                        "pull_request_number": "pr",
                    },
                },
                "version_history_private": {
                    "source": "acme.versions",
                    "columns": {"commit_sha": "sha", "bogus": "ver"},
                },
            }
        }
    )
    rep = rc.diff_mapped("acme", "acme", mapping, live)
    assert rep.mapping_errors == ["version_history_private.bogus: not a column loom reads"]
    assert set(rep.mapped) == {"github_issues", "version_history"}
