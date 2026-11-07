"""A repository links several trackers. The first is the
primary — its numbers are the repository's, so every lookup by number reads
it — and every search reads them all, each result naming its tracker."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

import server.auth.authz as authz
import server.tracker.app.binding as b
import server.tracker.app.dupcheck as hd
import server.tracker.app.dupcheck_fanout as fo
from server.settings import settings
from server.tracker.app import handlers as h
from server.tracker.app import similar as tsim
from server.tracker.app import source as tsrc

ORG = "acme"
CODE = "code-widgets"
QA, DESK, PRIV = "tracker-jira", "tracker-zendesk", "tracker-security-private"


class _Ns:
    def __init__(
        self, restricted: bool = False, repos: tuple[str, ...] = ("Acme/widgets",)
    ) -> None:
        self.restricted = restricted
        self.repos = repos


NAMESPACES = {QA: _Ns(), DESK: _Ns(), PRIV: _Ns(True)}


@pytest.fixture
def wired(monkeypatch):
    b.reset_overrides_for_tests({(ORG, CODE): f"{QA},{DESK},{PRIV}"})
    monkeypatch.setattr(b, "refresh_overrides", _no_refresh)
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.tracker_namespace", lambda org, n: NAMESPACES.get(n)
    )
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces", lambda: dict(NAMESPACES)
    )
    readable: set[str] = {CODE}

    async def _scope(org, ns, role=None, *, hides_answer=True):
        return ns in readable

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    yield readable
    b.reset_overrides_for_tests({})


async def _no_refresh(**_: Any) -> dict[str, str]:
    return {}


# ---- the binding is an ordered list, the stored form stays one string ------------


def test_a_binding_reads_as_an_ordered_list_and_a_single_name_as_one() -> None:
    assert b.parse_binding("tracker-a") == ["tracker-a"]
    assert b.parse_binding(" tracker-a , tracker-b,tracker-a,, ") == ["tracker-a", "tracker-b"]
    assert b.parse_binding(["tracker-b", "tracker-a"]) == ["tracker-b", "tracker-a"]
    assert b.parse_binding("") == [] and b.parse_binding(None) == []
    assert b.encode_binding(["tracker-a", "tracker-b", "tracker-a"]) == "tracker-a,tracker-b"


def test_the_primary_is_first_and_the_config_may_name_a_list(monkeypatch) -> None:
    b.reset_overrides_for_tests({})
    monkeypatch.setattr(
        settings,
        "tracker_code_namespace_bindings_json",
        '{"code-widgets*": ["tracker-jira", "tracker-zendesk"], "code-x": "tracker-x"}',
    )
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps(
            [
                {"org": ORG, "namespace": n, "repo": "Acme/widgets"}
                for n in (CODE, "code-widgets-release", "code-x")
            ]
        ),
    )
    assert b.tracker_namespaces_for(ORG, "code-widgets-release") == [QA, DESK]
    assert b.tracker_namespace_for(ORG, "code-widgets-release") == QA
    assert b.tracker_namespaces_for(ORG, "code-x") == ["tracker-x"]
    b.reset_overrides_for_tests({(ORG, CODE): ""})  # a maintainer cleared the config's binding
    assert b.tracker_namespaces_for(ORG, CODE) == []
    b.reset_overrides_for_tests({(ORG, CODE): f"{DESK},{QA}"})
    assert b.tracker_namespace_for(ORG, CODE) == DESK
    assert b.code_namespaces_linking(ORG, QA, [CODE, "code-x"]) == [CODE]
    b.reset_overrides_for_tests({})


@pytest.mark.asyncio
async def test_each_linked_tracker_passes_its_own_gate(wired) -> None:
    # standard trackers through the repository; the restricted one needs its own grant
    assert await b.authorized_tracker_namespaces(ORG, CODE) == [QA, DESK]
    wired.add(PRIV)
    assert await b.authorized_tracker_namespaces(ORG, CODE) == [QA, DESK, PRIV]
    assert await b.authorized_tracker_namespace(ORG, CODE) == QA
    out = await b.resolve_binding(ORG, CODE)
    assert out["tracker_namespace"] == QA and out["tracker_namespaces"] == [QA, DESK, PRIV]


# ---- searches read every linked tracker ---------------------------------------


@pytest.mark.asyncio
async def test_linked_trackers_come_right_after_the_primary_and_are_never_capped(
    wired, monkeypatch
) -> None:
    async def _kinds(org):
        return {CODE: "code"}

    monkeypatch.setattr("server.identity.app.namespace_kinds.kind_map", _kinds)
    got = await fo.readable_tracker_namespaces(ORG, QA, [], 1, linked=[QA, DESK])
    assert got == [QA, DESK], "cap 1 still reads every linked tracker"
    # a maintainer's link opens a standard tracker to the repository's readers
    assert await fo._may_read_tracker(ORG, DESK, {CODE: "code"}, hides_answer=False)


@pytest.mark.asyncio
async def test_the_duplicate_check_on_a_repository_reads_all_its_trackers(
    wired, monkeypatch
) -> None:
    from server.tracker.api.models import TrackerDuplicateCheckInput, TrackerDuplicateCheckOutput

    asked: list[str] = []

    async def _one(inp, wall, **_kw):
        asked.append(inp.namespace)
        return TrackerDuplicateCheckOutput(
            verdict="related", items=[{"number": 7, "confidence": 0.5}], epistemic="calibrated"
        )

    async def _kinds(org):
        return {CODE: "code"}

    monkeypatch.setattr(hd, "_check_one", _one)
    monkeypatch.setattr("server.identity.app.namespace_kinds.kind_map", _kinds)
    monkeypatch.setattr(settings, "tracker_dupcheck_fanout_namespaces", 1)
    out = await hd.handle_tracker_duplicate_check(
        TrackerDuplicateCheckInput(org=ORG, namespace=CODE, title="crash in merge")
    )
    assert asked == [QA, DESK]
    assert {it["namespace"] for it in out.items} == {QA, DESK}


@pytest.mark.asyncio
async def test_similar_by_number_searches_the_others_by_that_items_text(wired, monkeypatch) -> None:
    from server.tracker.api.models import TrackerSimilarInput, TrackerSimilarOutput

    calls: list[tuple[str, int, str]] = []

    async def _one(inp):
        calls.append((inp.namespace, int(inp.issue_number), inp.text))
        score = 0.9 if inp.namespace == QA else 0.95
        return TrackerSimilarOutput(
            items=[{"number": 7, "score": score}], query={"number": inp.issue_number}
        )

    class _Src:
        async def similar_item(self, org, ns, number):
            assert ns == QA and number == 101
            return {"title": "Too many parts", "body": "merge wedged"}

    monkeypatch.setattr(tsim, "_similar_one", _one)
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: _Src())
    out = await tsim.handle_tracker_similar(
        TrackerSimilarInput(org=ORG, namespace=CODE, issue_number=101)
    )
    assert calls[0] == (QA, 101, "")
    assert calls[1] == (DESK, 0, "Too many parts\nmerge wedged"), (
        "#101 is the repository's, not the desk's"
    )
    assert out.namespaces == [QA, DESK]
    assert [(it["namespace"], it["score"]) for it in out.items] == [(DESK, 0.95), (QA, 0.9)]
    assert out.query == {"number": 101}


@pytest.mark.asyncio
async def test_similar_on_one_tracker_is_the_single_read(wired, monkeypatch) -> None:
    from server.tracker.api.models import TrackerSimilarInput, TrackerSimilarOutput

    b.reset_overrides_for_tests({(ORG, CODE): QA})
    seen: list[str] = []

    async def _one(inp):
        seen.append(inp.namespace)
        return TrackerSimilarOutput()

    monkeypatch.setattr(tsim, "_similar_one", _one)
    out = await tsim.handle_tracker_similar(TrackerSimilarInput(org=ORG, namespace=CODE, text="x"))
    assert seen == [CODE] and out.namespaces == []  # _similar_one binds it


@pytest.mark.asyncio
async def test_the_answer_route_adds_the_others_served_sections_but_not_by_number(
    monkeypatch,
) -> None:
    from server.tracker.app import router

    def _sec(template, status, params):
        return router.Section(
            template=template,
            title=template,
            access="public",
            params=params,
            why="",
            status=status,
            cites=[],
        )

    async def _answer(question, *, tracker_namespace, **_):
        return router.TrackerAnswer(
            intent="x",
            anchors={},
            namespace=tracker_namespace,
            sections=[
                _sec("by_component", "served", {"component": "merges"}),
                _sec("items_by_number", "served", {"numbers": [7]}),
                _sec("by_version", "empty", {"version": "24.8"}),
            ],
        )

    monkeypatch.setattr(router, "answer", _answer)
    monkeypatch.setattr(router, "render_lines", lambda a, **_: [s.template for s in a.sections])
    kept, lines = await h._answer_elsewhere("merges", [DESK], {"org": ORG})
    assert [(ns, s.template) for ns, s in kept] == [(DESK, "by_component")]
    assert lines == [f"{DESK}: by_component"]


@pytest.mark.asyncio
async def test_bisect_merges_the_other_trackers_prs_once_each(wired, monkeypatch) -> None:
    from server.code.app import bisect_scan

    async def _scan(src, org, t, plan, **_):
        return ([{"pr_number": 5}, {"pr_number": 9}] if t == DESK else [{"pr_number": 5}]), True

    monkeypatch.setattr(bisect_scan, "_scan", _scan)
    monkeypatch.setattr(
        "server.tracker.store.source.TrackerSource", lambda org, t: SimpleNamespace()
    )
    got = await bisect_scan._linked_scan(
        ORG,
        CODE,
        QA,
        bisect_scan.ScanPlan(paths=["a.cpp"]),
        {5},
        frames=[],
        patterns=[],
        weights={},
        limit=5,
    )
    assert got == [{"pr_number": 9, "tracker_namespace": DESK}]


@pytest.mark.asyncio
async def test_memory_ask_searches_every_tracker_for_a_component_question(
    wired, monkeypatch
) -> None:
    import server.tracker.app.ask_route as ar

    async def _component(src, org, t, subject):
        return ([f"{t} line"], f"key:{t}") if t in (QA, DESK) else ([], "")

    monkeypatch.setattr(ar, "_component", _component)
    monkeypatch.setattr(
        "server.tracker.store.source.TrackerSource", lambda org, t: SimpleNamespace()
    )
    lines, key = await ar._searched_everywhere("component", "merges?", ORG, CODE, QA, "merges")
    assert lines == [f"{QA} line", f"{DESK}:", f"{DESK} line"] and key == f"key:{QA}"


# ---- the maintainer writes the ordered list ------------------------------------------


@pytest.mark.asyncio
async def test_set_source_writes_the_ordered_list(wired, monkeypatch) -> None:
    import server.code.app.repo_admin as adm
    from server.code.api.models_repos import CodeRepoSetSourceInput

    b.reset_overrides_for_tests({(ORG, CODE): f"{QA},{DESK}"})
    written: list[str] = []

    class _Repo:
        async def get_state(self, org, ns, key):
            return b.override_for(org, ns) or ""

        async def set_state(self, org, ns, key, value, detail=""):
            written.append(value)
            b.reset_overrides_for_tests({(ORG, CODE): value})

    async def _refresh(**_):
        return None

    monkeypatch.setattr("server.code.store.index.CodeRepo", _Repo)
    monkeypatch.setattr(adm.reg, "refresh_db_repos", _refresh)
    monkeypatch.setattr(adm, "_require_same_org", lambda org: None)
    monkeypatch.setattr(adm, "current_caller", lambda: ("alice", ORG))
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: SimpleNamespace(repo="Acme/widgets"),
    )
    NAMESPACES["tracker-kj"] = _Ns(repos=("Acme/kj",))
    from server.operations.base import Role

    admin_of = {QA, DESK, "tracker-kj"}  # the maintainer administers each tracker it links
    readers = {CODE, DESK, "tracker-new"}

    async def _scope(org, ns, role=None, *, hides_answer=True):
        return ns in admin_of if role is Role.ADMIN else ns in admin_of | readers

    monkeypatch.setattr(authz, "row_in_scope", _scope)
    try:
        out = await adm.handle_code_repo_set_source(
            CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[DESK, QA])
        )
        assert written[-1] == f"{DESK},{QA}" and out.previous_tracker_namespaces == [QA, DESK]
        assert out.tracker_namespaces == [DESK, QA] and out.not_covering == []
        out = await adm.handle_code_repo_set_source(
            CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[QA, "tracker-kj"])
        )
        assert written[-1] == f"{QA},tracker-kj" and out.not_covering == ["tracker-kj"]
        assert "tracker-kj names Acme/kj" in out.warning
        out = await adm.handle_code_repo_set_source(
            CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[])
        )
        assert written[-1] == "" and out.tracker_namespaces == []
        from server.core.errors import ValidationError

        monkeypatch.setattr("server.tracker.app.registry_sources.refresh_db_sources", _refresh)
        with pytest.raises(ValidationError, match="register its source first"):
            await adm.handle_code_repo_set_source(
                CodeRepoSetSourceInput(
                    org=ORG, namespace=CODE, tracker_namespaces=[QA, "tracker-gone"]
                )
            )
        # A new link to a tracker the maintainer reads but does not administer is refused:
        # linking it would open it to every reader of the repository. One it cannot read
        # answers as a missing one.
        from server.core.errors import AuthInsufficientError

        await adm.handle_code_repo_set_source(
            CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[QA, DESK])
        )
        admin_of.discard(DESK)
        NAMESPACES["tracker-new"] = NAMESPACES["tracker-hidden"] = _Ns()
        with pytest.raises(AuthInsufficientError, match="needs an admin"):
            await adm.handle_code_repo_set_source(
                CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=["tracker-new"])
            )
        with pytest.raises(ValidationError, match="register its source first"):
            await adm.handle_code_repo_set_source(
                CodeRepoSetSourceInput(
                    org=ORG, namespace=CODE, tracker_namespaces=["tracker-hidden"]
                )
            )
        # keeping, reordering and dropping a link already in force needs no admin of it
        out = await adm.handle_code_repo_set_source(
            CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[DESK, QA])
        )
        assert out.tracker_namespaces == [DESK, QA]
        out = await adm.handle_code_repo_set_source(
            CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[DESK])
        )
        assert out.tracker_namespaces == [DESK]
        # once dropped, it is a new link again
        await adm.handle_code_repo_set_source(
            CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[QA])
        )
        with pytest.raises(AuthInsufficientError, match="needs an admin"):
            await adm.handle_code_repo_set_source(
                CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[QA, DESK])
            )
        # a tracker only the operator map links is stored by this call: it needs its admin
        from server.code.domain.index.repos import RepoConfig

        b.reset_overrides_for_tests({})
        monkeypatch.setattr(b, "bindings", lambda: {CODE: DESK})
        entry = RepoConfig(
            org=ORG,
            namespace=CODE,
            repo="Acme/widgets",
            git_dir="",
            git_remote="",
            git_branch="main",
            path_prefixes=(),
            github_token="",
        )  # an operator entry (source "config"), so the map's link is in force
        monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: [entry])
        assert b.tracker_namespaces_for(ORG, CODE) == [DESK]
        with pytest.raises(AuthInsufficientError, match="needs an admin"):
            await adm.handle_code_repo_set_source(
                CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[DESK])
            )
    finally:
        for name in ("tracker-kj", "tracker-new", "tracker-hidden"):
            NAMESPACES.pop(name, None)


@pytest.mark.asyncio
async def test_a_token_naming_the_repository_brings_every_restricted_linked_tracker(
    wired, monkeypatch
) -> None:
    import server.identity.app.me_service_accounts as me
    from server.identity.app import me_callers

    async def _restricted(org, ns):
        return ns in (PRIV, DESK)

    async def _role(org, ns, subject):
        return "viewer" if ns == PRIV else None

    monkeypatch.setattr("server.identity.app.namespace_classification.is_restricted", _restricted)
    monkeypatch.setattr(me, "is_restricted", _restricted)
    monkeypatch.setattr(me_callers, "caller_role_on", _role)
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    parent = AuthInfo(
        subject="alice", org=ORG, namespaces=("*",), roles=(Role.WRITER,), is_service_account=False
    )
    assert await me._bound_trackers(ORG, "alice", parent, [CODE]) == [PRIV]


@pytest.mark.asyncio
async def test_memory_ask_names_a_tracker_it_could_not_read(wired, monkeypatch) -> None:
    import server.tracker.app.ask_route as ar

    async def _component(src, org, t, subject):
        if t == QA:
            raise RuntimeError("primary down")
        return [f"{t} line"], f"key:{t}"

    monkeypatch.setattr(ar, "_component", _component)
    monkeypatch.setattr(
        "server.tracker.store.source.TrackerSource", lambda org, t: SimpleNamespace()
    )
    lines, key = await ar._searched_everywhere("component", "merges?", ORG, CODE, QA, "merges")
    assert lines == [f"{DESK}:", f"{DESK} line", f"Not read (tracker unavailable): {QA}"]
    assert key == f"key:{DESK}"


@pytest.mark.asyncio
async def test_set_source_reads_its_own_stored_links_fail_closed(monkeypatch) -> None:
    """The repository's own stored links, read for its org, decide which trackers need
    their admin: an unread row refuses the write; a link stored under the same
    namespace name in another org counts for nothing here."""
    import server.code.app.repo_admin as adm
    from server.code.api.models_repos import CodeRepoSetSourceInput
    from server.core.errors import AuthInsufficientError

    written: list[str] = []
    stored: dict[tuple[str, str], str] = {("other-org", CODE): QA}
    down = [True]

    class _Repo:
        async def get_state(self, org, ns, key):
            if down[0]:
                raise ConnectionError("ClickHouse is down")
            return stored.get((org, ns), "")

        async def set_state(self, *a, **k):
            written.append("x")

    async def _none(**_):
        return {}

    async def _scope(org, ns, role=None, *, hides_answer=True):
        from server.operations.base import Role

        return role is not Role.ADMIN

    monkeypatch.setattr("server.code.store.index.CodeRepo", _Repo)
    monkeypatch.setattr(adm.reg, "refresh_db_repos", _none)
    monkeypatch.setattr(b, "refresh_overrides", _none)
    monkeypatch.setattr(adm, "_require_same_org", lambda org: None)
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.tracker_namespace", lambda org, n: NAMESPACES.get(n)
    )
    monkeypatch.setattr(authz, "row_in_scope", _scope)
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: SimpleNamespace(repo="Acme/widgets"),
    )
    b.reset_overrides_for_tests({("other-org", CODE): QA})  # another org's snapshot row names it
    try:
        with pytest.raises(ConnectionError):
            await adm.handle_code_repo_set_source(
                CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[QA])
            )
        down[0] = False
        with pytest.raises(AuthInsufficientError, match="needs an admin"):
            await adm.handle_code_repo_set_source(
                CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[QA])
            )
        assert written == []
    finally:
        b.reset_overrides_for_tests({})


@pytest.mark.asyncio
async def test_set_source_says_which_branches_read_the_links_too(wired, monkeypatch) -> None:
    """A link on an operator repository is read by its `code.repo.add_branch` branches;
    the tracker admin who sets it is told which."""
    import server.code.app.repo_admin as adm
    from server.code.api.models_repos import CodeRepoSetSourceInput
    from server.core import ch as ch_mod

    sequential: list[bool] = []

    class _Repo:
        async def get_state(self, org, ns, key):
            sequential.append(ch_mod.sequential_reads_active())
            return ""

        async def set_state(self, org, ns, key, value, detail=""):
            return None

    async def _noop(**_):
        return None

    async def _scope(org, ns, role=None, *, hides_answer=True):
        return True

    monkeypatch.setattr("server.code.store.index.CodeRepo", _Repo)
    monkeypatch.setattr(adm.reg, "refresh_db_repos", _noop)
    monkeypatch.setattr(b, "refresh_overrides", _noop)
    monkeypatch.setattr(adm, "_require_same_org", lambda org: None)
    monkeypatch.setattr(adm, "current_caller", lambda: ("alice", ORG))
    monkeypatch.setattr(
        "server.code.domain.index.repos.repo_for_namespace",
        lambda org, ns: SimpleNamespace(repo="Acme/widgets"),
    )
    monkeypatch.setattr(b, "branches_following", lambda org, ns: [f"{CODE}-26-8"])
    monkeypatch.setattr(authz, "row_in_scope", _scope)
    out = await adm.handle_code_repo_set_source(
        CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[QA])
    )
    assert f"the readers of {CODE}-26-8 read these links too" in out.warning
    out = await adm.handle_code_repo_set_source(
        CodeRepoSetSourceInput(org=ORG, namespace=CODE, tracker_namespaces=[])
    )
    assert "read these links too" not in out.warning  # a clear delegates nothing
    # the stored links that decide which link is new read their own writes
    assert sequential == [True, True]
