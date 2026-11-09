"""A restricted repository added to a token brings
its bound tracker record, granted read-only."""

from __future__ import annotations

from typing import Any

import pytest

import server.identity.app.me_service_accounts as me
import server.identity.app.token_grants as tg
from server.auth.jwt import AuthInfo
from server.identity.app import me_callers
from server.identity.store import grants as grants_repo
from server.operations.base import Role

ORG = "clickhouse"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    import server.identity.app.namespace_classification as nc
    import server.tracker.app.binding as binding

    state: dict[str, Any] = {
        "restricted": {"code-private", "code-private-26-8", "tracker-private", "code-lumen"},
        "bindings": {
            "code-private": "tracker-private",
            "code-private-26-8": "tracker-private",
            "code-lumen": "tracker-public",
        },
        "grants": {
            "code-private": "admin",
            "code-private-26-8": "admin",
            "tracker-private": "admin",
            "code-lumen": "admin",
        },
    }

    async def _isr(org: str, ns: str) -> bool:
        return ns in state["restricted"]

    async def _role(org: str, ns: str, subject: str) -> str | None:
        return state["grants"].get(ns)

    monkeypatch.setattr(nc, "is_restricted", _isr)
    monkeypatch.setattr(
        binding,
        "tracker_namespaces_for",
        lambda org, ns, **_: [state["bindings"][ns]] if state["bindings"].get(ns) else [],
    )
    monkeypatch.setattr(me_callers, "caller_role_on", _role)
    return state


# A sign-in token: it reaches every namespace its owner's grants do.
PARENT = AuthInfo(
    subject="alice", org=ORG, namespaces=("*",), roles=(Role.WRITER,), is_service_account=False
)


@pytest.mark.asyncio
async def test_the_bound_restricted_tracker_record_is_added_once(wired) -> None:
    out = await me._bound_trackers(ORG, "alice", PARENT, ["code-private", "code-private-26-8"])
    assert out == ["tracker-private"]
    # A delegated parent whose claim does not reach the record brings none.
    narrow = AuthInfo(
        subject="sa:ci",
        org=ORG,
        namespaces=("code-private",),
        roles=(Role.VIEWER,),
        is_service_account=True,
        creator="alice",
    )
    assert await me._bound_trackers(ORG, "alice", narrow, ["code-private"]) == []


@pytest.mark.asyncio
async def test_a_standard_tracker_record_and_an_ungranted_one_are_not_added(wired) -> None:
    # tracker-public is not restricted: read through the repository, nothing to name
    assert await me._bound_trackers(ORG, "alice", PARENT, ["code-lumen"]) == []
    # no direct grant on the record: skipped, never a 403 — the repository still lands
    wired["grants"].pop("tracker-private")
    assert await me._bound_trackers(ORG, "alice", PARENT, ["code-private"]) == []
    assert await me._bound_trackers(ORG, "alice", PARENT, []) == []


@pytest.mark.asyncio
async def test_the_tracker_record_is_granted_viewer_like_a_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: list[dict[str, Any]] = []

    async def _insert(batch: list[dict[str, Any]], **_kw: Any) -> None:
        rows.extend(batch)

    monkeypatch.setattr(grants_repo, "insert_rows", _insert)

    async def _isr(org: str, ns: str) -> bool:
        return True

    granted = await tg.grant_restricted_to_token(
        ORG,
        "sa:t",
        "alice",
        {"code-private": "admin", "tracker-private": "admin", "secret-notes": "admin"},
        is_restricted=_isr,
        is_code_namespace=lambda o, n: n.startswith(("code-", "tracker-")),
    )
    assert granted == ["code-private", "tracker-private", "secret-notes"]
    roles = {r["namespace"]: r["role"] for r in rows}
    assert roles == {
        "code-private": "viewer",
        "tracker-private": "viewer",
        "secret-notes": "writer",
    }


@pytest.mark.asyncio
async def test_the_tracker_record_is_viewer_whatever_the_role_handed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `*` mint hands the person's own role (writer here) for the bound record; the
    # token's direct grant there is still read-only.
    rows: list[dict[str, Any]] = []

    async def _insert(batch: list[dict[str, Any]], **_kw: Any) -> None:
        rows.extend(batch)

    async def _isr(org: str, ns: str) -> bool:
        return True

    monkeypatch.setattr(grants_repo, "insert_rows", _insert)
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.tracker_namespace",
        lambda o, n: object() if n == "tracker-private" else None,
    )
    monkeypatch.setattr("server.identity.app.namespace_classification.is_restricted", _isr)
    is_code = await tg.code_namespace_predicate(ORG, ["tracker-private"])
    await tg.grant_restricted_to_token(
        ORG,
        "sa:t",
        "alice",
        {"tracker-private": "writer"},
        is_code_namespace=is_code,
        is_restricted=_isr,
    )
    assert [(r["namespace"], r["role"]) for r in rows] == [("tracker-private", "viewer")]
