"""Other packages read state keys across every namespace through the code index's
reads, not through their own statement on `loom.code_index_state`."""

from __future__ import annotations

import asyncio
from typing import Any

from server.code.app.reads import CodeIndexReads
from server.code.store.index import CodeRepo


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((" ".join(sql.split()), params))
        return [
            {
                "org": "acme",
                "namespace": "code-a",
                "state_key": "qa_binding",
                "state_value": "tracker-a",
            },
            {"org": "acme", "namespace": "code-b", "state_key": "qa_binding", "state_value": None},
        ]


def test_state_values_for_keys_maps_each_key_to_org_and_namespace() -> None:
    ch = _CH()
    reads: CodeIndexReads = CodeRepo(ch)  # type: ignore[arg-type]
    got = asyncio.run(reads.state_values_for_keys(["qa_binding", "qa_branch_of"]))
    assert got == {
        "qa_binding": {("acme", "code-a"): "tracker-a", ("acme", "code-b"): ""},
        "qa_branch_of": {},
    }
    ((sql, params),) = ch.calls
    assert "SELECT org, namespace, state_key, state_value" in sql
    assert "FROM loom.code_index_state FINAL WHERE state_key IN {keys:Array(String)}" in sql
    assert params == {"keys": ["qa_binding", "qa_branch_of"]}


def test_tracker_binding_overrides_read_through_the_code_index(monkeypatch: Any) -> None:
    import server.code.app.reads as reads_mod
    import server.core.ch as ch_mod
    from server.tracker.app import binding

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(reads_mod, "code_index", lambda: CodeRepo(ch))  # type: ignore[arg-type]
    try:
        asyncio.run(binding.refresh_overrides(force=True))
        assert binding._OVERRIDES == {("acme", "code-a"): "tracker-a", ("acme", "code-b"): ""}
        assert [p for _, p in ch.calls] == [{"keys": [binding.OVERRIDE_KEY, binding.BRANCH_OF_KEY]}]
    finally:
        binding.reset_overrides_for_tests()


def test_the_refresh_fills_both_snapshots_from_one_read(monkeypatch: Any) -> None:
    import server.code.app.reads as reads_mod
    import server.core.ch as ch_mod
    from server.settings import settings
    from server.tracker.app import binding

    class _Both:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            self.calls.append(params)
            return [
                {
                    "org": "acme",
                    "namespace": "code-w-1-2",
                    "state_key": binding.BRANCH_OF_KEY,
                    "state_value": "code-w",
                },
                {
                    "org": "acme",
                    "namespace": "code-x",
                    "state_key": binding.OVERRIDE_KEY,
                    "state_value": "tracker-x",
                },
            ]

    ch = _Both()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(reads_mod, "code_index", lambda: CodeRepo(ch))  # type: ignore[arg-type]
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-w*": "tracker-w"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        '[{"org": "acme", "namespace": "code-w", "repo": "Acme/w"}]',
    )
    from server.code.domain.index import repos

    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    branch = repos.RepoConfig(
        org="acme",
        namespace="code-w-1-2",
        repo="Acme/w",
        git_dir="",
        git_remote="",
        git_branch="1.2",
        path_prefixes=(),
        github_token="",
        source="registry",
    )
    monkeypatch.setattr(repos, "_registry_repos", repos._registry_repos)
    monkeypatch.setattr(repos, "_registry_namespaces", repos._registry_namespaces)
    repos.set_registry_repos([branch], frozenset())
    try:
        asyncio.run(binding.refresh_overrides(force=True))
        assert len(ch.calls) == 1
        assert binding.tracker_namespaces_for("acme", "code-x") == ["tracker-x"]
        assert binding.tracker_namespaces_for("acme", "code-w-1-2") == ["tracker-w"]
    finally:
        binding.reset_overrides_for_tests()


class _OneRow:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        return self.row


def test_get_state_or_none_tells_an_absent_row_from_an_empty_one() -> None:
    """Removal writes "" only over a row that holds a value: an absent `qa_binding` must
    stay absent, or the empty row would read as cleared and hide the operator's map."""

    def read(row: dict[str, Any] | None) -> str | None:
        repo = CodeRepo(_OneRow(row))  # type: ignore[arg-type]
        return asyncio.run(repo.get_state_or_none("acme", "code-a", "qa_binding"))

    assert read(None) is None
    assert read({"state_value": ""}) == ""
    assert read({"state_value": None}) == ""
    assert read({"state_value": "tracker-a"}) == "tracker-a"
