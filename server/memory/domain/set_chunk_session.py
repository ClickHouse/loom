"""The session a key's `memory.set` chunks are written under (`loom.memory_chunks`). It
derives from the key alone, so a later write for the same key finds them by primary key
and a reader can tell a memory.set chunk from a conversation one."""

from __future__ import annotations

SESSION_PREFIX = "memory.set:"


def set_chunk_session_id(memory_key: str) -> str:
    """The deterministic `session_id` (and `source_url`) of a key's memory.set chunks."""
    return f"{SESSION_PREFIX}{memory_key}"
