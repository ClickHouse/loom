"""General tracker discovery reads persisted repository links before authorizing."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app.loom_search.tracker_leg import prepare_tracker_search
from server.settings import settings
from server.tracker.app import binding
from tests.unit.clock_fake import ManualClock

ORG = "acme"
CODE = "code-project"
ROOT = "code-project-root"
PUBLIC = "tracker-project"
RESTRICTED = "tracker-restricted"


@pytest.fixture
def stored_links(monkeypatch):
    clock = ManualClock(t=1000.0)
    values = {binding.OVERRIDE_KEY: {}, binding.BRANCH_OF_KEY: {}}

    class Repository:
        async def state_values_for_keys(self, keys):
            assert keys == [binding.OVERRIDE_KEY, binding.BRANCH_OF_KEY]
            return deepcopy(values)

    async def kinds(org):
        assert org == ORG
        return {CODE: "code"}

    async def readable(org, namespace, role=None, *, hides_answer=True):
        return org == ORG and namespace == CODE

    targets = {
        PUBLIC: SimpleNamespace(restricted=False),
        RESTRICTED: SimpleNamespace(restricted=True),
    }
    monkeypatch.setattr(binding, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(binding, "_OVERRIDES", {})
    monkeypatch.setattr(binding, "_BRANCH_OF", {})
    monkeypatch.setattr(binding, "_OVERRIDES_AT", -float("inf"))
    monkeypatch.setattr(binding, "_AFTER_WRITE", False)
    monkeypatch.setattr(binding, "operator_repositories", lambda: frozenset({(ORG, ROOT)}))
    monkeypatch.setattr(binding.repos, "registry_namespaces", lambda: {(ORG, CODE)})
    monkeypatch.setattr(settings, "code_repos_refresh_seconds", 1)
    monkeypatch.setattr(settings, "tracker_code_namespace_bindings_json", "{}")
    monkeypatch.setattr("server.code.app.reads.code_index", Repository)
    monkeypatch.setattr("server.core.ch.get_client", lambda: SimpleNamespace(_client=object()))
    monkeypatch.setattr("server.auth.authz.row_in_scope", readable)
    monkeypatch.setattr("server.identity.app.namespace_kinds.kind_map", kinds)
    monkeypatch.setattr("server.code.domain.scope.current_code_scope", lambda: None)
    monkeypatch.setattr("server.identity.app.request_scope.request_scope", lambda: WHOLE_NAMESPACE)
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces", lambda: dict(targets)
    )
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.tracker_namespace",
        lambda org, namespace: targets.get(namespace) if org == ORG else None,
    )
    return values, clock


@pytest.mark.parametrize("link_kind", ["override", "branch"])
async def test_cold_persisted_links_open_only_the_standard_tracker(
    stored_links, monkeypatch, link_kind
):
    values, _ = stored_links
    if link_kind == "override":
        values[binding.OVERRIDE_KEY][ORG, CODE] = f"{PUBLIC},{RESTRICTED}"
    else:
        values[binding.BRANCH_OF_KEY][ORG, CODE] = ROOT
        monkeypatch.setattr(
            settings,
            "tracker_code_namespace_bindings_json",
            json.dumps({ROOT: [PUBLIC, RESTRICTED]}),
        )

    plan = await prepare_tracker_search("retention decision", org=ORG)

    assert plan is not None
    assert plan.namespaces == (PUBLIC,)


async def test_cleared_persisted_link_closes_the_warm_discovery_cache(stored_links):
    values, clock = stored_links
    values[binding.OVERRIDE_KEY][ORG, CODE] = PUBLIC
    await binding.refresh_overrides()
    plan = await prepare_tracker_search("retention decision", org=ORG)
    assert plan is not None and plan.namespaces == (PUBLIC,)

    values[binding.OVERRIDE_KEY][ORG, CODE] = ""
    clock.advance(2)

    assert await prepare_tracker_search("retention decision", org=ORG) is None


async def test_another_orgs_persisted_link_opens_nothing(stored_links):
    values, _ = stored_links
    values[binding.OVERRIDE_KEY]["other-org", CODE] = PUBLIC

    assert await prepare_tracker_search("retention decision", org=ORG) is None


async def test_persisted_binding_read_obeys_the_discovery_deadline(stored_links, monkeypatch):
    cancelled = asyncio.Event()

    async def blocked_read(keys):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        "server.code.app.reads.code_index",
        lambda: SimpleNamespace(state_values_for_keys=blocked_read),
    )

    assert await prepare_tracker_search("retention decision", org=ORG, timeout_s=0.01) is None
    assert cancelled.is_set()
