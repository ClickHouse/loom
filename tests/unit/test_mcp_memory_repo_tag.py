"""An MCP `memory.set` with no `repo:` tag takes the `repo:<id>` of the connection's code
scope, so the memory can be linked to the repository's code. The repository comes from the
configured repositories, never from the scope's namespace read as an id; an explicit tag
stays; the write's org, authorization and provenance are the call's own."""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from typing import Any

import pytest

import server.transport.mcp_call as mcp_call
from server.auth.jwt import mint_token, verify_token
from server.code.app import memory_links
from server.code.domain.index import repos as repos_mod
from server.code.domain.index.remote import repo_tag
from server.code.domain.scope import reset_code_scope, set_code_scope
from server.memory.app import code_link_pass, code_link_reads, code_links
from server.settings import settings
from tests.unit.ch_fake import RecordingCH
from tests.unit.code_fixtures import repo_config

LOOM = "https://github.com/acme/loom.git"
OTHER = "https://github.com/acme/other.git"
FOREIGN = "https://github.com/globex/loom.git"
LOOM_TAG, OTHER_TAG, FOREIGN_TAG = repo_tag(LOOM), repo_tag(OTHER), repo_tag(FOREIGN)
TOOL = "loom_memory_set"
FIXED = "def fetch_rows(n):\n    return n + 1\n"


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch) -> None:
    configured = [
        repo_config(
            org="acme", namespace="code-loom", repo="acme/loom", git_remote=LOOM, git_branch="main"
        ),
        repo_config(
            org="acme",
            namespace="code-other",
            repo="acme/other",
            git_remote=OTHER,
            git_branch="main",
        ),
        repo_config(
            org="globex",
            namespace="code-loom",
            repo="globex/loom",
            git_remote=FOREIGN,
            git_branch="main",
        ),
        # a branch of acme/loom: its namespace names the same repository
        repo_config(
            org="acme",
            namespace="code-loom-r1",
            repo="acme/loom",
            git_remote=LOOM,
            git_branch="release/1",
        ),
    ]
    monkeypatch.setattr(repos_mod, "configured_repos", lambda: configured)
    monkeypatch.setattr(memory_links, "configured_repos", lambda: configured)


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch, repos: None) -> list[Any]:
    """The validated `memory.set` input each call reaches the handler with."""
    seen: list[Any] = []
    op = mcp_call.TOOLS_BY_NAME[TOOL]

    async def handler(inp: Any) -> Any:
        seen.append(inp)
        return op.output_model.model_construct()

    monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, TOOL, dataclasses.replace(op, handler=handler))

    async def defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "acme", "namespace": "personal-ann", "agent": "claude-code"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", defaults)
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(type(settings), "auth_actually_required", lambda self: True)
    RecordingCH(strict=False).install(monkeypatch)
    return seen


def _auth(*namespaces: str, org: str = "acme") -> Any:
    return verify_token(
        mint_token(
            subject="ann", org=org, namespaces=list(namespaces), roles=["writer"], ttl_seconds=300
        )
    )


async def _asave(scope: str | None, auth: Any = None, **args: Any) -> dict[str, Any]:
    """One MCP `memory.set` over a connection naming `scope` in its header."""
    body = {"memory_key": "k", "value": "I prefer short answers", **args}
    token = set_code_scope(scope)
    try:
        await mcp_call._call_tool(
            1,
            {"name": TOOL, "arguments": body},
            auth=auth or _auth("personal-ann", "code-loom", "code-other"),
        )
    finally:
        reset_code_scope(token)
    return body


def _save(scope: str | None, auth: Any = None, **args: Any) -> dict[str, Any]:
    return asyncio.run(_asave(scope, auth, **args))


def test_a_write_with_no_repo_tag_takes_the_configured_repository_of_the_scope(saved) -> None:
    _save("acme/code-loom", tags=["billing"])
    assert [i.tags for i in saved] == [["billing", LOOM_TAG]]
    assert saved[0].namespace == "personal-ann" and saved[0].org == "acme"


def test_a_branch_namespace_names_its_repository(saved) -> None:
    _save("acme/code-loom-r1")
    assert [i.tags for i in saved] == [[LOOM_TAG]]


def test_an_explicit_repo_tag_wins_even_when_it_names_another_repository(saved) -> None:
    _save("acme/code-loom", tags=[OTHER_TAG])
    _save("acme/code-loom", tags=["repo:zzzz"])  # not a configured id: still the caller's
    assert [i.tags for i in saved] == [[OTHER_TAG], ["repo:zzzz"]]


def test_a_scope_no_repository_is_configured_for_adds_nothing(saved) -> None:
    # `personal-ann` is a real namespace, not a repository: its name is never an id
    _save("acme/personal-ann")
    _save("acme/code-unconfigured")
    assert [i.tags for i in saved] == [[], []]


def test_a_scope_of_another_org_is_not_inherited(saved) -> None:
    # globex configures a code-loom too; the write is acme's
    _save("globex/code-loom")
    assert [i.tags for i in saved] == [[]]
    assert FOREIGN_TAG not in saved[0].tags


def test_a_write_into_another_code_namespace_stays_with_that_repository(saved) -> None:
    _save("acme/code-loom", namespace="code-other")
    _save("acme/code-other", namespace="code-loom")
    assert [i.tags for i in saved] == [[OTHER_TAG], [LOOM_TAG]]


def test_a_write_into_a_configured_code_namespace_needs_a_configured_readable_scope(saved) -> None:
    # `code-unconfigured` names no repository: the target's tag is not inherited from it
    _save("acme/code-unconfigured", namespace="code-other")
    _save("acme/personal-ann", namespace="code-other")
    # a configured scope the caller cannot read adds nothing either
    _save("acme/code-loom", namespace="code-other", auth=_auth("personal-ann", "code-other"))
    assert [i.tags for i in saved] == [[], [], []]
    # a readable configured scope and a different configured target: the target's repository
    _save("acme/code-loom", namespace="code-other")
    assert saved[-1].tags == [OTHER_TAG]


def test_a_write_into_a_code_namespace_with_a_scope_of_another_org_adds_nothing(saved) -> None:
    _save("globex/code-loom", namespace="code-other")
    assert [i.tags for i in saved] == [[]]


def test_a_scope_the_caller_cannot_read_adds_nothing_and_does_not_refuse_the_write(saved) -> None:
    _save("acme/code-loom", auth=_auth("personal-ann"))
    assert [i.tags for i in saved] == [[]]


def test_no_scope_and_a_malformed_scope_leave_the_call_as_it_was(saved) -> None:
    for scope in (None, "", "code-loom", "acme/", "/code-loom", "acme/code-loom/x", "ac me/x"):
        _save(scope, tags=["billing"])
    assert [i.tags for i in saved] == [["billing"]] * 7


def test_only_memory_set_takes_the_tag(saved, monkeypatch: pytest.MonkeyPatch) -> None:
    op = mcp_call.TOOLS_BY_NAME["loom_memory_search"]
    args: dict[str, Any] = {"org": "acme", "namespace": "personal-ann", "tags": ["billing"]}
    asyncio.run(mcp_call._tag_session_repository(SimpleNamespace(name=op.name), args, None))
    assert args["tags"] == ["billing"]


def test_tags_that_are_not_a_list_of_strings_are_left_to_validation(saved) -> None:
    body = _save("acme/code-loom", tags="billing")
    assert body["tags"] == "billing" and saved == []
    body = _save("acme/code-loom", tags=["a", 3])
    assert body["tags"] == ["a", 3] and saved == []


def test_the_write_is_otherwise_the_calls_own(saved) -> None:
    _save("acme/code-loom", tags=["billing"], files=None)
    _save(None, tags=["billing"], files=None)
    tagged, plain = saved
    assert tagged.model_dump(exclude={"tags"}) == plain.model_dump(exclude={"tags"})
    # the server stamps `actor` and `source_class` below the transport; no tag path sets them
    for field in ("actor", "source_class"):
        assert getattr(tagged, field, None) == getattr(plain, field, None)


def test_each_request_sees_its_own_scope_when_calls_run_together(saved) -> None:
    async def one(scope: str, key: str) -> None:
        token = set_code_scope(scope)
        try:
            await asyncio.sleep(0)
            await mcp_call._call_tool(
                1,
                {"name": TOOL, "arguments": {"memory_key": key, "value": "v"}},
                auth=_auth("personal-ann", "code-loom", "code-other"),
            )
        finally:
            reset_code_scope(token)

    async def both() -> None:
        await asyncio.gather(one("acme/code-loom", "a"), one("acme/code-other", "b"))

    asyncio.run(both())
    assert {i.memory_key: i.tags for i in saved} == {"a": [LOOM_TAG], "b": [OTHER_TAG]}


# ---- the tag reaches the link pass --------------------------------------------------


def _memory(tags: list[str], value: str) -> dict[str, Any]:
    return {
        "mid": "m", "org": "acme", "namespace": "personal-ann", "agent": "claude-code",
        "user_id": "-", "session_id": "-", "memory_key": "k", "status": "active",
        "tombstoned_at": None, "tags": tags, "files": [], "value": value, "pinned": 0,
        "lock_mode": "", "lock_until": None, "verified_by": "", "verified_sha256": "",
        "sha": "s", "inputs": "i",
    }  # fmt: skip


async def _linked(monkeypatch: pytest.MonkeyPatch, tags: list[str], value: str) -> tuple[str, list]:
    """The repository the link pass reads for a memory with `tags` in a personal namespace,
    and the links the real pass makes from `value` against that repository's file."""
    cns = code_link_reads.code_namespace_for("acme", "personal-ann", tags)
    if not cns:
        return cns, []
    files = code_link_pass.RepoReads("acme", cns)

    async def at(commit: str, path: str) -> Any:
        from server.code.app.memory_links import FileAt, file_definitions

        if path != "pkg/a.py":
            return None
        return FileAt(FIXED.split("\n"), file_definitions(path, FIXED.encode()))

    async def names(commit: str, path: str) -> Any:
        found = await at(commit, path)
        return {d.qualified_name for d in found.defs} if found is not None else None

    async def candidates(org: str, namespace: str, *, names: list[str]) -> list:
        from server.code.domain.anchor import Definition

        return [Definition("pkg/a.py", "pkg.a::fetch_rows", "fetch_rows")] * ("fetch_rows" in names)

    async def indexed_head(org: str, namespace: str) -> str:
        return "c1"

    monkeypatch.setattr(files, "at", at)
    monkeypatch.setattr(files, "names", names)
    monkeypatch.setattr(code_links.code, "candidates", candidates)
    monkeypatch.setattr(code_links.code, "indexed_head", indexed_head)
    rows, _ = await code_links._link("acme", cns, [_memory(tags, value)], {}, files)
    return cns, rows


@pytest.mark.asyncio
async def test_a_memory_naming_a_symbol_links_through_the_server_added_tag(
    saved, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _asave("acme/code-loom", value="`pkg.a::fetch_rows` pages its reads")
    (call,) = saved
    cns, rows = await _linked(monkeypatch, call.tags, call.value)
    assert cns == "code-loom"
    assert [(r["code_namespace"], r["path"], r["qualified_name"]) for r in rows] == [
        ("code-loom", "pkg/a.py", "pkg.a::fetch_rows")
    ]


@pytest.mark.asyncio
async def test_a_preference_gets_the_tag_and_no_symbol_link(
    saved, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _asave("acme/code-loom", value="I prefer short answers")
    (call,) = saved
    assert call.tags == [LOOM_TAG]
    cns, rows = await _linked(monkeypatch, call.tags, call.value)
    assert cns == "code-loom" and rows == []


@pytest.mark.asyncio
async def test_without_the_tag_the_same_memory_names_no_repository(
    saved, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _asave(None, value="`pkg.a::fetch_rows` pages its reads")
    (call,) = saved
    assert call.tags == []
    assert await _linked(monkeypatch, call.tags, call.value) == ("", [])
