"""context.pack, context offload, session briefs and the mentor's hints."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from server.settings._live import LIVE


class ContextSettings(BaseModel):
    # context.offload: largest single parked tool result, in bytes.
    context_offload_max_bytes: Annotated[int, LIVE] = 8_000_000
    # session.brief_save. `session_brief_llm` off = every brief comes from the
    # deterministic builder (goal, status, files, errors, rules, pointers; no
    # decisions or dead ends). The model is the
    # deployment's LLM when `session_brief_llm_model` has no key. A save
    # condenses its events into windows of `session_brief_window_chars`
    # (≈6k tokens each) and revises the brief once per window, at most
    # `session_brief_max_windows` calls per save (the last window takes the
    # remainder, condensed harder).
    session_brief_llm: Annotated[bool, LIVE] = True
    # Condensed size, characters, of one brief window (floor 2000); a window holds about three
    # times this much raw event text before it is condensed.
    session_brief_window_chars: Annotated[int, LIVE] = Field(default=24_000, ge=2000)
    # Most windows (and model revision calls) per brief save; the last window takes the rest.
    session_brief_max_windows: Annotated[int, LIVE] = 4
    # Seconds a per-process (org, namespace, agent) mentor profile is cached
    # (a failed read ≤ 300 s). 0 disables the mentor everywhere (no pack
    # section, no call hints, empty mentor.brief).
    mentor_ttl_seconds: Annotated[int, LIVE] = 3600
    # At most this many hints per session, each code once.
    mentor_max_hints_per_session: Annotated[int, LIVE] = 6
