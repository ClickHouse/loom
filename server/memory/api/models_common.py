"""Shared memory-plane primitives used across the split memory.* op modules.

The identity tuple base model + the two module-level constants, in one place
so the per-group modules (memory_crud, memory_search, ...) import them rather
than duplicating.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from server.core.naming import SENTINEL
from server.operations.base import MCP_HIDDEN

# Open string at the wire so operators can register custom taxonomies via
# tag_registry. Core four (semantic/procedural/preference = searchable+embedded,
# episodic = session-scoped) remain a runtime convention the handlers key off.
MemoryType = str


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
    rid = (run_id or "").strip()
    if not rid or rid == SENTINEL:
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
) -> str:
    """The `next` nudge on a write response: the same shape as
    `run_next_hint` — the exact op and field the agent should reach for, or
    nothing. Empty on the common clear write so it costs no tokens.

    `conflict` names the peer the store now disagrees with and the update verb
    keyed by that peer's key; a third copy of the fact would not settle it.
    A `merge` the store has seen before (the row was already reinforced) names
    `memory.pin`: the fact keeps arriving, so the agent may want it to outrank
    its neighbours. A first merge, a duplicate, a clear write: empty.
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
    if verdict == "pending":
        # The row is in; the judge runs after this call.
        return (
            "verdict=pending: written; the conflict judge settles after this call. "
            "memory.get(memory_key=<this key>) shows the settled status "
            "(active | conflicted | duplicate); await_verdict=true waits inline."
        )
    if verdict == "merge" and repeated_merge and peers:
        p = peers[0]
        return (
            f"verdict=merge: loom already holds this fact as {p.memory_key!r} and has now "
            "heard it more than once; nothing was written. If it should outrank its "
            f"neighbours, memory.pin(memory_key={p.memory_key!r}, value=...) makes it a "
            "write-time invariant."
        )
    return ""


class TemporalArgs(BaseModel):
    """The bi-temporal read contract, shared by the memory reads.

    No argument: the current state as currently recorded. `valid_as_of=V`: the
    latest recorded interpretation of the world at V. `known_as_of=K`: what loom
    had recorded at K about the world at K. Both: what loom had recorded at K
    about the world at V. Current permissions, blacklist and erasure apply at
    every K: a historical read never authorizes an action today."""

    # World time V; None = now, or K when only `known_as_of` is given.
    valid_as_of: datetime | None = None
    # Knowledge time K: answer from the journal as committed at K; None = now. A
    # future K is a 422.
    known_as_of: datetime | None = None
    # A write's `read_token`: the read waits (within budget) until the journal
    # cutoff covers that write, and answers `projection_not_ready` otherwise.
    # Not in the MCP listing.
    read_token: str = Field(default="", json_schema_extra=MCP_HIDDEN)
    # `compat` admits an assertion with an unknown or inferred validity bound and
    # discloses it; `strict` excludes it. Not in the MCP listing.
    validity_policy: Literal["compat", "strict"] = Field(
        default="compat", json_schema_extra=MCP_HIDDEN
    )

    def temporal_requested(self) -> bool:
        return (
            self.valid_as_of is not None
            or self.known_as_of is not None
            or bool(self.read_token)
            or self.validity_policy != "compat"
        )


class TemporalCutoff(BaseModel):
    """What a temporal read answered from. Namespace sequences are not a global
    snapshot: a multi-namespace read lists one cutoff per namespace."""

    namespace: str = ""
    valid_as_of: datetime | None = None
    known_as_of: datetime | None = None
    # Highest journal sequence the answer includes; 0 = none.
    cutoff_seq: int = 0
    # The namespace's newest committed sequence when the read ran.
    head_seq: int = 0
    # True when nothing the read needed was missing: no projection lag left
    # unreduced, no integrity break, no pending vector.
    complete: bool = True
    # Recording time of the namespace's first commit.
    history_from: datetime | None = None
    validity_policy: str = "compat"
    # Named gaps: projection_not_ready, integrity_break_at:<seq>, ranking_current,
    # vectors_pending:<n>, truncated:<n>, and for a plane read from a
    # history mirror (shared memory) the mirror and its limits.
    gaps: list[str] = Field(default_factory=list)


class TemporalAssertion(BaseModel):
    """One recorded assertion about a memory, with both of its times."""

    assertion_id: str
    origin_assertion_id: str = ""
    parent_assertion_id: str | None = None
    memory_key: str = ""
    agent: str = ""
    user_id: str = ""
    session_id: str = ""
    value: Any = None
    # sha256 of the stored value text: what a receipt cites.
    value_sha256: str = ""
    memory_type: str = ""
    # state | event | snapshot | rule
    temporal_kind: str = "state"
    # stated | extracted | inferred | recorded | unknown
    validity_basis: str = "unknown"
    # The valid-time span [valid_from, valid_to); None = open (or unknown, per basis).
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    valid_text: str = ""
    valid_precision: str = ""
    valid_tz: str = ""
    # asserted | observed | planned | hypothetical | quoted | disputed
    modality: str = "asserted"
    status: str = "active"
    confidence: float = 1.0
    verified_by: str = ""
    verified_at: datetime | None = None
    source_class: str = ""
    actor: str = ""
    qualifiers: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    # The commits that added and (if it is no longer served) retired it.
    born_seq: int = 0
    born_at: datetime | None = None
    retired_seq: int | None = None
    retired_at: datetime | None = None
