"""Profile client. Named, char-limited, agent-editable always-loaded context.

Distinct from `memory.search` (episodic recall, queried) — profile
sections are *always-loaded* context the LLM sees on every turn:

    # Episodic: fetched on demand.
    hits = await agent.memory.search("Bowie age")

    # Always-loaded: stable persona / preferences rendered every turn.
    await agent.profile.set("persona", "Helpful, terse, no emoji.", char_limit=2048)
    system = await agent.profile.system_prompt(base="You are my bot.")

A profile section is a memory row with `memory_type='profile'` and a
server-enforced `char_limit`. Writes past the cap return 422.

Per-end-user binding (`user_id=...`) gives one section per end-user
without polluting the `memory.search` ranking.
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from loom.agent import Agent


# Default cap on a profile section's stringified value.
DEFAULT_CHAR_LIMIT = 8192


class ProfileClient:
    def __init__(self, agent: Agent) -> None:
        self._agent = agent

    async def set(
        self,
        label: str,
        value: str,
        *,
        char_limit: int = DEFAULT_CHAR_LIMIT,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or replace the profile section at `label`.

        `char_limit` is enforced server-side on the stringified value
        (the canonical JSON form); 422 on overflow.

        Returns the full profile.set response — `version`, `memory_id`,
        `content_sha256`, `char_limit`, `value_length`.
        """
        payload: dict[str, Any] = self._agent.memory._identity(user_id, session_id)
        payload.update(
            {
                "label": label,
                "value": value,
                "char_limit": char_limit,
            }
        )
        return await self._agent.transport.post("/v1/profile.set", payload)

    async def get(
        self,
        label: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Read one profile section. Returns the memory.get response
        (including `value`, `char_limit`, `content_sha256`, `memory_type`)
        or None if absent."""
        return await self._agent.memory.get(
            memory_key=label,
            user_id=user_id,
            session_id=session_id,
        )

    async def list(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """All profile sections under this identity, ordered by memory_key.

        Returns the `entries` list directly — no envelope."""
        result = await self._agent.memory.list(
            memory_type="profile",
            user_id=user_id,
            session_id=session_id,
            limit=limit,
        )
        return list(result.get("entries") or [])

    async def append(
        self,
        label: str,
        addition: str,
        *,
        separator: str = "\n",
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomic append to a profile section.

        The server reads the current value, concatenates
        `separator + addition`, and writes back under optimistic
        concurrency control in one round trip. Preserves the existing
        `char_limit`. A missing section is treated as an initial set
        (created with the default char_limit).
        """
        payload: dict[str, Any] = self._agent.memory._identity(user_id, session_id)
        payload.update(
            {
                "label": label,
                "addition": addition,
                "separator": separator,
            }
        )
        return await self._agent.transport.post("/v1/profile.append", payload)

    async def replace(
        self,
        label: str,
        old: str,
        new: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Substring replace within a profile section.

        The server reads the current value, swaps `old` for `new`, and
        writes back under CAS. Preserves char_limit.

        Raises 422 SUBSTRING_NOT_FOUND if `old` isn't a substring of the
        section; 404 SECTION_NOT_FOUND if the section doesn't exist.
        """
        payload: dict[str, Any] = self._agent.memory._identity(user_id, session_id)
        payload.update(
            {
                "label": label,
                "old": old,
                "new": new,
            }
        )
        return await self._agent.transport.post("/v1/profile.replace", payload)

    async def delete(
        self,
        label: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """Tombstone a profile section. Soft-delete via memory.delete;
        restoreable via `memory.restore` within the grace window."""
        return await self._agent.memory.delete(
            memory_key=label,
            user_id=user_id,
            session_id=session_id,
        )

    async def render(
        self,
        *,
        labels: builtins.list[str] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        format: str = "xml",
        scope: str = "namespace",
        read_scope: Literal["namespace", "own_user", "own_session"] = "namespace",
    ) -> str:
        """Render all profile sections as a system-prompt fragment.

        One server-side round-trip — the server reads every section,
        formats, and returns the rendered string.

        Args:
          format: "xml" (default — `<section label="…">…</section>` lines)
                  or "markdown" (`## label\\nvalue` sections).
          labels: render only these, in this order. Default: alphabetical
                  by label.
          scope:  "namespace" (default — read sections written by any
                  agent in the namespace, so a persona authored via
                  /me/profile reaches every MCP client) or "this_agent"
                  (restrict to sections the calling agent wrote).
          read_scope: how far the render reaches across users. "own_user" /
                  "own_session" render only this user's (session's) sections
                  and the namespace-level ones, never another user's.
        """
        payload: dict[str, Any] = self._agent.memory._identity(user_id, session_id)
        payload.update(
            {
                "format": format,
                "scope": scope,
            }
        )
        if labels is not None:
            payload["labels"] = list(labels)
        if read_scope != "namespace":
            payload["read_scope"] = read_scope
        result = await self._agent.transport.post("/v1/profile.render", payload)
        return str(result.get("rendered") or "")

    async def system_prompt(
        self,
        base: str = "",
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        format: str = "xml",
        scope: str = "namespace",
        read_scope: Literal["namespace", "own_user", "own_session"] = "namespace",
        wrapper: str = "<context>\n{ctx}\n</context>",
    ) -> str:
        """Build a system-prompt string with the user's profile prepended.

        The profile is stable across turns: fetch it once per conversation,
        `system = await agent.profile.system_prompt(base="You are my bot.")`,
        and pass the cached string as the system prompt of every LLM call.
        Re-fetch only when you expect mid-conversation profile edits.

        Multi-user bots: when you pass `user_id=`, key your conversation
        cache by `user_id` too, or user A's cached `system` string can
        leak into user B's session.

        If the profile is empty (zero sections), returns `base` unchanged.
        When sections exist, wraps them in `<context>...</context>` by
        default and appends `base` after a blank line. Override `wrapper`
        to use a different shape; must contain the literal `{ctx}` token.
        """
        ctx = await self.render(
            user_id=user_id,
            session_id=session_id,
            format=format,
            scope=scope,
            read_scope=read_scope,
        )
        if not ctx:
            return base
        prefix = wrapper.format(ctx=ctx)
        return f"{prefix}\n\n{base}" if base else prefix
