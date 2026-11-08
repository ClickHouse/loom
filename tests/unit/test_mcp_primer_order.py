"""The session-start instructions put the caller's profile last, so on the `coding`
toolset a client that cuts long instructions (Claude Code at 2,048 characters) keeps
every rule whatever the profile holds."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.transport import mcp_primer

CLIENT_LIMIT = 2048  # Claude Code's cut for MCP server instructions
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
        (None, ["**7. Using Loom.**", "\n\nWORKSPACE"]),
        ("coding", ["one plain-text claim", f"Default namespace `{NAMESPACE}`"]),
    ],
)
def test_the_profile_comes_after_every_rule(
    toolset: str | None, expected_before: list[str], wired_caller: list[Any]
) -> None:
    text = asyncio.run(mcp_primer._initialize_instructions(object(), toolset))
    before_profile, profile = _split(text)
    assert "be terse." in profile and "be terse." not in before_profile
    for part in [*expected_before, "MENTOR", "Code index scope for this connection"]:
        assert part in before_profile, part
    assert len(wired_caller) == 1  # one render, so one restricted-access check per initialize


def test_the_primer_surfaces_end_with_the_profile() -> None:
    """`loom://primer`, the `loom` prompt and `loom_activate` serve this text."""
    text = asyncio.run(mcp_primer._loom_primer_text(object(), toolset="core"))
    before_profile, profile = _split(text)
    assert "**7. Using Loom.**" in before_profile and profile.endswith("it's been delivered.")


def test_coding_rules_fit_2048_characters_before_any_profile() -> None:
    text = asyncio.run(mcp_primer._initialize_instructions(object(), "coding"))
    assert len(text) > CLIENT_LIMIT  # the profile alone runs past the cut
    assert len(_split(text)[0]) <= CLIENT_LIMIT
