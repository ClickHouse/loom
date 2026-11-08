"""A self-service repository checks its name again under the account claim after the
remote probe, writes the creator's grant first, and waits on no claim inside it."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.code.app import repo_admin
from server.core.errors import ValidationError


@pytest.mark.asyncio
async def test_a_name_taken_during_the_probe_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The free-name check runs again under the account's claim, which sign-up and
    namespace.register hold: one that took the name meanwhile wins."""
    held: list[bool] = []

    @asynccontextmanager
    async def _account_change(org: str, namespace: str) -> Any:
        held.append(True)
        yield
        held.append(False)

    async def _taken(org: str, namespace: str) -> bool:
        assert held == [True]  # asked under the account claim
        return True

    monkeypatch.setattr(repo_admin, "account_change", _account_change)
    monkeypatch.setattr(repo_admin, "_empty_tracker_links", AsyncMock())
    monkeypatch.setattr(repo_admin, "current_caller", lambda: ("alice", None))
    monkeypatch.setattr(repo_admin, "_namespace_taken", _taken)
    insert = AsyncMock()
    monkeypatch.setattr(repo_admin.reg, "insert_entry", insert)
    make = AsyncMock()
    monkeypatch.setattr("server.identity.app.registration.grant_self_admin", make)
    with pytest.raises(ValidationError) as ei:
        await repo_admin._register(
            org="dev",
            namespace="code-x",
            repo="o/x",
            git_remote="https://github.com/o/x",
            provider="github",
            branch="main",
            classification="standard",
            join_policy="open",
            github_token="",
            path_prefixes=[],
            exclude=[],
            remote_head="abc",
        )
    assert ei.value.to_detail()["code"] == "REPO_EXISTS"
    insert.assert_not_awaited()
    make.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_claim_is_waited_on_under_the_account_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The maintainers' fan-out waits on claims of its own, so it runs once the account
    claim is released, not inside its bound (`ensure_registered(sync=False)`)."""
    from server.code.app.index import bootstrap, registry

    events: list[str] = []

    @asynccontextmanager
    async def _account_change(org: str, namespace: str) -> Any:
        events.append("claim")
        yield
        events.append("release")

    async def _ensure(cfg: Any, **kw: Any) -> bool:
        events.append(f"ensure sync={kw.get('sync', True)}")
        return True

    async def _sync(org: str, namespace: str) -> None:
        events.append("sync")

    cfg = SimpleNamespace(org="dev", namespace="code-x")
    monkeypatch.setattr(repo_admin, "account_change", _account_change)
    monkeypatch.setattr(repo_admin, "_empty_tracker_links", AsyncMock())
    monkeypatch.setattr(repo_admin, "current_caller", lambda: ("alice", None))
    monkeypatch.setattr(repo_admin, "_namespace_taken", AsyncMock(return_value=False))

    async def _entry(**_k: Any) -> None:
        events.append("entry")

    async def _grant(*_a: Any) -> None:
        events.append("grant")

    monkeypatch.setattr(repo_admin.reg, "insert_entry", _entry)
    monkeypatch.setattr(repo_admin.reg, "refresh_db_repos", AsyncMock())
    monkeypatch.setattr(repo_admin, "registry_repos", lambda: [cfg])
    monkeypatch.setattr(registry, "ensure_registered", _ensure)
    monkeypatch.setattr("server.identity.app.registration.grant_self_admin", _grant)
    monkeypatch.setattr(repo_admin, "_sync_maintainers", _sync)
    monkeypatch.setattr(bootstrap, "set_stage", AsyncMock())
    await repo_admin._register(
        org="dev",
        namespace="code-x",
        repo="o/x",
        git_remote="https://github.com/o/x",
        provider="github",
        branch="main",
        classification="standard",
        join_policy="open",
        github_token="",
        path_prefixes=[],
        exclude=[],
        remote_head="abc",
    )
    # The grant first: a replica that has not loaded the entry still sees the name in use.
    assert events == ["claim", "grant", "entry", "ensure sync=False", "release", "sync"]
