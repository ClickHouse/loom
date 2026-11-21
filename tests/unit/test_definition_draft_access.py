"""Unapproved definition drafts are visible only to namespace admins."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_learnings import LearningsListInput
from server.memory.app import learnings
from server.memory.store import learnings as repository


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("kind", ["knowledge_gap", "query_history"])
async def test_private_drafts_are_filtered_before_the_list_limit(
    monkeypatch: pytest.MonkeyPatch, admin: bool, kind: str
) -> None:
    from server.auth import context
    from server.auth.authz import CachedGate
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    auth = AuthInfo(
        subject="reviewer",
        org="o",
        namespaces=("n",),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    monkeypatch.setattr(context, "current_auth", lambda: auth)

    async def admits(self: Any, namespace: str, role: Role) -> bool:
        assert namespace == "n" and role == Role.ADMIN
        return admin

    monkeypatch.setattr(CachedGate, "admits", admits)

    def row(identifier: str, source_kind: str) -> dict[str, Any]:
        return dict(
            candidate_id=identifier,
            memory_key="definition/x",
            memory_type="definition",
            proposed_value=json.dumps({"term": "ARR", "body": identifier}),
            supporter_count=0,
            confidence_avg=1.0,
            supporters=[],
            rationale=identifier,
            source_kind=source_kind,
            agent="-",
            status="pending",
            proposed_at=datetime(2026, 1, 1),
            decided_at=None,
            decided_by="",
            decision_reason="",
        )

    private, public = row("private-canary", kind), row("public", "conversation_seeder")

    class Store:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            rows = [private, public]
            if "source_kind NOT IN" in sql:
                rows = [r for r in rows if r["source_kind"] not in params["excluded_kinds"]]
            return rows[: params["lim"]]

    monkeypatch.setattr(repository, "_default_client", lambda: Store())
    result = await learnings.handle_learnings_list(
        LearningsListInput(org="o", namespace="n", limit=1)
    )
    assert [r.candidate_id for r in result.rows] == ["private-canary" if admin else "public"]


async def test_quality_counts_private_drafts_only_where_the_caller_administers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth import context
    from server.auth.authz import CachedGate
    from server.auth.jwt import AuthInfo
    from server.memory.api.models_learnings import LearningsQualityInput
    from server.operations.base import Role

    auth = AuthInfo(
        subject="reviewer",
        org="o",
        namespaces=("one", "two"),
        roles=(Role.VIEWER,),
        is_service_account=False,
    )
    monkeypatch.setattr(context, "current_auth", lambda: auth)

    async def scope(org: str) -> Any:
        return context.OrgScopeFilter(accessible=True, where="", params={})

    async def admits(self: Any, namespace: str, role: Role) -> bool:
        return namespace == "one" and role == Role.ADMIN

    monkeypatch.setattr(context, "resolve_org_scope", scope)
    monkeypatch.setattr(CachedGate, "admits", admits)

    class Store:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            if "SELECT DISTINCT namespace" in sql:
                return [{"namespace": "one"}, {"namespace": "two"}]
            admins = params["draft_admin_ns"] if "source_kind NOT IN" in sql else ["one", "two"]
            return [
                dict(
                    namespace=ns,
                    pending=public + (private if ns in admins else 0),
                    promoted=0,
                    rejected=0,
                    approve_ratio=0,
                )
                for ns, public, private in (("one", 1, 42), ("two", 3, 47))
            ]

    monkeypatch.setattr("server.core.ch.get_client", lambda: Store())
    monkeypatch.setattr(repository, "_default_client", lambda: Store())
    result = await learnings.handle_learnings_quality(
        LearningsQualityInput(org="o", source_kind=None)
    )
    assert {row.namespace: row.pending for row in result.rows} == {"one": 43, "two": 3}


@pytest.mark.parametrize("path", ["tags", "shared", "inferred"])
@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("kind", ["knowledge_gap", "query_history"])
async def test_lineage_does_not_disclose_an_unapproved_drafts_rationale(
    monkeypatch: pytest.MonkeyPatch, path: str, admin: bool, kind: str
) -> None:
    from server.memory.api.models_graph import MemoryLineageInput
    from server.memory.app import dedup, learning_access
    from server.memory.store import shared
    from tests.unit.ch_fake import RecordingCH

    cid = "00000000-0000-0000-0000-000000000001"
    candidate = dict(
        candidate_id=cid,
        source_kind=kind,
        supporter_count=3,
        supporters=[],
        rationale="private-canary",
        decided_at=datetime(2026, 1, 1),
    )

    async def allowed(org: str, namespace: str) -> bool:
        return admin

    async def find(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return candidate

    async def fetch(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
        if path == "tags":
            return None
        return dict(
            promoted_by="human:reviewer",
            promoted_at=datetime(2026, 1, 1),
            promoted_from_candidate_id=cid if path == "shared" else None,
            promoted_from=[],
            memory_type="semantic",
            memory_key="k",
        )

    RecordingCH().on(
        "FROM loom.memory FINAL",
        [
            dict(
                tags=[f"candidate-id:{cid}", f"source-kind:{kind}"],
                created_at=datetime(2026, 1, 1),
                user_id="-",
                agent="-",
                actor="human:reviewer",
            )
        ],
    ).install(monkeypatch)
    monkeypatch.setattr(learning_access, "private_candidates_visible", allowed)
    monkeypatch.setattr(repository, "find_candidate", find)
    monkeypatch.setattr(shared, "find_candidate_for_promotion", find)
    monkeypatch.setattr(shared, "fetch_shared", fetch)
    result = await dedup.handle_memory_lineage(
        MemoryLineageInput(org="o", namespace="n", agent="-", memory_key="k")
    )
    assert result.found
    assert (result.candidate is not None) is admin
    assert ("private-canary" in result.model_dump_json()) is admin
    if admin:
        assert result.candidate is not None
        assert result.candidate.supporter_count == 3
    assert result.supporters == []
