"""A tracker source is registered from the page. The form's
Check is `tracker.source.add(dry_run=True)` — every gate, nothing written —
and a schema that fails the contract comes back table by table with the
views that would meet it, instead of one refusal sentence. The draft is
loom's reading of the owner's schema; applied as written, it passes the
contract. A binding made a moment after registration, on another process,
finds the source."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import server.tracker.app.onboarding as ob
from server.core.errors import ValidationError
from server.tracker.api.models import TrackerSourceListInput
from server.tracker.app.sources import handle_tracker_source_add, handle_tracker_source_list
from server.tracker.domain import contract as rc
from tests.unit.test_tracker_source_add import (  # noqa: F401 — _cfg is the autouse fixture
    ORG,
    _add_input,
    _cfg,
    _full_schema,
    _probe,
)


def _failing_schema() -> rc.Described:
    """github_prs without its PR number and reviewers and with `additions`
    as text; no edges table at all; issues fine."""
    live = _full_schema()
    prs = dict(live["github_prs"])
    del prs["pr_number"], prs["reviewers"]
    prs["additions"] = "String"
    live["github_prs"] = prs
    del live["edges"]
    return live


def _apply(templates: dict[str, str], live: rc.Described) -> rc.Described:
    """The schema after the owner runs each draft: a view's columns are the
    names after `AS` (or the bare passthrough), typed as the draft writes
    them — a passthrough keeps the live type, a CAST or default its concrete
    type."""
    out = {t: dict(c) for t, c in live.items()}
    for table, sql in templates.items():
        want = rc.REQUIRED[table]
        have = live.get(table, {})
        cols: dict[str, str] = {}
        for line in sql.splitlines():
            code = line.split("--")[0].strip().rstrip(",")
            if not code or code.startswith(("CREATE", "SELECT", "FROM", "DEFINER")):
                continue
            col = code.rsplit(" AS ", 1)[-1].strip() if " AS " in code else code
            cols[col] = have[col] if code == col else rc.concrete_type(want[col])
        out[table] = cols
    return out


@pytest.mark.asyncio
async def test_a_dry_run_checks_every_gate_and_writes_nothing(_cfg) -> None:  # noqa: F811
    ch = _cfg
    out = await handle_tracker_source_add(_add_input(dry_run=True))
    assert out.dry_run is True and out.written is False
    assert out.contract["ok"] is True and out.view_templates == {}
    assert [(n["namespace"], n["classification"], n["join_policy"]) for n in out.namespaces] == [
        ("tracker-clickstack", "standard", "open"),
        ("tracker-stack-private", "restricted", "approval"),
    ]
    assert ch.inserted == {}  # no source row, no directory row, no grant
    # the real add after it is unchanged
    real = await handle_tracker_source_add(_add_input())
    assert real.written is True and real.dry_run is False and "loom.qa_sources" in ch.inserted


@pytest.mark.asyncio
async def test_a_dry_run_still_refuses_a_write_credential(_cfg, monkeypatch) -> None:  # noqa: F811
    monkeypatch.setattr(ob, "probe_credential", _probe(grants=["GRANT INSERT ON loom_qa.* TO u"]))
    with pytest.raises(ValidationError) as e:
        await handle_tracker_source_add(_add_input(dry_run=True))
    assert e.value.to_detail()["code"] == "CREDENTIAL_NOT_READONLY"


@pytest.mark.asyncio
async def test_a_failing_schema_comes_back_with_the_views_that_meet_the_contract(
    _cfg,  # noqa: F811
    monkeypatch,
) -> None:
    ch = _cfg
    live = _failing_schema()
    monkeypatch.setattr(ob, "probe_credential", _probe(described=live))
    # the real add refuses, as before
    with pytest.raises(ValidationError) as e:
        await handle_tracker_source_add(_add_input())
    assert e.value.to_detail()["code"] == "CONTRACT_FAILED"
    out = await handle_tracker_source_add(_add_input(dry_run=True))
    assert out.contract["ok"] is False and out.written is False and ch.inserted == {}
    assert out.contract["missing_tables"] == ["edges"]
    assert "pr_number" in out.contract["missing_columns"]["github_prs"]
    assert set(out.view_templates) == {"github_prs", "edges"}  # issues pass: no draft
    prs = out.view_templates["github_prs"]
    # A DEFINER view (loom's user reads the view, not the tables)
    assert (
        "CREATE OR REPLACE VIEW loom_qa.github_prs\nDEFINER = CURRENT_USER SQL SECURITY DEFINER AS"
        in prs
    )
    assert "    title," in prs  # passthrough
    assert "CAST(additions AS UInt64) AS additions,  -- is String" in prs
    assert "toUInt64(0) AS pr_number,  -- MUST map" in prs  # identity: a default is wrong
    assert "CAST([] AS Array(String)) AS reviewers,  -- map it, or keep the default" in prs
    assert prs.rstrip().endswith("FROM <your_table>;")
    edges = out.view_templates["edges"]
    assert "from_id,  -- MUST map" in edges and "AS rel_type,  -- MUST map" in edges
    # the draft, applied as written, meets the contract
    assert rc.diff("clickstack", "loom_qa", _apply(out.view_templates, live)).ok


def test_a_column_loom_aliases_itself_is_left_out_of_the_draft() -> None:
    live = _full_schema()
    issues = dict(live["github_issues"])
    issues["severity"] = issues.pop("severity_level")  # the upstream rename
    del issues["title"]
    live["github_issues"] = issues
    rep = rc.diff("s", "db", live)
    assert rep.aliased["github_issues"] == {"severity_level": "severity"}
    sql = rc.view_templates(rep)["github_issues"]
    assert "severity_level" not in sql and "'' AS title,  -- MUST map" in sql


def test_types_and_defaults_by_family() -> None:
    assert rc.concrete_type("Array(UInt)") == "Array(UInt64)"
    assert rc.default_expr("Array(String)") == "CAST([] AS Array(String))"
    assert rc.default_expr("DateTime") == "toDateTime64(0, 3)"
    for fam in ("String", "UInt", "Int", "Float", "DateTime", "UUID", "JSON", "Array(Float)"):
        assert rc.family_ok(fam, rc.concrete_type(fam)), fam


@pytest.mark.asyncio
async def test_the_listing_says_whether_the_page_may_register(_cfg, monkeypatch) -> None:  # noqa: F811
    from server.settings import settings

    out = await handle_tracker_source_list(TrackerSourceListInput(org=ORG))
    assert out.self_service is True
    monkeypatch.setattr(settings, "tracker_self_service_enabled", False)
    out = await handle_tracker_source_list(TrackerSourceListInput(org=ORG))
    assert out.self_service is False


@pytest.mark.asyncio
async def test_a_binding_rereads_the_registry_before_refusing(monkeypatch) -> None:
    """Registered on process A, bound on process B within B's snapshot TTL:
    B reads the registry once before saying the namespace is unknown."""
    from server.code.app import repo_admin as h
    from server.tracker.app import binding, registry_sources

    known: set[str] = set()
    refreshed: list[bool] = []

    async def refresh_sources(*, force=False, ch=None):
        refreshed.append(force)
        known.add("tracker-kj")
        return []

    class _Repo:
        async def get_state(self, org, ns, key):
            return binding.override_for(ns) or ""

        async def set_state(self, org, ns, key, value, detail=""):
            binding.reset_overrides_for_tests({ns: value})

    async def _noop(*a, **k):
        return {}

    monkeypatch.setattr(h.reg, "refresh_db_repos", _noop)
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: SimpleNamespace(repo="Acme/kj"),
    )
    records = {"tracker-kj": ("Acme/kj",), "tracker-other": ("Acme/other",)}
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.tracker_namespace",
        lambda org, ns: SimpleNamespace(repos=records[ns]) if ns in known else None,
    )
    monkeypatch.setattr(registry_sources, "refresh_db_sources", refresh_sources)
    monkeypatch.setattr("server.code.store.index.CodeRepo", lambda: _Repo())
    monkeypatch.setattr(binding, "refresh_overrides", _noop)
    monkeypatch.setattr(h, "current_caller", lambda: ("alice", ORG))

    async def _readable(org, ns, role=None, *, hides_answer=True):
        return True

    monkeypatch.setattr("server.auth.authz.row_in_scope", _readable)
    binding.reset_overrides_for_tests()
    out = await h.handle_code_repo_set_source(
        SimpleNamespace(org=ORG, namespace="code-kj", tracker_namespaces=["tracker-kj"])
    )
    assert out.tracker_namespaces == ["tracker-kj"] and refreshed == [True]
    assert out.not_covering == [] and out.warning == ""
    # a record that does not name the repository binds, and says it will answer nothing
    known.add("tracker-other")
    out = await h.handle_code_repo_set_source(
        SimpleNamespace(org=ORG, namespace="code-kj", tracker_namespaces=["tracker-other"])
    )
    assert out.not_covering == ["tracker-other"]
    assert out.warning == (
        "tracker-other names Acme/other, not Acme/kj: its reads will not return "
        "Acme/kj's issues and PRs"
    )
    # clearing covers trivially
    out = await h.handle_code_repo_set_source(
        SimpleNamespace(org=ORG, namespace="code-kj", tracker_namespaces=[])
    )
    assert out.not_covering == []
    refreshed.clear()
    # a name the registry does not hold either is still refused, after one read
    with pytest.raises(ValidationError) as e:
        await h.handle_code_repo_set_source(
            SimpleNamespace(org=ORG, namespace="code-kj", tracker_namespaces=["kj"])
        )
    assert e.value.to_detail()["code"] == "NOT_A_TRACKER_NAMESPACE" and refreshed == [True]
    binding.reset_overrides_for_tests()


def _select_list(sql: str) -> list[str]:
    """The SELECT list of a draft, each line with its comment cut off."""
    body = sql.split("\nSELECT\n", 1)[1].rsplit("\nFROM ", 1)[0]
    return [line.split("--", 1)[0].rstrip() for line in body.splitlines()]


def test_every_draft_separates_its_columns_before_the_comment() -> None:
    """A comma after `--` is commented out and the view does not parse: every
    column but the last ends in a comma once the comment is cut off, the last
    does not."""
    live = _failing_schema()
    rep = rc.diff("clickstack", "loom_qa", live)
    drafts = rc.view_templates(rep)
    assert drafts
    for table, sql in drafts.items():
        cols = _select_list(sql)
        assert len(cols) > 1, table
        assert all(c.endswith(",") for c in cols[:-1]), (
            table,
            [c for c in cols[:-1] if not c.endswith(",")],
        )
        assert not cols[-1].endswith(","), table


def test_every_draft_parses_as_clickhouse_sql() -> None:
    """The drafts through ClickHouse's own parser, where one is installed."""
    import re
    import shutil
    import subprocess

    fmt = shutil.which("clickhouse-format") or shutil.which("clickhouse")
    if not fmt:
        pytest.skip("no clickhouse-format on PATH")
    args = [fmt, "-n"] if fmt.endswith("clickhouse-format") else [fmt, "format", "-n"]
    for table, sql in rc.view_templates(rc.diff("s", "db", _failing_schema())).items():
        r = subprocess.run(
            args,
            # every operator-filled placeholder (<your_table>, <from_source_id>, ...)
            input=re.sub(r"<[a-z_]+>", "src.t", sql),
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert r.returncode == 0, (table, r.stderr[:300])
