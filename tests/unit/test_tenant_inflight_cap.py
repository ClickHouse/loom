"""One org may have at most its share of LOOM_TENANT_MAX_CONCURRENT_REQUESTS
running in a process: the slot is taken around the handler and released
however the handler ends."""

from __future__ import annotations

import pytest

from server.auth.jwt import AuthInfo
from server.core import tenant_slots
from server.core.errors import RateLimitedError
from server.operations.base import Role
from server.settings import settings
from server.transport import gates

_TARGET = gates.Target(org="acme", namespace="n", named_namespace="n", register_namespace="n")


def _auth(org: str = "acme") -> AuthInfo:
    return AuthInfo(
        subject="alice", org=org, namespaces=("n",), roles=(Role.VIEWER,), is_service_account=False
    )


@pytest.fixture(autouse=True)
def _one_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tenant_max_concurrent_requests", 2)
    monkeypatch.setattr(settings, "replica_count", 1)
    monkeypatch.setattr(settings, "web_workers", 1)
    monkeypatch.setattr(tenant_slots, "_IN_FLIGHT", {})


def test_admits_the_cap_and_refuses_one_more() -> None:
    with gates.tenant_slot(_auth(), _TARGET), gates.tenant_slot(_auth(), _TARGET):
        with pytest.raises(RateLimitedError) as caught:
            with gates.tenant_slot(_auth(), _TARGET):
                pass
        assert caught.value.status_code == 429 and caught.value.code == "RATE_LIMITED"
        assert caught.value.extra["retry_after_s"] == 1
        # another org has its own slots
        with gates.tenant_slot(_auth("other"), _TARGET):
            pass
    assert tenant_slots._IN_FLIGHT == {}


def test_a_failing_handler_releases_its_slot() -> None:
    for _ in range(3):
        with pytest.raises(ValueError), gates.tenant_slot(_auth(), _TARGET):
            raise ValueError("handler failed")
    assert tenant_slots._IN_FLIGHT == {}


def test_the_org_is_the_tokens_not_the_requests() -> None:
    foreign = gates.Target(org="victim", namespace="n", named_namespace="n", register_namespace="n")
    with gates.tenant_slot(_auth(), foreign):
        assert tenant_slots._IN_FLIGHT == {"acme": 1}


def test_the_cap_is_shared_across_replicas_and_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tenant_max_concurrent_requests", 64)
    monkeypatch.setattr(settings, "replica_count", 2)
    monkeypatch.setattr(settings, "web_workers", 4)
    assert tenant_slots.inflight_cap() == 8
    monkeypatch.setattr(settings, "tenant_max_concurrent_requests", 3)
    assert tenant_slots.inflight_cap() == 1  # never below one


def test_zero_turns_the_cap_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tenant_max_concurrent_requests", 0)
    with gates.tenant_slot(_auth(), _TARGET), gates.tenant_slot(_auth(), _TARGET):
        with gates.tenant_slot(_auth(), _TARGET):
            pass
    assert tenant_slots._IN_FLIGHT == {}
