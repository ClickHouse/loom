"""The PR history pass syncs nothing while the self-service registry is unread: an
operator entry for a namespace a member added is not skipped yet, and its history,
fetched with the operator's token, would land in that member's namespace."""

from __future__ import annotations

import pytest

from server.code.app.index import repo_registry
from server.code.workers import pr_history


@pytest.mark.asyncio
async def test_the_pass_syncs_nothing_while_the_registry_is_unread(monkeypatch) -> None:
    def listed() -> list[object]:
        raise AssertionError("no entry is read")

    monkeypatch.setattr(repo_registry, "registry_read", lambda: False)
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", listed)
    result = pr_history.CodePrHistoryResult()
    await pr_history._run(result)
    assert "registry is unread" in result.note
