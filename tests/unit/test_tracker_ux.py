"""The register page reads the contract before anyone
connects (`tracker.contract`); a repository card reads how often agents
opened each document (`knowledge.reads`); a maintainer's `code.repos` row
says what its tracker binding is. Every view draft parses."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from server.knowledge.api.models_knowledge import KnowledgeReadsInput
from server.knowledge.app.reads import handle_knowledge_reads
from server.tracker.api.models import TrackerContractInput
from server.tracker.app.sources import handle_tracker_contract
from server.tracker.domain import contract as rc


@pytest.mark.asyncio
async def test_the_contract_is_readable_before_a_connection() -> None:
    out = await handle_tracker_contract(TrackerContractInput(org="acme", database="loom_tracker"))
    tables = {t["table"]: t for t in out.tables}
    assert [t["table"] for t in out.tables if t["required"]] == [
        "github_issues",
        "github_prs",
        "edges",
    ]
    assert "core_incidents" in tables and not tables["core_incidents"]["required"]
    cols = {c["name"]: c for c in tables["github_issues"]["columns"]}
    assert cols["source_id"] == {
        "name": "source_id",
        "family": "String",
        "type": "String",
        "must_map": True,
        "note": "'Org/repo#number' — the repository must be one the tracker lists",
    }
    assert cols["labels"]["type"] == "Array(String)" and not cols["labels"]["must_map"]
    assert cols["triage_bisect_outcome"]["note"].startswith("a triage-bot column")
    # every required column a default would make wrong carries a note
    for t in out.tables:
        for c in t["columns"]:
            if c["must_map"]:
                assert c["note"], (t["table"], c["name"])
    # one blank template per table, naming the database
    assert set(out.templates) == set(tables)
    assert (
        "CREATE OR REPLACE VIEW loom_tracker.edges\nDEFINER = CURRENT_USER SQL SECURITY DEFINER AS"
        in out.templates["edges"]
    )
    # a database name that is not an identifier becomes the placeholder
    bad = await handle_tracker_contract(
        TrackerContractInput(org="acme", database="x; DROP TABLE y")
    )
    assert bad.database == "<database>" and "<database>.edges" in bad.templates["edges"]


def _select_list(sql: str) -> list[str]:
    body = sql.split("\nSELECT\n", 1)[1].rsplit("\nFROM ", 1)[0]
    return [line.split("--", 1)[0].rstrip() for line in body.splitlines()]


def test_every_template_separates_columns_before_the_comment() -> None:
    """A comma after `--` is commented out and the view does not parse."""
    for table, sql in rc.catalogue("db")["templates"].items():
        cols = _select_list(sql)
        assert all(c.endswith(",") for c in cols[:-1]), table
        assert not cols[-1].endswith(","), table


def test_every_template_parses_as_clickhouse_sql() -> None:
    import re
    import shutil
    import subprocess

    fmt = shutil.which("clickhouse-format")
    if not fmt:
        pytest.skip("no clickhouse-format on PATH")
    for table, sql in rc.catalogue("db")["templates"].items():
        r = subprocess.run(
            [fmt, "-n"],
            # every operator-filled placeholder (<your_table>, <from_source_id>, ...)
            input=re.sub(r"<[a-z_]+>", "src.t", sql),
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert r.returncode == 0, (table, r.stderr[:300])


class _Ch:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, dict(params or {})))
        return self.rows


@pytest.mark.asyncio
async def test_reads_count_agent_opens_not_the_dashboards(monkeypatch) -> None:
    ch = _Ch(
        [
            {
                "source_uri": "doc://a",
                "reads": 5,
                "agents": 2,
                "last_read_at": "2026-09-24 10:00:00.000",
            }
        ]
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await handle_knowledge_reads(KnowledgeReadsInput(org="acme", namespace="code-kj", days=7))
    assert out.window_days == 7
    assert out.reads == [
        {
            "source_uri": "doc://a",
            "reads": 5,
            "agents": 2,
            "last_read_at": "2026-09-24 10:00:00.000",
        }
    ]
    sql, params = ch.calls[0]
    assert "operation = 'knowledge.get'" in sql
    assert "consumer NOT IN {skip:Array(String)}" in sql
    assert "traffic_class != 'bench'" in sql
    assert params["skip"] == ["loom-ui", "loom-assistant"]
    assert params == {**params, "org": "acme", "ns": "code-kj", "days": 7, "uris": []}
    # the ledger's sort key leads with (org, namespace): the read is a key range
    assert sql.index("org = {org:String}") < sql.index("operation")


def test_the_reads_window_is_bounded() -> None:
    with pytest.raises(ValidationError):
        KnowledgeReadsInput(org="a", namespace="b", days=31)
    with pytest.raises(ValidationError):
        KnowledgeReadsInput(org="a", namespace="b", source_uris=["x"] * 201)


@pytest.mark.asyncio
async def test_a_maintainer_row_says_its_binding(monkeypatch) -> None:
    from server.code.app import repos
    from server.tracker.app import binding

    async def refresh(**k):
        return {}

    monkeypatch.setattr(binding, "refresh_overrides", refresh)
    binding.reset_overrides_for_tests({"code-kj": "tracker-kj,tracker-desk", "code-x": ""})
    try:
        lookup = await repos._bound_trackers()
        assert lookup("code-kj") == ["tracker-kj", "tracker-desk"] and lookup("code-x") == []
    finally:
        binding.reset_overrides_for_tests()


def test_the_binding_field_is_absent_for_a_reader() -> None:
    from server.code.api.models_repos import CodeRepoSummary

    row = CodeRepoSummary(org="a", namespace="code-kj", repo="A/kj", branch="main")
    assert row.bound_trackers is None
