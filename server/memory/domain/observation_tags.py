"""How an observation's lifecycle and exclusions are spelled in its tags.

Observations are episodic memory rows; their state lives in tags so every
reader (the capture handler, the crystallize worker, the scrubber) filters on
the same strings. This module owns those strings and the one rule for which
tool calls are observed without content.
"""

from __future__ import annotations

import fnmatch

from server.settings import settings

# Crystallize lifecycle lives in tags (episodic rows already carry tags):
# PENDING (just-captured) → READY (flushed at session-end) → DONE (worker
# distilled it). The deferred worker filters on these.
OBSERVATION_TAG = "observation"
CRYSTALLIZE_READY_TAG = "crystallize:ready"
CRYSTALLIZE_DONE_TAG = "crystallize:done"
# An observation OF the memory plane is not a memory. A tool
# call that reads or writes loom itself (the MCP memory/knowledge/search
# tools) carries a memory's value as its input; observing it stores that
# value a second time, outside the key the user can delete (a deleted note
# would come back as a crystallized fact and a session digest). Meta tools (tool discovery, plan
# mode, questions to the user) carry nothing about the task. Both kinds are
# still recorded — the session's step count stays honest — but content-less
# and already DONE, so no distiller reads them.
SELF_REFERENTIAL_TAG = "skipped:self_referential"
SELF_REFERENTIAL_REASON = "self_referential"
# A step whose content matches a blacklist ("Refuse") pattern is recorded the
# same way: the step, not the content.
BLACKLISTED_TAG = "skipped:blacklisted"
BLACKLISTED_REASON = "blacklisted"


def _skip_patterns() -> list[str]:
    """The tool-name globs `observe.record` stores content-less (setting
    `observe_skip_tools`, comma-separated, case-insensitive fnmatch)."""
    raw = str(settings.observe_skip_tools)
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def is_self_referential(tool_name: str, patterns: list[str] | None = None) -> bool:
    """True when `tool_name` matches one of the skip globs: loom's own MCP
    tools (`mcp__loom__*`, `loom_memory_*`, ...) and the harness's meta tools."""
    name = (tool_name or "").strip().lower()
    if not name:
        return False
    pats = _skip_patterns() if patterns is None else patterns
    return any(fnmatch.fnmatchcase(name, pat) for pat in pats)
