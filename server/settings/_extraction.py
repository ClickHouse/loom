"""Fact extraction from messages and observed events."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.settings._env import CaseFolded
from server.settings._live import LIVE


class ExtractionSettings(BaseModel):
    # Value-update supersession on the extraction write path (memory.app.value_updates):
    # a fresh memory restating an existing one with a different value token marks the
    # older one superseded. Costs one ANN lookup per written row; a per-request
    # `detect_updates` overrides it. `_min_cosine` is the neighbour floor, set below the
    # conflict threshold.
    memory_extract_value_updates: Annotated[bool, LIVE] = False
    # Least cosine similarity a neighbour needs to be checked as the older value of a fresh memory.
    memory_extract_value_updates_min_cosine: Annotated[float, LIVE] = Field(
        default=0.75, gt=0.0, le=1.0
    )

    # The observation date a write is anchored to when the caller sends
    # none. "request_time" = today's UTC date, so valid_at is never NULL for a live
    # agent; "none" = undated.
    memory_extract_observation_date_default: Annotated[
        Literal["request_time", "none"], CaseFolded, LIVE
    ] = "request_time"

    # Capture long / structured / enumerated assistant answers whole (not clipped at
    # EXCERPT_LIMIT) so a positional follow-up resolves: at write time the prompt gains
    # a structured-answer block and assistant-answer facts get a higher content_excerpt
    # ceiling. The value column is untouched; takes effect only on (re-)ingest.
    memory_capture_structured_answers: Annotated[bool, LIVE] = True
    # Current messages totalling at most this many characters are
    # extracted with the compact system prompt (every rule, the schema anchors a short
    # statement can match, no structured-answer block); longer ones keep the full
    # prompt. 0 turns it off.
    memory_extract_compact_prompt_max_chars: Annotated[int, LIVE] = 1200
    # Existing memories (fact rows only) an extraction in that short tier sees for
    # update detection (sized on an extraction eval); 0 lets it see the caller's full
    # limit. A longer transcript sees none: shown the namespace's facts, the extractor
    # links later details to them instead of writing them.
    memory_extract_existing_short_limit: Annotated[int, LIVE] = 8
    # A session's procedure and hint (observe notes) are mined
    # only once the session holds this many non-trivial observations. 0 = any.
    observe_extract_notes_min_observations: Annotated[int, LIVE] = 15
    # A coding capture keeps the coverage sweep's density / uncovered
    # auto-gates only with at least this many user turns (on short sessions the
    # sweep's output is mostly junk). 0 = always keep. An explicit namespace /
    # per-call switch wins either way.
    memory_extract_coding_sweep_min_user_turns: Annotated[int, LIVE] = 4
    # Per verifier call; a timeout keeps every fact.
    memory_extract_verify_timeout_s: Annotated[float, LIVE] = 45.0
    # The coverage sweep: turns the first pass cited nowhere get a second
    # reading of their own (one more call), when the namespace or the call turns it
    # on or the density / uncovered gates below fire. `_min_chars` skips
    # "thanks!"-sized turns; `_max_turns` bounds the call.
    memory_extract_coverage_sweep_min_chars: Annotated[int, LIVE] = Field(default=40, ge=1)
    # Most uncovered turns one sweep call reads; later ones are left out. 0 = no cap.
    memory_extract_coverage_sweep_max_turns: Annotated[int, LIVE] = 60
    # Inside a turn the first pass cited, the individual statements no fact covers are
    # swept too (a fact list, a long update), at most this many.
    memory_extract_coverage_sweep_max_sentences: Annotated[int, LIVE] = Field(default=60, ge=1)
    # With the namespace and the call silent, the
    # sweep still runs when the ingest is statement-dense — some turn holds
    # at least this many statements (a fact list, a long update). 0 = never.
    # Chatty conversation (one to four sentences a turn) never reaches it.
    memory_extract_coverage_sweep_auto_min_statements: Annotated[int, LIVE] = 10
    # With the namespace and the call silent and no list-shaped
    # turn, a conversation ingest still gets the sweep when the first pass left at least
    # this many statements without a memory (lexical, LLM-free). 0 = never: on
    # conversation the gate fires on nearly every session, so enabling it is the
    # operator's token trade.
    memory_extract_coverage_sweep_auto_min_uncovered: Annotated[int, LIVE] = 0
    # The listed turns and sentences are read in groups of this
    # many items, one concurrent call each (more recall for more tokens). 0 = one call.
    memory_extract_coverage_sweep_batch_items: Annotated[int, LIVE] = 0

    # Size segments: a session longer than this many messages is
    # extracted in fixed windows of `_turns` turns (a turn = one user message + the
    # assistant replies that follow). 0 disables. One call per window, concurrently.
    memory_extract_size_segment_messages: Annotated[int, LIVE] = 60
    # Turns per size segment window (a window never splits a turn).
    memory_extract_size_segment_turns: Annotated[int, LIVE] = Field(default=20, ge=1)
    # Most size segment windows per session; past it the last window takes the remainder
    # (windows grow past `_turns` turns first).
    memory_extract_size_segment_max: Annotated[int, LIVE] = Field(default=16, ge=1)
    # The same windows past this many characters of message content,
    # whatever the message count; a window closes early once its turns carry this much
    # (an agent transcript with tool results runs ~190K chars a session at the median).
    # 0 disables the character budget (turn budget only).
    memory_extract_size_segment_chars: Annotated[int, LIVE] = 60000
    # Buffer threshold: a session whose un-distilled
    # observations reach this many characters is distilled even without an
    # explicit observe.crystallize, like a short-term buffer that flushes when
    # full. 0 disables; the READY tag still works either way.
    observe_extract_buffer_chars: Annotated[int, LIVE] = 0
    # Observations a session needs before the buffer threshold can fire (at least 1);
    # a per-namespace value wins.
    observe_extract_buffer_min_observations: Annotated[int, LIVE] = Field(default=8, ge=1)
    # Deferred-extraction worker for the observe plane: distills READY rows
    # (observe.record/crystallize) into durable memories through the
    # memory.set_from_messages pipeline. Off by default (makes LLM calls).
    observe_extract_enabled: bool = False
    # Seconds between observe-extraction worker passes.
    observe_extract_interval_seconds: int = 300
    # Most transition events written per distilled session.
    observe_extract_max_events_per_session: Annotated[int, LIVE] = 12
    # Tool names (case-insensitive globs, comma-separated) whose
    # observations are stored content-less and already distilled: loom's own MCP tools
    # (observing one copies a memory outside the key the user can delete) and the
    # harness's meta tools, which say nothing about the task. "" disables the skip.
    observe_skip_tools: Annotated[str, LIVE] = (
        # The plugin-installed server's tools arrive as
        # `mcp__plugin_loom_loom__*`, hence the `mcp__plugin_loom*` glob.
        "mcp__loom__*,mcp__plugin_loom*,loom_memory_*,loom_knowledge_*,loom_search,loom_observe*,"
        "loom_procedure_*,loom_context_*,loom_ask,"
        "ToolSearch,ListAgents,ListMcpResourcesTool,ReadMcpResourceTool,"
        "ReadMcpResourceDirTool,AskUserQuestion,TodoWrite,Skill,EnterPlanMode,"
        "ExitPlanMode,EnterWorktree,ExitWorktree"
    )
    # Deferred set_from_messages replay. On by default: `defer`
    # is opt-in per call, and an empty queue costs one SELECT per interval.
    extraction_queue_enabled: bool = True
    # Agents whose set_from_messages runs deferred without the flag:
    # comma-separated agent names, "*" = every service-account
    # caller, "" = nobody. A call that sets `record_schema` is never deferred by this
    # list (it reads `written` back), and an explicit defer=false still wins.
    extraction_defer_agents: Annotated[str, LIVE] = ""
    # Seconds between deferred-extraction queue passes.
    extraction_queue_interval_seconds: int = 15
    # Most queued batches one pass claims.
    extraction_queue_max_batches_per_pass: Annotated[int, LIVE] = Field(default=8, ge=0)
    # Seconds a claimed batch stays leased to one worker before another may claim it;
    # at least a minute. Set it above the longest pass (max_batches_per_pass times one
    # replay), or an overlapping worker may claim a batch still waiting in the pass.
    extraction_queue_lease_seconds: Annotated[int, LIVE] = Field(default=600, ge=60)
    # Attempts before a failing batch stops returning to `pending` and is marked failed.
    extraction_queue_max_attempts: Annotated[int, LIVE] = Field(default=3, ge=0)
    # Namespaces replayed concurrently per pass; batches inside one namespace
    # stay serial in session-date order. At least 1: none would hang the pass.
    extraction_queue_namespace_concurrency: Annotated[int, LIVE] = Field(default=4, ge=1)
