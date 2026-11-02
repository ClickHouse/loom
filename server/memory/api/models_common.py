"""Shared memory-plane primitives used across the split memory.* op modules.

The identity tuple base model + the two module-level constants, in one place
so the per-group modules (memory_crud, memory_search, ...) import them rather
than duplicating.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

from server.core.naming import SENTINEL, named
from server.memory.domain.files import MAX_FILE_CHARS, MAX_FILES

# Open string at the wire so operators can register custom taxonomies via
# tag_registry. Core four (semantic/procedural/preference = searchable+embedded,
# episodic = session-scoped) remain a runtime convention the handlers key off.
MemoryType = str

# The repo files a row is about, as memory.set and decision.record take them.
FileRefs = Annotated[
    list[Annotated[str, Field(max_length=MAX_FILE_CHARS)]], Field(max_length=MAX_FILES)
]


class _Identity(BaseModel):
    """The identity tuple every memory op carries.

    `run_id` is the task-scope axis (one agent execution). Defaults to
    `'-'` so single-shot write paths don't need to set it.
    """

    org: str
    namespace: str
    agent: str
    user_id: str = Field(default=SENTINEL)
    session_id: str = Field(default=SENTINEL)
    run_id: str = Field(default=SENTINEL)


def run_next_hint(run_id: str | None) -> str:
    """The `next` nudge attached to recall responses made under a run_id.

    Empty when there is no run (the common, un-instrumented call pays no
    tokens). Names the exact op and field so the agent can act without
    reading a description: the `used` signal only exists if it is reported.
    """
    rid = named((run_id or "").strip())
    if rid is None:
        return ""
    return (
        f"run_id={rid}: when you rely on a result, keep its memory_key and pass "
        "it in run.complete(memories_used=[...]) so loom learns which memories "
        "were used, not only returned."
    )


class MemorySetPeer(BaseModel):
    """A row the conflict check compared the write against.

    Enough to act on without a second read: the key is the handle for
    `memory.update`, the excerpt says what the store holds, `valid_at` says
    when it held, `similarity` is the cosine the verdict was reached on.
    """

    memory_id: str = ""
    memory_key: str = ""
    excerpt: str = ""
    valid_at: datetime | None = None
    similarity: float = 0.0


# Chars of a peer's value that ride the write response. Long enough to tell
# "Tuesday" from "Thursday", short enough that five peers stay under a
# kilobyte.
SET_PEER_EXCERPT_CHARS = 200
SET_PEERS_MAX = 5


def set_peer_excerpt(value: str, limit: int = SET_PEER_EXCERPT_CHARS) -> str:
    """The stored value as one line, clipped. A JSON string loses its quotes
    so the agent reads the fact, not its encoding."""
    text = (value or "").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def set_next_hint(
    verdict: str,
    peers: list[MemorySetPeer],
    *,
    repeated_merge: bool = False,
    relations_unlinked: bool = False,
) -> str:
    """The `next` nudge on a write response: the same shape as
    `run_next_hint` — the exact op and field the agent should reach for, or
    nothing. Empty on the common clear write so it costs no tokens.

    `conflict` names the peer the store now disagrees with and the update verb
    keyed by that peer's key; a third copy of the fact would not settle it.
    A `merge` the store has seen before (the row was already reinforced) names
    `memory.pin`: the fact keeps arriving, so the agent may want it to outrank
    its neighbours. A merge into another writer's row says the write's `relations`
    were not linked (`relations_unlinked`). A first merge, a duplicate, a clear write:
    empty.
    """
    if verdict == "conflict" and peers:
        p = peers[0]
        return (
            f"verdict=conflict: this write disagrees with {p.memory_key!r} "
            f"(similarity {p.similarity:.2f}). If the fact changed, revise that row: "
            f"memory.update(memory_key={p.memory_key!r}, value=...). "
            "Writing a third copy will not settle it."
        )
    if verdict == "rejected" and peers:
        p = peers[0]
        return (
            f"verdict=rejected: {p.memory_key!r} is pinned and close to this value. "
            f"Revise it explicitly with memory.update(memory_key={p.memory_key!r}, value=...) "
            "or unpin it first; a plain memory.set will answer 409."
        )
    if verdict != "merge":
        return ""
    hints = []
    if relations_unlinked:
        hints.append("relations not linked: this merged into a row that is not yours.")
    if repeated_merge and peers:
        p = peers[0]
        hints.append(
            f"loom already holds this fact as {p.memory_key!r} and has now "
            "heard it more than once; nothing was written. If it should outrank its "
            f"neighbours, memory.pin(memory_key={p.memory_key!r}, value=...) makes it a "
            "write-time invariant."
        )
    return "verdict=merge: " + " ".join(hints) if hints else ""
