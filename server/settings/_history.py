"""Every change to a setting's default, with the release that ships it and why.

A default is behaviour: every deployment that never set the variable changes with
it. `tests/unit/test_settings_defaults.py` replays this list over the pinned
defaults in `tests/unit/snapshots/settings_defaults.json` and fails on a default
that changed without an entry here. The configuration reference lists them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DefaultChange:
    # The Loom release that ships the new default: the next release when the change
    # lands on main, since pyproject.toml still names the release already out.
    version: str
    name: str
    old: Any
    new: Any
    why: str


# Oldest first.
DEFAULT_CHANGES: tuple[DefaultChange, ...] = (
    DefaultChange(
        version="1.0.0",
        name="extraction_queue_max_attempts",
        old=3,
        new=8,
        why=(
            "A failed replay now waits 15 s, 1, 5, then 30 minutes before the next attempt, "
            "and an unreachable provider spends none: eight attempts span about two hours "
            "instead of three back to back."
        ),
    ),
    DefaultChange(
        version="1.2.0",
        name="clickstack_user_id_keys",
        old="user_id,user.id,enduser.id",
        new="user_id,user.id,enduser.id,loom.user_id,langfuse.user.id",
        why=(
            "A user-scoped ClickStack erasure also matches the user-id keys Loom's own "
            "child spans carry, so they are erased with the request span. A deployment "
            "that sets the variable keeps its own list and should add both keys."
        ),
    ),
    DefaultChange(
        version="1.2.0",
        name="knowledge_ingest_interval_seconds",
        old=2,
        new=0,
        why=(
            "The ingest worker takes the next job as soon as one ends instead of waiting "
            "2 s after every job, so a queue of small documents drains at the speed of its "
            "jobs; a pass that finds no job still waits "
            "`LOOM_KNOWLEDGE_INGEST_IDLE_INTERVAL_SECONDS`."
        ),
    ),
    DefaultChange(
        version="1.3.1",
        name="mcp_always_load_tools",
        old="code.search,code.symbol",
        new="search.query,code.search,code.symbol,loom_op",
        why=(
            "Expose search and operation discovery without a preliminary tool lookup; "
            "other schemas stay under client discovery. An explicit list selects entrypoints, "
            "and an empty value leaves loading to the host."
        ),
    ),
    DefaultChange(
        version="1.3.1",
        name="code_ask_token_budget",
        old=4000,
        new=0,
        why=(
            "Return selected retrieval context without an implicit token cap; "
            "positive values opt in to a cap."
        ),
    ),
    DefaultChange(
        version="1.3.1",
        name="code_result_lessons_tokens",
        old=250,
        new=0,
        why=(
            "Return selected retrieval context without an implicit token cap; "
            "positive values opt in to a cap."
        ),
    ),
    DefaultChange(
        version="1.3.1",
        name="memory_ask_code_symbol_token_budget",
        old=400,
        new=0,
        why=(
            "Return selected retrieval context without an implicit token cap; "
            "positive values opt in to a cap."
        ),
    ),
    DefaultChange(
        version="1.3.1",
        name="memory_ask_code_search_token_budget",
        old=1400,
        new=0,
        why=(
            "Return selected retrieval context without an implicit token cap; "
            "positive values opt in to a cap."
        ),
    ),
    DefaultChange(
        version="1.3.1",
        name="code_review_brief_rationale_tokens",
        old=250,
        new=0,
        why="Return selected review rationale without an implicit token cap.",
    ),
)
