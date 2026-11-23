"""Session-start instructions preserve all content and put routing before the profile."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.transport import mcp_primer

PROFILE = "<section label='persona'>" + "be terse. " * 600 + "</section>"
NAMESPACE = "agent-a-long-default-namespace-name"


@pytest.fixture(autouse=True)
def wired_caller(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The caller's workspace and profile; returns the restricted-access checks made."""
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
    monkeypatch.setattr(mcp_primer, "current_code_scope", lambda: ("acme", "code-repo"))
    return restricted_checks


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


def test_code_scope_precedes_a_long_memory_workspace_notice(monkeypatch) -> None:
    monkeypatch.setattr(mcp_primer, "_coding_workspace_line", lambda _: "WORKSPACE" * 1000)
    text = asyncio.run(mcp_primer._initialize_instructions(object(), "coding"))
    assert "WORKSPACE" * 1000 in text
    scope_at = text.index("repository bound to this connection")
    workspace_at = text.index("WORKSPACE" * 1000)
    assert scope_at < workspace_at < text.index(PROFILE)
    assert text.index("`loom_code_symbol` for a named definition") < scope_at
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
    monkeypatch.setattr(mcp_primer, "current_code_scope", lambda: ("other-org", "other-repo"))
    second = asyncio.run(mcp_primer._initialize_instructions(object(), toolset))
    assert first == second
    assert NAMESPACE not in first
    assert "acme/code-repo" not in first
