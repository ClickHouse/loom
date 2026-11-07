"""tracker.binding: the tracker plane tells the dashboard what a
namespace may ask. A tracker namespace answers itself; a code namespace answers
the tracker namespace bound to it after the gate; unbound, refused and
not-a-QA-namespace are one `none` answer that names nothing."""

from __future__ import annotations

import json

import pytest

import server.auth.authz as authz
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings
from server.tracker.api.models import TrackerBindingInput
from server.tracker.app import handlers as h
from server.tracker.app.binding import resolve_binding
from server.tracker.domain import catalogue as cat
from tests.unit._tracker_config import tracker_credentials_json, tracker_sources_json

ORG = "clickhouse"
PUB, PRIV = "tracker-clickhouse", "tracker-example-private"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(settings, "tracker_org", ORG)
    monkeypatch.setattr(
        settings,
        "tracker_sources_json",
        tracker_sources_json(
            f'{{"ClickHouse/ClickHouse": "{PUB}", "ClickHouse/example-private": "{PRIV}"}}',
            restricted=PRIV,
        ),
    )
    monkeypatch.setattr(settings, "tracker_credentials_json", tracker_credentials_json())
    monkeypatch.setattr(
        settings,
        "tracker_code_namespace_bindings_json",
        f'{{"code-clickhouse*": "{PUB}", "code-example-private": "{PRIV}",'
        f' "code-orphan": "tracker-nowhere"}}',
    )
    # the operator's map links the repositories the operator configured
    names = ("code-clickhouse", "code-clickhouse-26-4", "code-example-private", "code-orphan")
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG, "namespace": n, "repo": "ClickHouse/ClickHouse"} for n in names]),
    )


@pytest.fixture
def gate(monkeypatch):
    allowed: set[str] = set()
    seen: list[tuple[str, str, str]] = []

    async def _scope(org, ns, role, *, hides_answer=True):
        seen.append((org, ns, role.name))
        return ns in allowed

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    return allowed, seen


PUBLIC_TEMPLATES = [t.name for t in cat.TEMPLATES.values() if t.access == cat.PUBLIC]
ALL_TEMPLATES = list(cat.TEMPLATES)


@pytest.mark.asyncio
async def test_a_tracker_namespace_answers_itself_without_the_gate(gate) -> None:
    allowed, seen = gate
    out = await resolve_binding(ORG, PUB)
    assert out == {
        "tracker_namespace": PUB,
        "restricted": False,
        "kind": "tracker",
        "authorized": True,
        "templates": PUBLIC_TEMPLATES,
    }
    assert "severity_board" in out["templates"] and "area_incidents" not in out["templates"]
    out = await resolve_binding(ORG, PRIV)
    assert out["kind"] == "tracker" and out["restricted"] and out["templates"] == ALL_TEMPLATES
    assert seen == []  # the transport authorized the addressed namespace already


@pytest.mark.asyncio
async def test_a_bound_code_namespace_answers_its_target_after_the_gate(gate) -> None:
    allowed, seen = gate
    # A STANDARD target is read by whoever reads the code
    # namespace — the gate runs on the code namespace, not on the QA one
    allowed.add("code-clickhouse-26-4")
    out = await resolve_binding(ORG, "code-clickhouse-26-4")
    assert out["tracker_namespace"] == PUB and out["kind"] == "code" and out["authorized"]
    assert not out["restricted"] and out["templates"] == PUBLIC_TEMPLATES
    assert seen == [(ORG, "code-clickhouse-26-4", "VIEWER")]
    # a RESTRICTED target keeps its own gate
    allowed.add(PRIV)
    out = await resolve_binding(ORG, "code-example-private")
    assert (
        out["tracker_namespace"] == PRIV and out["restricted"] and out["templates"] == ALL_TEMPLATES
    )


@pytest.mark.asyncio
async def test_a_refused_binding_is_indistinguishable_from_an_unbound_one(gate) -> None:
    allowed, seen = gate  # nothing allowed: the restricted target is refused
    refused = await resolve_binding(ORG, "code-example-private")
    unbound = await resolve_binding(ORG, "clickgap-public")
    orphan = await resolve_binding(ORG, "code-orphan")  # bound to a name the plane does not serve
    none = {
        "tracker_namespace": "",
        "restricted": False,
        "kind": "none",
        "authorized": False,
        "templates": [],
    }
    assert refused == unbound == none
    assert orphan == none
    # nothing in the refused answer names the restricted namespace
    assert PRIV not in repr(refused) and "private" not in repr(refused)
    assert seen == [(ORG, PRIV, "VIEWER")]  # the unbound names never reached the gate


@pytest.mark.asyncio
async def test_a_failing_gate_is_closed_and_still_names_nothing(monkeypatch) -> None:
    async def _boom(org, ns, role, *, hides_answer=True):
        raise RuntimeError(f"gate down for {ns}")

    monkeypatch.setattr(authz, "row_in_scope", _boom)
    out = await resolve_binding(ORG, "code-example-private")
    assert out["kind"] == "none" and not out["authorized"] and out["tracker_namespace"] == ""
    assert PRIV not in repr(out)


@pytest.mark.asyncio
async def test_another_org_is_none(gate) -> None:
    allowed, _ = gate
    allowed.add(PUB)
    assert (await resolve_binding("other-org", PUB))["kind"] == "none"


@pytest.mark.asyncio
async def test_handler_and_registration(gate) -> None:
    allowed, _ = gate
    allowed.add("code-clickhouse")  # The code namespace's read right
    out = await h.handle_tracker_binding(TrackerBindingInput(org=ORG, namespace="code-clickhouse"))
    d = out.model_dump()
    assert d["tracker_namespace"] == PUB and d["kind"] == "code" and d["authorized"]
    assert d["templates"] == PUBLIC_TEMPLATES
    op = OPERATIONS_BY_NAME["tracker.binding"]
    assert op.required_role == Role.VIEWER and op.maturity == "experimental" and op.idempotent
    assert op.budget_p99_ms <= 50 and not op.skip_namespace_authz
    assert "namespace" in op.input_model.model_fields  # the addressed namespace is the authz target


def test_template_names_follow_the_restricted_flag() -> None:
    pub = cat.template_names(False)
    assert pub == PUBLIC_TEMPLATES and "severity_board" in pub
    assert all(cat.TEMPLATES[n].access == cat.PUBLIC for n in pub)
    assert cat.template_names(True) == ALL_TEMPLATES
