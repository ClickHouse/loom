"""An index added with auth off is maintained by the single user, not by nobody."""

from unittest.mock import AsyncMock

import pytest

from server.auth import default_namespace
from server.code.app import repo_admin
from server.settings import settings


@pytest.mark.parametrize(("required", "subject"), [(False, "you"), (True, "")])
def test_auth_off_subject(monkeypatch: pytest.MonkeyPatch, required: bool, subject: str) -> None:
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda _self: required)
    assert default_namespace.auth_off_subject() == subject


@pytest.mark.asyncio
async def test_auth_off_adder_becomes_maintainer(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.index import bootstrap

    monkeypatch.setattr(type(settings), "auth_actually_required", lambda _self: False)
    monkeypatch.setattr(repo_admin, "current_caller", lambda: ("", None))
    monkeypatch.setattr(repo_admin, "_namespace_taken", AsyncMock(return_value=False))
    monkeypatch.setattr(repo_admin.reg, "insert_entry", AsyncMock())
    monkeypatch.setattr(repo_admin, "_empty_tracker_links", AsyncMock())
    monkeypatch.setattr(repo_admin.reg, "refresh_db_repos", AsyncMock())
    monkeypatch.setattr(repo_admin, "registry_repos", lambda: [])
    monkeypatch.setattr(bootstrap, "set_stage", AsyncMock())
    make = AsyncMock()
    monkeypatch.setattr("server.identity.app.registration.grant_self_admin", make)
    out = await repo_admin._register(
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
    make.assert_awaited_once_with("dev", "code-x", "you")
    assert out.my_role == "admin"
    assert repo_admin.reg.insert_entry.await_args.kwargs["created_by"] == "you"
