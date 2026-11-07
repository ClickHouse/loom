"""The code→tracker namespace binding: exact key, then glob; the
bound namespace passes the gate for the current principal or is not read."""

from __future__ import annotations

import json

import pytest

import server.auth.authz as authz
import server.tracker.app.binding as b
from server.settings import settings

ORG = "clickhouse"


@pytest.fixture
def bound(monkeypatch):
    monkeypatch.setattr(
        settings,
        "tracker_code_namespace_bindings_json",
        '{"code-clickhouse*": "tracker-clickhouse", "code-example-private": "tracker-priv"}',
    )
    # the operator's map links only the repositories the operator configured
    names = ("code-clickhouse", "code-clickhouse-26-4", "code-example-private", "code-other")
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG, "namespace": n, "repo": "ClickHouse/ClickHouse"} for n in names]),
    )


def test_exact_key_beats_glob(bound) -> None:
    assert b.tracker_namespace_for(ORG, "code-example-private") == "tracker-priv"
    assert b.tracker_namespace_for(ORG, "code-clickhouse") == "tracker-clickhouse"
    assert b.tracker_namespace_for(ORG, "code-clickhouse-26-4") == "tracker-clickhouse"
    assert b.tracker_namespace_for(ORG, "code-other") == ""
    assert b.tracker_namespace_for(ORG, "") == ""


def test_unset_or_bad_json_binds_nothing(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tracker_code_namespace_bindings_json", "")
    assert b.tracker_namespace_for(ORG, "code-clickhouse") == ""
    monkeypatch.setattr(settings, "tracker_code_namespace_bindings_json", "{oops")
    assert b.bindings() == {}


@pytest.mark.asyncio
async def test_authorized_binding_runs_the_gate(bound, monkeypatch) -> None:
    seen = []

    async def _scope(org, ns, role, *, hides_answer=True):
        seen.append((org, ns, role.name))
        return ns == "tracker-clickhouse"

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    assert (
        await b.authorized_tracker_namespace("clickhouse", "code-clickhouse-26-4")
        == "tracker-clickhouse"
    )
    assert await b.authorized_tracker_namespace("clickhouse", "code-example-private") == ""
    assert seen == [
        ("clickhouse", "tracker-clickhouse", "VIEWER"),
        ("clickhouse", "tracker-priv", "VIEWER"),
    ]
    assert await b.authorized_tracker_namespace("clickhouse", "code-other") == ""


@pytest.mark.asyncio
async def test_gate_failure_is_closed(bound, monkeypatch) -> None:
    async def _boom(org, ns, role, *, hides_answer=True):
        raise RuntimeError("down")

    monkeypatch.setattr(authz, "row_in_scope", _boom)
    assert await b.authorized_tracker_namespace("clickhouse", "code-clickhouse") == ""


# --- which gate ---------------------------------------------------------


@pytest.fixture
def tracker_sources(monkeypatch):
    from tests.unit.test_tracker_binding_op import tracker_credentials_json, tracker_sources_json

    monkeypatch.setattr(settings, "tracker_org", "clickhouse")
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(
            '{"ClickHouse/ClickHouse": "tracker-clickhouse", "ClickHouse/example-private": "tracker-priv"}',
            restricted="tracker-priv",
        ),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())


@pytest.mark.asyncio
async def test_standard_target_is_read_by_whoever_reads_the_code_namespace(
    bound, tracker_sources, monkeypatch
) -> None:
    """A customer service account scoped to its own namespaces reads the
    repository through its creator's grant but can never join or be
    granted the tracker namespace as a scoped token; the public QA facts follow
    the repository's read right instead."""
    seen = []

    async def _scope(org, ns, role, *, hides_answer=True):
        seen.append(ns)
        return ns.startswith("code-")  # may read the repository, holds nothing on qa-*

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    assert (
        await b.authorized_tracker_namespace("clickhouse", "code-clickhouse-26-4")
        == "tracker-clickhouse"
    )
    assert seen == ["code-clickhouse-26-4"]


@pytest.mark.asyncio
async def test_restricted_target_keeps_its_own_two_factor_gate(
    bound, tracker_sources, monkeypatch
) -> None:
    seen = []

    async def _scope(org, ns, role, *, hides_answer=True):
        seen.append(ns)
        return ns.startswith("code-")

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    assert await b.authorized_tracker_namespace("clickhouse", "code-example-private") == ""
    assert seen == ["tracker-priv"], (
        "restricted: the gate is on the tracker namespace, and the caller has nothing there"
    )


@pytest.mark.asyncio
async def test_unknown_target_is_gated_strictly_on_the_tracker_namespace(
    bound, monkeypatch
) -> None:
    """No tracker source map at all: the plane cannot say the target is standard,
    so the gate stays on the tracker namespace (fail closed)."""
    seen = []

    async def _scope(org, ns, role, *, hides_answer=True):
        seen.append(ns)
        return False

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    assert await b.authorized_tracker_namespace("clickhouse", "code-clickhouse") == ""
    assert seen == ["tracker-clickhouse"]
