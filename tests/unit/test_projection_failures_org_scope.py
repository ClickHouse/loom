"""The projection-failure ops read and change only namespaces in the caller's org scope.

`ops.projection_failures.*` skip the transport's namespace gate (an admin lists a whole
org), so the handlers narrow every statement by `resolve_org_scope`: a restricted
namespace the caller holds no grant and claim for stays out, for org and platform
admins too (docs/architecture/security.md, restricted namespaces). CH-free.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.auth.jwt import AuthInfo
from server.operations.base import Role
from tests.unit.ch_fake import RecordingCH


@pytest.fixture
def org_admin_with_hidden(monkeypatch: pytest.MonkeyPatch) -> RecordingCH:
    import server.auth.context as ctx

    admin = AuthInfo(
        subject="ops",
        org="dev",
        namespaces=("own-ns",),
        roles=(Role.ORG_ADMIN,),
        is_service_account=False,
    )
    monkeypatch.setattr(ctx, "current_auth", lambda: admin)

    async def hidden(org: str) -> list[str]:
        return ["restricted-ns"]

    async def readable_collections(
        org: str, column: str = "namespace"
    ) -> tuple[str, dict[str, Any]]:
        return " AND (NOT startsWith(namespace, {col_prefix:String}))", {"col_prefix": "col-"}

    monkeypatch.setattr(ctx, "restricted_namespaces_hidden_from", hidden)
    monkeypatch.setattr(ctx, "collection_clause", readable_collections)
    return (
        RecordingCH()
        .on("FROM loom.memory_projection_failures", [])
        .install(monkeypatch, "server.memory.app.projection_failures")
    )


def _excludes_restricted(sql: str, params: dict[str, Any]) -> bool:
    """The statement leaves out the hidden restricted namespace and the collections the
    caller cannot read."""
    return (
        "namespace NOT IN {ns_hidden:Array(String)}" in sql
        and params["ns_hidden"] == ["restricted-ns"]
        and "startsWith(namespace, {col_prefix:String})" in sql
        and params["col_prefix"] == "col-"
    )


@pytest.mark.asyncio
async def test_list_leaves_out_a_restricted_namespace(org_admin_with_hidden: RecordingCH) -> None:
    from server.insights.api.models_ops import ProjectionFailureListInput
    from server.insights.app.ops import handle_projection_failures_list

    await handle_projection_failures_list(
        ProjectionFailureListInput(org="dev", namespace="restricted-ns")
    )
    ((sql, params),) = [(c.sql, c.params) for c in org_admin_with_hidden.calls]
    assert _excludes_restricted(sql, params)
    assert params["ns"] == "restricted-ns"


@pytest.mark.asyncio
async def test_retry_reads_only_the_scope(org_admin_with_hidden: RecordingCH) -> None:
    from server.insights.api.models_ops import ProjectionFailureRetryInput
    from server.insights.app.ops import handle_projection_failures_retry

    out = await handle_projection_failures_retry(ProjectionFailureRetryInput(org="dev"))
    assert out.results == []
    ((sql, params),) = [(c.sql, c.params) for c in org_admin_with_hidden.calls]
    assert _excludes_restricted(sql, params)


@pytest.mark.asyncio
async def test_resolve_changes_only_the_scope(org_admin_with_hidden: RecordingCH) -> None:
    from server.insights.api.models_ops import ProjectionFailureResolveInput
    from server.insights.app.ops import handle_projection_failures_resolve

    await handle_projection_failures_resolve(
        ProjectionFailureResolveInput(
            org="dev", failure_ids=["7d1b0c52-0000-4000-8000-000000000001"]
        )
    )
    ((sql, params),) = [(c.sql, c.params) for c in org_admin_with_hidden.calls]
    assert sql.lstrip().startswith("ALTER TABLE loom.memory_projection_failures")
    assert _excludes_restricted(sql, params)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["list", "retry", "resolve"])
async def test_an_unreadable_classification_reads_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, org_admin_with_hidden: RecordingCH, op: str
) -> None:
    import server.auth.context as ctx
    from server.insights.api import models_ops as m
    from server.insights.app import ops

    async def everything_hidden(org: str) -> list[str]:  # the classification read failed
        return ["*"]

    monkeypatch.setattr(ctx, "restricted_namespaces_hidden_from", everything_hidden)
    if op == "list":
        out = await ops.handle_projection_failures_list(m.ProjectionFailureListInput(org="dev"))
        assert out.rows == []
    elif op == "retry":
        retried = await ops.handle_projection_failures_retry(
            m.ProjectionFailureRetryInput(org="dev")
        )
        assert retried.results == []
    else:
        resolved = await ops.handle_projection_failures_resolve(
            m.ProjectionFailureResolveInput(
                org="dev", failure_ids=["7d1b0c52-0000-4000-8000-000000000001"]
            )
        )
        assert resolved.resolved == 0
    assert org_admin_with_hidden.calls == []
