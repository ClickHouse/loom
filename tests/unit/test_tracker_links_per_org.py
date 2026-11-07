"""Tracker links are read per (org, code namespace). Code namespace names are
derived from the repository and branch, so two orgs can hold the same name: a
maintainer's link in one org never applies to the other, and the operator's map
applies only to the repositories the operator configured."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server.code.domain.index import repos
from server.code.store.index import CodeRepo
from server.settings import settings
from server.tracker.app import binding

ORG_X, ORG_Y = "org-x", "org-y"
CODE = "code-widgets"


@pytest.fixture(autouse=True)
def _clean():
    binding.reset_overrides_for_tests()
    yield
    binding.reset_overrides_for_tests()


def test_a_maintainer_link_applies_only_in_its_own_org() -> None:
    binding.reset_overrides_for_tests({(ORG_Y, CODE): "tracker-shared"})
    assert binding.tracker_namespaces_for(ORG_Y, CODE) == ["tracker-shared"]
    assert binding.tracker_namespaces_for(ORG_X, CODE) == []
    assert binding.code_namespaces_linking(ORG_X, "tracker-shared", [CODE]) == []


def test_a_cleared_link_in_one_org_keeps_the_other_orgs_link() -> None:
    binding.reset_overrides_for_tests({(ORG_X, CODE): "tracker-x", (ORG_Y, CODE): ""})
    assert binding.tracker_namespaces_for(ORG_X, CODE) == ["tracker-x"]
    assert binding.tracker_namespaces_for(ORG_Y, CODE) == []


class _CH:
    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "org": ORG_X,
                "namespace": CODE,
                "state_key": "qa_binding",
                "state_value": "tracker-x",
            },
            {
                "org": ORG_Y,
                "namespace": CODE,
                "state_key": "qa_binding",
                "state_value": "tracker-y",
            },
        ]


def test_the_stored_links_keep_their_org() -> None:
    got = asyncio.run(CodeRepo(_CH()).state_values_for_keys([binding.OVERRIDE_KEY]))  # type: ignore[arg-type]
    assert got == {binding.OVERRIDE_KEY: {(ORG_X, CODE): "tracker-x", (ORG_Y, CODE): "tracker-y"}}


def test_the_operator_map_links_only_the_operator_s_repositories(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    added = repos.RepoConfig(
        org=ORG_Y,
        namespace="code-widgets-fork",
        repo="Other/widgets",
        git_dir="",
        git_remote="",
        git_branch="main",
        path_prefixes=(),
        github_token="",
        source="registry",
    )
    _set_registry(monkeypatch, [added])
    assert binding.tracker_namespaces_for(ORG_X, CODE) == ["tracker-widgets"]
    # a self-service repository whose name matches the glob gets nothing from it
    assert binding.tracker_namespaces_for(ORG_Y, "code-widgets-fork") == []
    # nor does a namespace of the same name in an org the operator configured no entry for
    assert binding.tracker_namespaces_for(ORG_Y, CODE) == []


def test_an_operator_entry_with_a_maintainer_access_row_keeps_its_glob_link(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    access = repos.RepoConfig(
        org=ORG_X,
        namespace=CODE,
        repo="Acme/widgets",
        git_dir="",
        git_remote="",
        git_branch="main",
        path_prefixes=(),
        github_token="",
        classification="restricted",
        source="registry",
    )
    _set_registry(monkeypatch, [access])
    (entry,) = repos.configured_repos()
    assert (entry.source, entry.access_source) == ("config", "registry")
    assert binding.tracker_namespaces_for(ORG_X, CODE) == ["tracker-widgets"]


def test_a_branch_follows_only_an_operator_source_and_its_own_row_wins(monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    branch = "code-widgets-26-8"
    _set_registry(monkeypatch, [_entry(ORG_X, branch), _entry(ORG_X, "code-widgets-fork-1")])
    binding.reset_overrides_for_tests(
        branches={(ORG_X, branch): CODE, (ORG_X, "code-widgets-fork-1"): "code-widgets-fork"}
    )
    assert binding.tracker_namespaces_for(ORG_X, branch) == ["tracker-widgets"]
    # a source that is not an operator entry is not followed
    assert binding.tracker_namespaces_for(ORG_X, "code-widgets-fork-1") == []
    # the row names a source in its own org only
    assert binding.tracker_namespaces_for(ORG_Y, branch) == []
    binding.reset_overrides_for_tests({(ORG_X, branch): "tracker-own"}, {(ORG_X, branch): CODE})
    assert binding.tracker_namespaces_for(ORG_X, branch) == ["tracker-own"]


def _set_registry(monkeypatch, entries: list[repos.RepoConfig]) -> None:
    """Set the registry snapshot through its writer; monkeypatch restores both globals."""
    monkeypatch.setattr(repos, "_registry_repos", repos._registry_repos)
    monkeypatch.setattr(repos, "_registry_namespaces", repos._registry_namespaces)
    repos.set_registry_repos(entries)


def _entry(org: str, namespace: str) -> repos.RepoConfig:
    return repos.RepoConfig(
        org=org,
        namespace=namespace,
        repo="Acme/widgets",
        git_dir="",
        git_remote="",
        git_branch="26.8",
        path_prefixes=(),
        github_token="",
        source="registry",
    )


def test_a_branch_row_with_no_repository_entry_is_not_followed(monkeypatch) -> None:
    """A removal or a late copy can leave a `qa_branch_of` row behind; a namespace with
    no entry beside it reads nothing from the operator's repository."""
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    branch = "code-widgets-26-8"
    binding.reset_overrides_for_tests(branches={(ORG_X, branch): CODE})
    _set_registry(monkeypatch, [])
    assert binding.tracker_namespaces_for(ORG_X, branch) == []
    _set_registry(monkeypatch, [_entry(ORG_X, branch)])
    assert binding.tracker_namespaces_for(ORG_X, branch) == ["tracker-widgets"]
    # self-service switched off: the registry reads empty, and the row is not followed
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    assert binding.tracker_namespaces_for(ORG_X, branch) == []


def test_the_registry_namespaces_follow_each_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    _set_registry(monkeypatch, [_entry(ORG_X, "code-widgets-26-8")])
    assert repos.registry_namespaces() == {(ORG_X, "code-widgets-26-8")}
    repos.set_registry_repos([_entry(ORG_Y, "code-widgets-26-8")])
    assert repos.registry_namespaces() == {(ORG_Y, "code-widgets-26-8")}
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    assert repos.registry_namespaces() == frozenset()


def test_a_refresh_read_before_forget_links_keeps_nothing(monkeypatch) -> None:
    """A refresh whose read started before a registration emptied the rows must not put
    the old rows back after `forget_links` dropped them."""
    import server.code.app.reads as reads_mod
    import server.core.ch as ch_mod

    class _Slow:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            binding.forget_links(ORG_X, CODE)  # the registration lands mid-read
            return [
                {
                    "org": ORG_X,
                    "namespace": CODE,
                    "state_key": binding.BRANCH_OF_KEY,
                    "state_value": "code-root",
                }
            ]

    ch = _Slow()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(reads_mod, "code_index", lambda: CodeRepo(ch))  # type: ignore[arg-type]
    asyncio.run(binding.refresh_overrides(force=True))
    assert (ORG_X, CODE) not in binding._BRANCH_OF


def test_a_discarded_refresh_leaves_the_next_call_to_read_again(monkeypatch) -> None:
    import server.code.app.reads as reads_mod
    import server.core.ch as ch_mod

    calls: list[int] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            calls.append(1)
            if len(calls) == 1:
                binding.forget_links(ORG_X, "code-other")  # a registration lands mid-read
            return []

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(reads_mod, "code_index", lambda: CodeRepo(ch))  # type: ignore[arg-type]
    asyncio.run(binding.refresh_overrides())  # the snapshot is unread, so this reads
    asyncio.run(binding.refresh_overrides())  # discarded above, so this reads again
    assert len(calls) == 2


@pytest.mark.parametrize("caller", ["pack", "brief", "risk_fit", "token_setup"])
def test_every_reader_of_the_links_sees_a_branch_from_an_empty_snapshot(
    monkeypatch, caller
) -> None:
    """A process that has served no admin op starts with an empty snapshot; each reader
    still finds a registered branch's operator root, and so its tracker. The tracker is
    restricted here, so each reader's answer shows it found one."""
    import server.code.app.reads as reads_mod
    import server.core.ch as ch_mod
    from server.identity.app import me_callers
    from server.identity.app import namespace_classification as nc

    branch = "code-widgets-26-8"

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return [
                {
                    "org": ORG_X,
                    "namespace": branch,
                    "state_key": binding.BRANCH_OF_KEY,
                    "state_value": CODE,
                }
            ]

    async def _restricted(org: str, ns: str) -> bool:
        return ns == "tracker-widgets"

    async def _role(org: str, ns: str, subject: str) -> str:
        return "viewer"

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(reads_mod, "code_index", lambda: CodeRepo(ch))  # type: ignore[arg-type]
    monkeypatch.setattr(nc, "is_restricted", _restricted)
    monkeypatch.setattr(me_callers, "caller_role_on", _role)
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets*": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    _set_registry(monkeypatch, [_entry(ORG_X, branch)])
    if caller == "pack":
        from server.code.app.index import packs

        got: Any = asyncio.run(packs.tracker_section_rows(ORG_X, branch, 1, []))
        assert got == ("restricted", [])
    elif caller == "brief":
        from server.code.app.index import review_brief_sources
        from server.code.app.index.review_brief import RESTRICTED

        got = asyncio.run(review_brief_sources.tracker_pr_facts(ORG_X, branch, 1))
        assert got == (RESTRICTED, {})
    elif caller == "risk_fit":
        from server.code.app.index import risk_fit

        got = asyncio.run(risk_fit._bound_tracker_namespace(ORG_X, branch))
        assert got == ("", "restricted")
    else:
        from server.identity.app import me_setup

        got = asyncio.run(me_setup._with_bound_tracker(ORG_X, "alice", [branch]))
        assert got == [branch, "tracker-widgets"]


def test_a_link_set_on_an_operator_repository_later_reaches_its_branches(monkeypatch) -> None:
    """The contract the tracker overview states: a branch reads its operator repository's
    links when read, so a link a tracker admin sets on the repository after the branch
    was added reaches the branch's readers too."""
    monkeypatch.setattr(settings, "tracker_code_namespace_bindings_json", "")
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    branch = "code-widgets-26-8"
    _set_registry(monkeypatch, [_entry(ORG_X, branch)])
    binding.reset_overrides_for_tests(branches={(ORG_X, branch): CODE})
    assert binding.tracker_namespaces_for(ORG_X, branch) == []
    binding.reset_overrides_for_tests({(ORG_X, CODE): "tracker-later"}, {(ORG_X, branch): CODE})
    assert binding.tracker_namespaces_for(ORG_X, branch) == ["tracker-later"]


def test_a_refresh_after_a_write_reads_its_own_writes(monkeypatch) -> None:
    """A forced refresh follows the caller's own write, and the first refresh after
    `forget_links` follows an emptying: both read with sequential consistency, so a
    lagging ClickHouse replica cannot hand back the old rows. A plain refresh does not."""
    import server.code.app.reads as reads_mod
    import server.core.ch as ch_mod

    seen: list[bool] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            seen.append(ch_mod.sequential_reads_active())
            return []

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(reads_mod, "code_index", lambda: CodeRepo(ch))  # type: ignore[arg-type]
    asyncio.run(binding.refresh_overrides())
    asyncio.run(binding.refresh_overrides(force=True))
    binding.forget_links(ORG_X, CODE)
    binding._OVERRIDES_AT = -float("inf")
    asyncio.run(binding.refresh_overrides())
    binding._OVERRIDES_AT = -float("inf")
    asyncio.run(binding.refresh_overrides())
    assert seen == [False, True, True, False]


def test_branches_following_names_only_the_branches_that_read_the_links(monkeypatch) -> None:
    """The same test `tracker_namespaces_for` makes: a registered branch of an operator
    repository with no `qa_binding` of its own."""
    monkeypatch.setattr(
        settings, "tracker_code_namespace_bindings_json", '{"code-widgets": "tracker-widgets"}'
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps([{"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"}]),
    )
    monkeypatch.setattr(settings, "code_self_service_enabled", True)
    names = ["code-widgets-26-8", "code-widgets-own", "code-widgets-cleared"]
    _set_registry(monkeypatch, [_entry(ORG_X, n) for n in names] + [_entry(ORG_Y, names[0])])
    branches = {(ORG_X, n): CODE for n in [*names, "code-widgets-gone"]}  # gone: no entry
    branches[(ORG_Y, names[0])] = CODE  # another org, where CODE is no operator entry
    own = {(ORG_X, "code-widgets-own"): "tracker-own", (ORG_X, "code-widgets-cleared"): ""}
    binding.reset_overrides_for_tests(own, branches=branches)
    assert binding.branches_following(ORG_X, CODE) == ["code-widgets-26-8"]
    assert [binding.tracker_namespaces_for(ORG_X, n) for n in names] == [
        ["tracker-widgets"],  # follows the root
        ["tracker-own"],
        [],  # its cleared row wins over the root
    ]
    assert binding.branches_following(ORG_Y, CODE) == []
    # a branch the operator later configures reads the map under its own name
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps(
            [
                {"org": ORG_X, "namespace": CODE, "repo": "Acme/widgets"},
                {"org": ORG_X, "namespace": names[0], "repo": "Acme/widgets"},
            ]
        ),
    )
    assert binding.branches_following(ORG_X, CODE) == []
    monkeypatch.setattr(settings, "code_repos_json", "[]")  # the root left the config
    assert binding.branches_following(ORG_X, CODE) == []


def test_a_forced_refresh_that_forget_links_discards_reads_once_more(monkeypatch) -> None:
    """A caller that forces a refresh right after its own write reads the result at once,
    so a concurrent `forget_links` does not leave it the old snapshot."""
    import server.code.app.reads as reads_mod
    import server.core.ch as ch_mod

    calls: list[int] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            calls.append(1)
            if len(calls) == 1:
                binding.forget_links(ORG_Y, "code-other")  # another registration lands
            return [
                {
                    "org": ORG_X,
                    "namespace": CODE,
                    "state_key": binding.OVERRIDE_KEY,
                    "state_value": "tracker-new",
                }
            ]

    ch = _CH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(reads_mod, "code_index", lambda: CodeRepo(ch))  # type: ignore[arg-type]
    asyncio.run(binding.refresh_overrides(force=True))
    assert len(calls) == 2
    assert binding.override_for(ORG_X, CODE) == "tracker-new"
