"""The `SearchQuery` fields that decide which rows a search may read."""

from __future__ import annotations

from typing import Any

from server.core.naming import SENTINEL
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.served_superseded import served_flags


def without_run_sentinel[M: MemorySearchInput](input: M) -> M:
    """`input` with `run_id` "-" read as no run filter. The SDKs stamp the identity
    sentinel when a call names no run; filtering on it would serve only rows written
    outside every run."""
    if input.run_id == SENTINEL:
        return input.model_copy(update={"run_id": None})
    return input


def read_filters(input: MemorySearchInput) -> dict[str, Any]:
    """Keyword arguments for `SearchQuery`, from a search input whose read
    scope is already pinned to the caller."""
    return {
        "scope": input.scope,
        "agent": input.agent,
        **served_flags(input),
        "memory_type": input.memory_type or None,
        "user_id": input.user_id,
        "session_id": input.session_id,
        "session_ids": list(input.session_ids or []),
        "run_id": input.run_id,
        "tags": list(input.tags or []),
        "tags_soft": bool(input.tags_soft),
        "category": input.category or "",
        "category_soft": bool(input.category_soft),
        "as_of": input.as_of,
        "as_of_basis": input.as_of_basis,
    }
