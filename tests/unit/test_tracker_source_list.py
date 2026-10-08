"""`tracker.source.list` — every tracker source of an org with its
contract verdict, breaker, freshness and the tracker namespaces reading it, each
with the directory row's effective join rule and the caller's standing.
Org-level, viewer role, no secret on the wire."""

from __future__ import annotations

import json
from typing import Any

import pytest

import server.tracker.app.contract as ct
import server.tracker.domain.sources as qs
import server.tracker.store.source as src
import server.tracker.store.source.qa_intel as qi
from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo, Role
from server.settings import settings
from server.tracker.api.models import TrackerSourceListInput
from server.tracker.api.ops import tracker_source_list
from server.tracker.app.sources import handle_tracker_source_list
from server.tracker.domain import contract as rc
from server.tracker.store.source import contract_reports as reports
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG = "clickhouse"
PUB, PRIV = "tracker-clickhouse", "tracker-example-private"
REPO, PREPO = "ClickHouse/ClickHouse", "ClickHouse/example-private"
CS = {"clickstack": {"host": "cs.example", "database": "loom_qa"}}
CS_NS = {"tracker-clickstack": {"source": "clickstack", "repos": ["ClickHouse/clickstack"]}}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    reports.reset_for_tests()
    qs.reset_warnings_for_tests()
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(
            {REPO: PUB, PREPO: PRIV}, restricted=PRIV, extra_sources=CS, extra_namespaces=CS_NS
        ),
    )
    # the `qa` source has a credential; `clickstack` is declared but has none yet
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(settings, "tracker_live_serve", True)
    yield
    qi.reset_for_tests()
    src.reset_cache_for_tests()
    reports.reset_for_tests()


class _Ch:
    """loom's own ClickHouse: the directory rows and the caller's grants."""

    def __init__(self, rows: list[dict[str, Any]], grants: list[dict[str, Any]]) -> None:
        self.rows, self.grants, self.calls = rows, grants, []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        if "FROM loom.namespaces" in sql:
            return [r for r in self.rows if r["namespace"] in params["names"]]
        if "FROM loom.namespace_grants" in sql:
            return [g for g in self.grants if g["namespace"] in params["names"]]
        raise AssertionError(sql)


def _auth(subject: str = "alice") -> AuthInfo:
    return AuthInfo(
        subject=subject, org=ORG, namespaces=(PUB,), roles=(Role.VIEWER,), is_service_account=False
    )


ROWS = [
    {
        "namespace": PUB,
        "visibility": "private",
        "join_policy": "open",
        "requires_approval": False,
        "classification": "standard",
        "kind": "tracker",
        "active": True,
    },
    {
        "namespace": PRIV,
        "visibility": "private",
        "join_policy": "inherit",
        "requires_approval": False,
        "classification": "restricted",
        "kind": "tracker",
        "active": True,
    },
]
GRANTS = [
    {"namespace": PUB, "role": "viewer", "status": "active", "pending_role": ""},
    {"namespace": PRIV, "role": "viewer", "status": "pending", "pending_role": "viewer"},
]


async def _upstream(sql, params=None, *, credential="qa", **kw):
    """The default source: the freshness counts."""
    assert credential == "qa"
    if "github_issues" in sql:
        return [{"n": 42452, "newest": "2026-09-16 15:00:00"}]
    return [{"n": 190889, "newest": "2026-09-16 16:00:00"}]


@pytest.mark.asyncio
async def test_list_names_every_source_with_its_namespaces_rule_and_standing(monkeypatch) -> None:
    ch = _Ch(ROWS, GRANTS)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(qi, "query_rows", _upstream)
    reports.mark_for_tests("qa", rc.diff("qa", "qa_intelligence", {}))  # a cached (failing) verdict
    token = current_auth_var.set(_auth())
    try:
        out = await handle_tracker_source_list(TrackerSourceListInput(org=ORG))
    finally:
        current_auth_var.reset(token)
    by = {s["name"]: s for s in out.sources}
    assert set(by) == {"qa", "clickstack"}

    qa = by["qa"]
    assert qa["host"] == "qa.example" and qa["database"] == "qa_intelligence" and qa["configured"]
    assert qa["origin"] == "config" and by["clickstack"]["origin"] == "config"
    assert "user" not in json.dumps(out.model_dump()) or '"user"' not in json.dumps(qa)
    assert qa["contract"]["ok"] is False and "missing tables" in qa["contract"]["summary"]
    assert qa["breaker"]["state"] == "CLOSED"
    assert qa["freshness"] == {
        "issues": 42452,
        "prs": 190889,
        "last_refresh_at": "2026-09-16 16:00:00",
    }
    ns = {v["namespace"]: v for v in qa["namespaces"]}
    assert set(ns) == {PUB, PRIV}
    assert ns[PUB]["join_policy"] == "open" and ns[PUB]["kind"] == "tracker"
    assert ns[PUB]["visibility"] == "private" and ns[PUB]["classification"] == "standard"
    assert ns[PUB]["member_role"] == "viewer" and ns[PUB]["pending_role"] == ""
    assert ns[PUB]["repos"] == [REPO] and ns[PUB]["registered"] is True
    # inherit + private resolves to approval; the caller's request is pending
    assert ns[PRIV]["join_policy"] == "approval" and ns[PRIV]["classification"] == "restricted"
    assert ns[PRIV]["member_role"] == "" and ns[PRIV]["pending_role"] == "viewer"

    cs = by["clickstack"]
    assert cs["host"] == "cs.example" and cs["configured"] is False
    assert cs["contract"] == {
        "ok": None,
        "summary": "unchecked",
        "checked_at": "",
        "optional_present": [],
        "features": {},
    }
    assert cs["breaker"]["state"] == "unconfigured" and cs["freshness"] == {}
    (only,) = cs["namespaces"]
    # no directory row yet: the map says what the leader will write
    assert only["registered"] is False and only["join_policy"] == "open"
    assert only["member_role"] == "" and only["pending_role"] == ""
    # the directory was asked once for all namespaces, the grants once for the caller
    # The alias pairing adds one directory read; the grant
    # read itself carries the caller under every spelling
    grant_calls = [c for c in ch.calls if "namespace_grants" in c[0]]
    assert len(grant_calls) == 1 and grant_calls[0][1]["subs"] == ["alice"]


@pytest.mark.asyncio
async def test_list_refuses_another_org_and_the_op_is_org_level_viewer(monkeypatch) -> None:
    from server.core.errors import AuthInsufficientError

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Ch([], []))
    token = current_auth_var.set(
        AuthInfo(
            subject="bob",
            org="other",
            namespaces=("x",),
            is_service_account=False,
            roles=(Role.VIEWER,),
        )
    )
    try:
        with pytest.raises(AuthInsufficientError):
            await handle_tracker_source_list(TrackerSourceListInput(org=ORG))
    finally:
        current_auth_var.reset(token)
    assert (
        tracker_source_list.required_role == Role.VIEWER
        and tracker_source_list.skip_namespace_authz
    )
    assert tracker_source_list.idempotent and tracker_source_list.maturity == "experimental"


@pytest.mark.asyncio
async def test_an_org_that_is_not_the_tracker_org_sees_and_checks_no_source(monkeypatch) -> None:
    """Sources and their namespaces are the tracker org's: a principal of another org,
    acting in its own org, lists nothing (no host, database or mapping) and cannot run
    a live contract check against a source."""
    from server.core.errors import ValidationError
    from server.tracker.api.models import TrackerSourceCheckInput
    from server.tracker.app.sources import handle_tracker_source_check

    def _no_reads():
        raise AssertionError("another org's listing must not read anything")

    monkeypatch.setattr("server.core.ch.get_client", _no_reads)
    token = current_auth_var.set(
        AuthInfo(
            subject="bob",
            org="acme",
            namespaces=("x",),
            is_service_account=False,
            roles=(Role.VIEWER,),
        )
    )
    try:
        out = await handle_tracker_source_list(TrackerSourceListInput(org="acme"))
        assert out.sources == []
        with pytest.raises(ValidationError):
            await handle_tracker_source_check(TrackerSourceCheckInput(org="acme", name="qa"))
    finally:
        current_auth_var.reset(token)


@pytest.mark.asyncio
async def test_contract_and_freshness_that_do_not_answer_are_statuses(monkeypatch) -> None:
    monkeypatch.setattr("server.core.ch.get_client", lambda: _Ch(ROWS, []))
    monkeypatch.setattr(ct, "_enforce", True)

    async def _down(sql, params=None, *, credential="qa", **kw):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(qi, "query_rows", _down)
    token = current_auth_var.set(_auth())
    try:
        out = await handle_tracker_source_list(TrackerSourceListInput(org=ORG))
    finally:
        current_auth_var.reset(token)
    qa = next(s for s in out.sources if s["name"] == "qa")
    assert qa["contract"]["summary"] == "unchecked" and qa["contract"]["ok"] is None
    assert qa["freshness"] == {}
    assert {v["namespace"] for v in qa["namespaces"]} == {PUB, PRIV}


@pytest.mark.asyncio
async def test_item_counts_carry_the_privacy_predicate(monkeypatch) -> None:
    seen: list[str] = []

    async def _rows(sql, params=None, *, credential="qa", **kw):
        seen.append(" ".join(sql.split()))
        return [{"n": 3, "newest": "2026-09-16 12:00:00"}]

    monkeypatch.setattr(qi, "query_rows", _rows)
    out = await src.TrackerSource(ORG, PUB).item_counts(ORG, PUB)
    assert out == {"issues": 3, "prs": 3, "last_refresh_at": "2026-09-16 12:00:00"}
    assert len(seen) == 2 and all("WHERE is_private = 0 AND 1" in q for q in seen)
    assert any("qa_intelligence.github_issues FINAL" in q for q in seen)
