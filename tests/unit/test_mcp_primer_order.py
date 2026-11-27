"""Session-start instructions preserve all content and put routing before the profile."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.domain.scope import reset_code_scope, set_code_scope
from server.settings import settings
from server.transport import mcp_primer

PROFILE = "<section label='persona'>" + "be terse. " * 600 + "</section>"
NAMESPACE = "agent-a-long-default-namespace-name"


@pytest.fixture(autouse=True)
def wired_caller(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """The caller's workspace and profile on a connection bound to a repository;
    yields the restricted-access checks made."""
    restricted_checks: list[Any] = []

    async def defaults(*_a: Any, **_k: Any) -> dict[str, str]:
        return {"org": "acme", "namespace": NAMESPACE, "agent": "a"}

    async def render(_inp: Any) -> Any:
        return SimpleNamespace(section_count=1, rendered=PROFILE)

    async def unrestricted(*args: Any) -> bool:
        restricted_checks.append(args)
        return False

    async def mentor(*_a: Any, **_k: Any) -> str:
        return "\n\nMENTOR"

    async def workspace(*_a: Any, **_k: Any) -> str:
        return "\n\nWORKSPACE"

    monkeypatch.setattr(mcp_primer, "_caller_defaults", defaults)
    monkeypatch.setattr("server.memory.app.profile.handle_profile_render", render)
    monkeypatch.setattr("server.identity.app.namespace_classification.is_restricted", unrestricted)
    monkeypatch.setattr(mcp_primer, "_mentor_line", mentor)
    monkeypatch.setattr(mcp_primer, "_caller_default_context", workspace)
    bound = set_code_scope("acme/code-repo")
    yield restricted_checks
    reset_code_scope(bound)


def _split(text: str) -> tuple[str, str]:
    """The text before the profile and the profile."""
    at = text.index("**My always-loaded context")
    return text[:at], text[at:]


@pytest.mark.parametrize(
    ("toolset", "expected_before"),
    [
        (None, ["**7. Using Loom.**", "\n\nWORKSPACE", "MENTOR", "Code index scope"]),
        ("coding", ["one plain-text claim", "default memory namespace", "repository bound"]),
    ],
)
def test_the_profile_comes_after_every_rule(
    toolset: str | None, expected_before: list[str], wired_caller: list[Any]
) -> None:
    text = asyncio.run(mcp_primer._initialize_instructions(object(), toolset))
    before_profile, profile = _split(text)
    assert "be terse." in profile and "be terse." not in before_profile
    for part in expected_before:
        assert part in before_profile, part
    assert len(wired_caller) == 1  # one render, so one restricted-access check per initialize


def test_the_primer_surfaces_end_with_the_profile() -> None:
    """`loom://primer`, the `loom` prompt and `loom_activate` serve this text."""
    text = asyncio.run(mcp_primer._loom_primer_text(object(), toolset="core"))
    before_profile, profile = _split(text)
    assert "**7. Using Loom.**" in before_profile and profile.endswith("it's been delivered.")


def test_coding_instructions_preserve_the_complete_large_profile() -> None:
    text = asyncio.run(mcp_primer._initialize_instructions(object(), "coding"))
    before_profile, profile = _split(text)
    assert PROFILE in profile
    assert "repository bound to this connection" in before_profile
    assert "one plain-text claim" in before_profile
    assert text.endswith("Don't re-fetch — it's been delivered.")


def test_compact_coding_primer_preserves_scope_and_profile() -> None:
    full = asyncio.run(
        mcp_primer._initialize_instructions(object(), "coding", schema_profile="standard")
    )
    compact = asyncio.run(
        mcp_primer._initialize_instructions(object(), "coding", schema_profile="compact")
    )
    rules, profile = _split(compact)
    assert profile == _split(full)[1]
    assert len(compact) < len(full)
    assert "repository bound to this connection" in rules
    assert "`loom_code_edit_context`" in rules
    edit_read = "If the editor needs a local read first, start with one line near the edit."
    assert edit_read in rules
    assert edit_read in _split(full)[0]
    assert "`loom_code_grep`" in rules
    assert "`loom_code_read` to read known line ranges together" in rules
    assert "unindexed paths" in rules
    assert "ToolSearch" in rules
    assert "code_state" in rules
    callers = "`loom_code_callers` for callers and indexed uses of a known definition"
    assert callers in rules and callers in _split(full)[0]
    assert "loom_code_impact" not in rules


def test_bound_coding_primers_use_task_pack_for_additional_history(monkeypatch) -> None:
    defaults = {"org": "project", "namespace": "history", "agent": "coding"}
    # a compact profile the URL did not ask for: the deployment's default
    monkeypatch.setattr(settings, "mcp_schema_profile", "compact")
    bound = mcp_primer._coding_instructions("coding", defaults)
    standard = mcp_primer._coding_instructions("coding", defaults, schema_profile="standard")
    assert "First step of each task" not in bound
    assert "For prior decisions or procedures beyond linked code lessons" in bound
    assert "For prior decisions or procedures beyond linked code lessons" in standard
    reuse = "For a repeat source lookup, set `include_lessons: false`"
    activation = asyncio.run(mcp_primer._loom_primer_text(object(), toolset="coding"))
    assert "For prior decisions or procedures beyond linked code lessons" in activation
    for primer in (bound, standard, activation):
        assert primer.index(reuse) < primer.index("with the task as `query`. Skip it when")
    assert "first with the task" not in activation
    assert reuse not in mcp_primer._coding_instructions("coding")
    set_code_scope(None)
    unbound = mcp_primer._coding_instructions("coding", defaults)
    assert "First step of each task" in unbound
    assert "`loom_op(" in unbound and "`loom_search`" in unbound


def test_bound_compact_coding_primer_routes_code_tools_alone() -> None:
    """Its listing carries the code tools alone, so the text names no other tool."""

    def routing(defaults: dict[str, str] | None) -> str:
        return mcp_primer._coding_instructions("coding", defaults, schema_profile="compact")

    defaults = {"org": "project", "namespace": "history", "agent": "coding"}
    reuse = "For a repeat source lookup, set `include_lessons: false`"
    bound = routing(defaults)
    assert bound.startswith("Use Loom first for unchanged indexed code and tests.")
    assert reuse in bound
    no_memory_home = routing({**defaults, "namespace": ""})
    assert reuse not in no_memory_home
    assert reuse not in routing(None)
    started = asyncio.run(
        mcp_primer._initialize_instructions(object(), "coding", schema_profile="compact")
    )
    # the `loom` prompt, `loom://primer` and `loom_activate`, served by name
    activation = asyncio.run(
        mcp_primer._loom_primer_text(object(), toolset="coding", schema_profile="compact")
    )
    for text in (bound, no_memory_home, _split(started)[0], _split(activation)[0]):
        assert "`loom_code_edit_context`" in text
        for absent in ("loom_op", "loom_search", "loom_memory", "loom_context_pack"):
            assert absent not in text, absent
    assert "repository bound to this connection when namespace is omitted" in started
    assert "default memory namespace" not in started
    assert _split(activation)[1] == _split(started)[1]


def test_a_compact_deployment_default_keeps_the_bound_coding_primer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The URL names no profile: the connection lists every tool, so its
    instructions route memory and name `loom_op`."""
    monkeypatch.setattr(settings, "mcp_schema_profile", "compact")
    started = asyncio.run(mcp_primer._initialize_instructions(object(), "coding"))
    rules = _split(started)[0]
    assert "Use Loom first for unchanged indexed code and tests." in rules
    assert "`loom_op(" in rules and "`loom_search`" in rules and "`loom_memory_set`" in rules
    assert "For prior decisions or procedures beyond linked code lessons" in rules
    assert "default memory namespace" in rules
    asked = asyncio.run(
        mcp_primer._initialize_instructions(object(), "coding", schema_profile="compact")
    )
    assert "`loom_op(" not in asked and len(asked) < len(started)
    activation = asyncio.run(mcp_primer._loom_primer_text(object(), toolset="coding"))
    assert "**Loom-first mode active" in activation


def test_bound_coding_activation_with_no_default_memory_home() -> None:
    rules = mcp_primer._loom_rules(
        "coding", {"org": "project", "namespace": "", "agent": "coding", "readable": ["source"]}
    )
    first_rule = rules.split("**2. Recall lookup order.**")[0]
    assert "Read indexed source and its linked lessons" in first_rule
    assert "loom_context_pack" not in first_rule


def test_compact_coding_catalog_describes_the_task_pack_without_ordering() -> None:
    from server.transport.mcp_tools import _tools_descriptor

    set_code_scope(None)  # the listing of code tools alone carries no task pack

    async def describe(profile: str) -> str:
        tools = await _tools_descriptor(toolset="coding", schema_profile=profile)
        return next(tool["description"] for tool in tools if tool["name"] == "loom_context_pack")

    standard = asyncio.run(describe("standard"))
    compact = asyncio.run(describe("compact"))
    for description in (standard, compact):
        assert "call first" not in description
        assert "call it first" not in description
    assert compact == (
        "Retrieve task context by passing the task as `query`. It returns memories, decisions, "
        "procedures, working memory and the profile; `include_knowledge` / `include_conversations` "
        "add docs and chats."
    )


def test_code_scope_precedes_a_long_memory_workspace_notice(monkeypatch) -> None:
    monkeypatch.setattr(mcp_primer, "_coding_workspace_line", lambda _: "WORKSPACE" * 1000)
    text = asyncio.run(mcp_primer._initialize_instructions(object(), "coding"))
    assert "WORKSPACE" * 1000 in text
    scope_at = text.index("repository bound to this connection")
    workspace_at = text.index("WORKSPACE" * 1000)
    assert scope_at < workspace_at < text.index(PROFILE)
    assert text.index("use `loom_code_edit_context` with a definition or literal clues") < scope_at
    assert text.index("`loom_code_grep` for exact text or an error") < scope_at


@pytest.mark.parametrize("toolset", ["coding", "coding+tracker"])
def test_coding_startup_reuses_routing_text_across_implicit_bindings(
    monkeypatch: pytest.MonkeyPatch, toolset: str
) -> None:
    async def different_default(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"org": "other-org", "namespace": "other-workspace", "agent": "b"}

    async def mentor_should_be_on_demand(*_args: Any, **_kwargs: Any) -> str:
        pytest.fail("Coding startup does not look up usage hints")

    monkeypatch.setattr(mcp_primer, "_mentor_line", mentor_should_be_on_demand)
    first = asyncio.run(mcp_primer._initialize_instructions(object(), toolset))
    monkeypatch.setattr(mcp_primer, "_caller_defaults", different_default)
    set_code_scope("other-org/other-repo")
    second = asyncio.run(mcp_primer._initialize_instructions(object(), toolset))
    assert first == second
    assert NAMESPACE not in first
    assert "acme/code-repo" not in first


@pytest.mark.parametrize(
    ("configured", "requested"),
    [("coding", None), ("coding+tracker", "unknown"), ("core", "coding+unknown")],
)
def test_startup_resolves_toolset_like_the_listing(
    monkeypatch: pytest.MonkeyPatch, configured: str, requested: str | None
) -> None:
    monkeypatch.setattr(settings, "mcp_toolset", configured)
    expected = asyncio.run(mcp_primer._initialize_instructions(object(), configured))
    assert asyncio.run(mcp_primer._initialize_instructions(object(), requested)) == expected
