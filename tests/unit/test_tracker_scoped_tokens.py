"""The org-level tracker reads admit a scoped (ns_roles) token — the shape
every plugin and SDK token holds — for `tracker.contract`,
`tracker.source.list` and `tracker.source.check`, with or without a
namespace in the body, although their inputs carry none to authorize on
(the transport must not answer IDENTITY_INCOMPLETE).
The writes stay closed to a scoped token: a delegation for one namespace
does not register, rotate or retire an org's tracker."""

from __future__ import annotations

import pytest

from server.auth.authz import check_authorized
from server.auth.jwt import AuthInfo
from server.core.errors import IdentityIncompleteError
from server.operations.base import Role
from server.operations.registry import ALL_OPERATIONS

READS = ("tracker.contract", "tracker.source.list", "tracker.source.check")
WRITES = ("tracker.source.add", "tracker.source.rotate", "tracker.source.remove")


def _op(name: str):  # type: ignore[no-untyped-def]
    return next(op for op in ALL_OPERATIONS if op.name == name)


def _scoped(role: str = "viewer") -> AuthInfo:
    return AuthInfo(
        subject="clickgap-private",
        org="clickhouse",
        namespaces=["code-clickhouse"],
        roles=(),
        is_service_account=True,
        ns_roles=(("code-clickhouse", role),),
    )


@pytest.mark.parametrize("name", READS)
async def test_a_scoped_token_reads_the_orgs_trackers(name: str) -> None:
    op = _op(name)
    assert op.deployment_scoped and op.skip_namespace_authz and op.required_role == Role.VIEWER
    await check_authorized(
        _scoped(),
        op.required_role,
        org="clickhouse",
        namespace=None,
        deployment_scoped=op.deployment_scoped,
    )


@pytest.mark.parametrize("name", WRITES)
async def test_a_scoped_token_does_not_add_rotate_or_retire_one(name: str) -> None:
    op = _op(name)
    assert not op.deployment_scoped
    with pytest.raises(IdentityIncompleteError):
        await check_authorized(
            _scoped("admin"),
            op.required_role,
            org="clickhouse",
            namespace=None,
            deployment_scoped=op.deployment_scoped,
        )


async def test_the_listing_refreshes_the_registry_and_fresh_forces_a_consistent_read(
    monkeypatch,
) -> None:
    """A tracker added on another process shows on the next listing: the
    listing runs the TTL refresh, and `fresh` (the page right after its own
    add) forces it; a forced refresh reads with sequential consistency."""
    from server.core.ch import sequential_reads_active
    from server.tracker.api.models import TrackerSourceListInput
    from server.tracker.app import registry_sources
    from server.tracker.app import sources as h

    seen: list[tuple[bool, bool]] = []

    class _Ch:
        async def query_all(self, sql, params=None):
            if "qa_sources" in sql:
                seen.append(("FINAL" in sql, sequential_reads_active()))
            return []

    monkeypatch.setattr("server.core.ch.get_client", lambda: _Ch())
    monkeypatch.setattr(registry_sources.settings, "tracker_self_service_enabled", True)
    monkeypatch.setattr(registry_sources.settings, "tracker_org", "clickhouse")
    monkeypatch.setattr(h, "current_caller", lambda: ("", None))
    registry_sources.reset_for_tests()
    await h.handle_tracker_source_list(TrackerSourceListInput(org="clickhouse"))
    assert seen == [(True, False)]  # the TTL refresh: a plain read
    await h.handle_tracker_source_list(TrackerSourceListInput(org="clickhouse"))
    assert len(seen) == 1  # within the TTL: no second read
    await h.handle_tracker_source_list(TrackerSourceListInput(org="clickhouse", fresh=True))
    assert seen[-1] == (True, True)  # forced, read-your-writes
    registry_sources.reset_for_tests()
