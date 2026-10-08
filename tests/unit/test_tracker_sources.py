"""A tracker namespace names its source, a source has its own
endpoint, credential and breaker, and a source serves only while its schema
meets the record contract. Every source, the default one included, is
configured through LOOM_TRACKER_SOURCES_JSON + LOOM_TRACKER_CREDENTIALS_JSON."""

from __future__ import annotations

import re
from typing import Any

import pytest

import server.tracker.app.contract as ct
import server.tracker.domain.namespaces as qn
import server.tracker.domain.sources as qs
import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.settings import settings
from server.tracker.domain import contract as rc
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.source import contract_reports as reports
from server.tracker.store.source.rows import ISSUE_COLS, PR_COLS
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG = "clickhouse"
PUB, PRIV = "tracker-clickhouse", "tracker-example-private"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"

SOURCES = {
    "sources": {
        "clickstack": {
            "host": "https://cs.example/",
            "port": 8443,
            "secure": True,
            "database": "loom_qa",
        },
        "broken": {"host": "", "database": "x"},
    },
    "namespaces": {
        "tracker-clickstack": {"source": "clickstack", "repos": ["ClickHouse/clickstack"]},
        "tracker-stack-private": {
            "source": "clickstack",
            "repos": ["ClickHouse/cs-private"],
            "restricted": True,
        },
        "tracker-orphan": {"source": "nowhere", "repos": ["x/y"]},
    },
}
CREDS = {"clickstack": {"user": "cs_ro", "password": "s3cret"}}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    qs.reset_warnings_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json({REPO: PUB, PREPO: PRIV}, restricted=PRIV),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(settings, "tracker_live_serve", True)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()


def _with_sources(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(
            {REPO: PUB, PREPO: PRIV},
            restricted=PRIV,
            extra_sources=SOURCES["sources"],
            extra_namespaces=SOURCES["namespaces"],
        ),
    )
    monkeypatch.setattr(
        settings,
        "tracker_credentials_json",
        tracker_credentials_json(clickstack=CREDS["clickstack"]),
    )


# ---------------------------------------------------------------- configuration


def test_the_sources_json_alone_configures_qa_intelligence_like_any_source() -> None:
    eps = qs.endpoints()
    assert list(eps) == ["qa"]
    assert eps["qa"] == qs.TrackerEndpoint(
        name="qa", host="qa.example", port=8443, secure=True, database="qa_intelligence"
    )
    entries = qs.namespace_entries()
    assert entries[PUB] == qs.NamespaceEntry(PUB, "qa", (REPO,), False)
    assert entries[PRIV] == qs.NamespaceEntry(PRIV, "qa", (PREPO,), True)
    assert qs.credentials_for("qa") == ("ro", "pw")
    cfg = qn.configured_namespaces()
    assert cfg[PUB].credential == "qa" and cfg[PUB].database == "qa_intelligence"
    assert cfg[PRIV].restricted and not cfg[PUB].restricted
    assert qn.repo_namespaces() == {REPO: PUB, PREPO: PRIV}
    assert qn.restricted_namespaces() == frozenset({PRIV})
    assert qi.credentials() == ("qa",) and qi.CREDENTIALS == ("qa",)


def test_sources_json_adds_endpoints_and_namespaces_and_drops_the_unservable(monkeypatch, caplog):
    _with_sources(monkeypatch)
    eps = qs.endpoints()
    assert set(eps) == {"qa", "clickstack"}  # `broken` has no host
    assert eps["clickstack"].host == "cs.example" and eps["clickstack"].database == "loom_qa"
    entries = qs.namespace_entries()
    assert set(entries) == {PUB, PRIV, "tracker-clickstack", "tracker-stack-private"}
    assert entries["tracker-clickstack"].source == "clickstack"
    assert entries["tracker-stack-private"].restricted
    assert not entries["tracker-clickstack"].restricted
    # a namespace naming an unknown source is dropped, loudly, once
    assert "tracker-orphan" not in entries
    assert any("nowhere" in r.message for r in caplog.records)
    n = sum("nowhere" in r.message for r in caplog.records)
    qs.namespace_entries()
    assert sum("nowhere" in r.message for r in caplog.records) == n
    # credentials: one entry per source, nothing for a source without one
    assert qs.credentials_for("clickstack") == ("cs_ro", "s3cret")
    assert qs.credentials_for("qa") == ("ro", "pw")
    assert qs.credentials_for("broken") == ("", "")
    cfg = qn.configured_namespaces()
    assert cfg["tracker-clickstack"].credential == "clickstack"
    assert cfg["tracker-clickstack"].database == "loom_qa"
    assert qn.restricted_namespaces() == frozenset({PRIV, "tracker-stack-private"})
    assert qi.credentials() == ("clickstack", "qa")


def test_invalid_sources_json_serves_nothing(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "tracker_sources_json", "{not json")
    assert qs.endpoints() == {} and qs.namespace_entries() == {}
    assert any("LOOM_TRACKER_SOURCES_JSON" in r.message for r in caplog.records)


# ---------------------------------------------------------------- the client


def test_client_has_a_slot_and_credential_per_source(monkeypatch) -> None:
    _with_sources(monkeypatch)
    assert qs.credential_configured("clickstack") and qs.credential_configured("qa")
    assert not qs.credential_configured("broken") and not qs.credential_configured("nope")
    assert qi._slot("clickstack").breaker.name == "clickstack"
    assert qi._slot("clickstack") is not qi._slot("qa")
    with pytest.raises(qi.QaIntelUnavailable):
        qi._slot("nope")
    # breakers are independent
    monkeypatch.setattr(settings, "tracker_circuit_failure_threshold", 1)
    qi.circuit_record(False, "clickstack")
    assert qi.circuit_status("clickstack")["state"] == "OPEN"
    assert qi.circuit_status("qa")["state"] == "CLOSED"


@pytest.mark.asyncio
async def test_client_connects_to_the_sources_endpoint(monkeypatch) -> None:
    _with_sources(monkeypatch)
    captured: dict[str, Any] = {}

    async def _fake(**kw):
        captured.update(kw)
        return object()

    import clickhouse_connect

    monkeypatch.setattr(clickhouse_connect, "get_async_client", _fake)
    await qi._get_client("clickstack")
    assert captured["host"] == "cs.example" and captured["database"] == "loom_qa"
    assert captured["username"] == "cs_ro" and captured["password"] == "s3cret"
    assert captured["client_name"] == "loom-tracker-live-clickstack"


@pytest.mark.asyncio
async def test_source_statements_name_the_sources_database_and_credential(monkeypatch) -> None:
    _with_sources(monkeypatch)
    calls: list[tuple[str, str]] = []

    async def _rows(sql, params=None, *, credential="qa", **kw):
        calls.append((" ".join(sql.split()), credential))
        return []

    monkeypatch.setattr(qi, "query_rows", _rows)
    s = src.TrackerSource(ORG, "tracker-clickstack")
    assert s.db == "loom_qa" and s.cfg is not None and s.cfg.credential == "clickstack"
    await s.items_by_number(ORG, "tracker-clickstack", [7])
    assert calls and all(c == "clickstack" for _, c in calls)
    assert all("loom_qa.github_" in sql for sql, _ in calls)
    assert all("is_private = 0" in sql for sql, _ in calls)
    # an unknown namespace has no source: the legacy database, and no serve
    u = src.TrackerSource(ORG, "tracker-orphan")
    assert u.db == "" and u.cfg is None  # No deployment's database as a default
    with pytest.raises(TrackerUnavailable):
        await u.items_by_number(ORG, "tracker-orphan", [7])


# ---------------------------------------------------------------- the contract


def _full_schema(**overrides: dict[str, str]) -> rc.Described:
    live = {
        "String": "LowCardinality(String)",
        "UInt": "UInt8",
        "DateTime": "DateTime64(3)",
        "UUID": "UUID",
        "Array(String)": "Array(LowCardinality(String))",
        "Array(Float)": "Array(Float32)",
    }
    out: rc.Described = {}
    for table, cols in {**rc.REQUIRED, **rc.OPTIONAL}.items():
        out[table] = {c: live[t] for c, t in cols.items()}
    for tables in rc.FEATURES.values():
        for table, cols in tables.items():
            out[table].update({c: live[t] for c, t in cols.items()})
    for table, cols in overrides.items():
        out[table] = cols
    return out


def test_type_families_are_lenient_in_shape_only() -> None:
    assert rc.family_ok("String", "LowCardinality(String)")
    assert rc.family_ok("String", "Nullable(String)") and rc.family_ok("String", "Enum8('a' = 1)")
    assert rc.family_ok("UInt", "Nullable(UInt8)") and rc.family_ok("UInt", "UInt32")
    assert rc.family_ok("DateTime", "DateTime64(3)") and rc.family_ok("DateTime", "Date")
    assert rc.family_ok("Array(String)", "Array(LowCardinality(String))")
    assert rc.family_ok("Array(Float)", "Array(Float32)")
    assert not rc.family_ok("UInt", "String") and not rc.family_ok("Array(String)", "String")
    assert not rc.family_ok("UUID", "String") and not rc.family_ok("DateTime", "UInt32")


def test_diff_names_what_is_missing_and_what_is_optional() -> None:
    rep = rc.diff("cs", "loom_qa", _full_schema())
    assert rep.ok and rep.summary() == "ok"
    assert set(rep.optional_present) == set(rc.OPTIONAL)
    assert rep.features == {name: True for name in rc.FEATURES}
    assert rep.provides("core_incidents") and rep.provides("github_issues")

    described = _full_schema()
    del described["edges"]
    del described["github_issues"]["error_code"]
    described["github_prs"]["is_merged"] = "String"
    del described["core_incidents"]
    del described["github_prs"]["embedding"]
    rep = rc.diff("cs", "loom_qa", described)
    assert not rep.ok
    assert rep.missing_tables == ["edges"]
    assert rep.missing_columns == {"github_issues": ["error_code"]}
    assert rep.type_mismatches == {"github_prs": {"is_merged": ["UInt", "String"]}}
    assert "core_incidents" not in rep.optional_present and not rep.provides("core_incidents")
    assert rep.features["vector"] is False and rep.features["bisect"] is True
    s = rep.summary()
    assert "missing tables edges" in s and "github_issues.{error_code}" in s
    assert "is_merged is String not UInt" in s
    assert rep.to_dict()["summary"] == s


def test_contract_covers_every_column_the_item_read_selects() -> None:
    """The item statements select these columns unconditionally: a source
    that lacks one fails at read time, so the contract must name them all."""
    for cols_sql, table in ((ISSUE_COLS, "github_issues"), (PR_COLS, "github_prs")):
        # the PR branch synthesises `labels` / `assignees` — not columns it reads
        text = re.sub(r"CAST\(\[\] AS Array\(String\)\) AS \w+,", "", cols_sql)
        names = {t.strip() for t in text.replace("\n", " ").split(",") if t.strip()}
        missing = names - set(rc.REQUIRED[table])
        assert not missing, f"{table}: {sorted(missing)}"


# ---------------------------------------------------------------- the gate


class _Schema:
    """A fake upstream answering the contract's schema statement."""

    def __init__(self, described: rc.Described) -> None:
        self.described = described
        self.calls = 0

    async def __call__(self, sql, params=None, *, credential="qa", **kw):
        self.calls += 1
        assert "system.columns" in sql and credential == "clickstack"
        assert params["db"] == "loom_qa"
        return [
            {"table": t, "name": c, "type": ty}
            for t, cols in self.described.items()
            for c, ty in cols.items()
        ]


@pytest.mark.asyncio
async def test_a_source_that_fails_the_contract_does_not_serve(monkeypatch) -> None:
    _with_sources(monkeypatch)
    monkeypatch.setattr(ct, "_enforce", True)
    described = _full_schema()
    del described["github_issues"]["state_reason"]
    up = _Schema(described)
    monkeypatch.setattr(qi, "query_rows", up)
    s = src.TrackerSource(ORG, "tracker-clickstack")
    with pytest.raises(TrackerUnavailable, match=r"contract: missing columns github_issues"):
        await s.items_by_number(ORG, "tracker-clickstack", [1])
    # cached: a second read does not re-describe
    with pytest.raises(TrackerUnavailable, match="contract"):
        await s.items_by_number(ORG, "tracker-clickstack", [2])
    assert up.calls == 1
    assert ct.provides(s.cfg, "core_incidents")  # the failed report still lists it


@pytest.mark.asyncio
async def test_a_source_that_meets_the_contract_serves_and_reports_optional_tables(monkeypatch):
    _with_sources(monkeypatch)
    monkeypatch.setattr(ct, "_enforce", True)
    described = _full_schema()
    del described["core_incidents"]
    seen: list[str] = []

    async def _rows(sql, params=None, *, credential="qa", **kw):
        seen.append(" ".join(sql.split()))
        if "system.columns" in sql:
            return [
                {"table": t, "name": c, "type": ty}
                for t, cols in described.items()
                for c, ty in cols.items()
            ]
        return []

    monkeypatch.setattr(qi, "query_rows", _rows)
    s = src.TrackerSource(ORG, "tracker-clickstack")
    assert await s.items_by_number(ORG, "tracker-clickstack", [1]) == {}
    assert seen[0].startswith("SELECT table, name, type FROM system.columns")
    assert any("loom_qa.github_issues" in q for q in seen[1:])
    assert ct.provides(s.cfg, "github_issues") and not ct.provides(s.cfg, "core_incidents")
    assert reports.cached("clickstack") is not None and reports.cached("qa") is None


@pytest.mark.asyncio
async def test_enforcement_off_means_no_schema_read(monkeypatch) -> None:
    _with_sources(monkeypatch)
    calls: list[str] = []

    async def _rows(sql, params=None, *, credential="qa", **kw):
        calls.append(sql)
        return []

    monkeypatch.setattr(qi, "query_rows", _rows)
    s = src.TrackerSource(ORG, "tracker-clickstack")
    await s.items_by_number(ORG, "tracker-clickstack", [1])
    assert calls and not any("system.columns" in q for q in calls)
    assert ct.provides(s.cfg, "core_incidents")


@pytest.mark.asyncio
async def test_describe_falls_back_to_describe_table_when_the_catalogue_is_hidden(monkeypatch):
    _with_sources(monkeypatch)
    calls: list[str] = []

    async def _rows(sql, params=None, *, credential="qa", **kw):
        calls.append(" ".join(sql.split()))
        if "system.columns" in sql:
            return []
        if "DESCRIBE TABLE loom_qa.edges" in sql:
            raise RuntimeError("UNKNOWN_TABLE")
        return [{"name": "source_id", "type": "String"}]

    monkeypatch.setattr(qi, "query_rows", _rows)
    described = await ct.describe("clickstack", "loom_qa")
    assert set(described) == {"github_issues", "github_prs"}
    assert described["github_issues"] == {"source_id": "String"}
    assert sum(q.startswith("DESCRIBE TABLE") for q in calls) == 3


# ---------------------------------------------------------------- the legs


@pytest.mark.asyncio
async def test_frames_leg_says_not_provided_when_the_source_has_no_incidents(monkeypatch):
    from server.tracker.app.similar import _similar_frames_leg
    from server.tracker.domain import similar as sim

    _with_sources(monkeypatch)
    monkeypatch.setattr(ct, "_enforce", True)
    described = _full_schema()
    del described["core_incidents"]
    reports.mark_for_tests("clickstack", rc.diff("clickstack", "loom_qa", described))

    class _Src:
        cfg = qn.configured_namespaces()["tracker-stack-private"]

        async def similar_frames(self, *a, **kw):
            return []

    class _In:
        org, namespace = ORG, "tracker-stack-private"

    q = sim.Query(frames=sim.normalise_frames(["DB::MergeTreeData::loadDataParts"]))
    status: dict[str, str] = {}
    out = await _similar_frames_leg(_Src(), _In(), q, None, ["issue", "incident"], None, "", status)
    assert out == [] and status["frames_incident"] == "not_provided"
    assert status["frames"] == "empty"
