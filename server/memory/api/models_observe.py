"""Ambient-capture wire ops over `memory_type='episodic'`.

`observe.record` is the per-tool-call hook endpoint: heuristic typing + a
single episodic insert, no LLM or synchronous embedding (hot path).
`observe.crystallize` is the session-end flush that enqueues observations
for the `observe_extract` worker's deferred distillation. The crystallize
lifecycle is tag-driven (`observation` → `crystallize:ready` →
`crystallize:done`). See `server/memory/app/observe.py` for the heuristics.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from server.core.naming import SENTINEL


class ObserveRecordInput(BaseModel):
    org: str
    namespace: str
    agent: str
    user_id: str = Field(default=SENTINEL)
    session_id: str = Field(default=SENTINEL)
    run_id: str = Field(default=SENTINEL)

    # Name of the tool that ran; it picks the observation `kind` by substring match.
    tool_name: str = Field(min_length=1, max_length=256)
    # Arbitrary JSON forwarded by the hook; stringified + truncated, never executed.
    tool_input: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    # What the tool returned; stringified and cut to 800 characters in the narrative.
    tool_output: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    # Merged with paths sniffed from the input/narrative.
    file_paths: list[str] = Field(default_factory=list, max_length=64)
    # Present iff the call failed; reclassifies the observation kind to `error`.
    error: str | None = None
    # Extra tags added after the server's own (`observation`, the kind, file basenames);
    # at most 32 tags are kept in all.
    tags: list[str] = Field(default_factory=list, max_length=16)
    # ISO YYYY-MM-DD stamped as valid_at; empty = leave NULL.
    observation_date: str = ""
    # The client's own action class (file_read, command_run, …), added as an
    # `action:<type>` tag.
    action_type: str = Field(default="", max_length=64)
    # Client context (project, repo, event); accepted, not stored.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # The moment the tool ran, so a capture posted later is dated when it
    # happened. Read only when `observation_date` is absent.
    observed_at: str = Field(default="", max_length=64)


class ObserveRecordOutput(BaseModel):
    # Key of the episodic row, `obs:<session_id>:<hash prefix>`; a re-fired hook for the
    # same call gets the same key.
    memory_key: str
    # Always empty: the row's id is assigned by the table and not read back.
    memory_id: str = ""
    # file_edit / command_run / error / web_fetch / file_read / tool_use.
    kind: str = ""
    # The tags stored on the row (at most 32).
    tags: list[str] = Field(default_factory=list)
    # File paths stored on the row: the ones sent plus those found in the input and
    # narrative; empty for a self-referential call.
    file_paths: list[str] = Field(default_factory=list)
    # Hash of the stored observation value (kind, tool, narrative, paths, error).
    content_sha256: str = ""
    # What the row holds, on the ledger as out_tool_name /
    # out_has_error / out_narrative_chars — an observation with a 40-char
    # narrative is a tool name, not an observation.
    tool_name: str = ""
    # True when `error` was non-empty (always False for a self-referential call).
    has_error: bool = False
    # Length of the stored narrative in characters (at most 2000).
    narrative_chars: int = 0
    # Why the row was stored content-less: "self_referential" (the tool was
    # loom itself or a harness meta tool), "blacklisted" (the content matched
    # a blacklist pattern); "" otherwise.
    skipped_reason: str = ""


class ObserveCrystallizeInput(BaseModel):
    org: str
    namespace: str
    agent: str
    user_id: str = Field(default=SENTINEL)
    session_id: str = Field(default=SENTINEL)

    # At most this many pending observations of the session are marked ready, oldest first.
    max_observations: int = Field(default=500, ge=1, le=2000)


class ObserveCrystallizeOutput(BaseModel):
    # How many observations were marked `crystallize:ready` for the extraction worker;
    # 0 when the session had none pending.
    enqueued: int = 0
    session_id: str = ""
