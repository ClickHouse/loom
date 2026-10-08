"""`memory.brief` — a task-oriented briefing over memory.

An agent orienting on a task wants a bounded, task-scoped context: what is
known, the evidence anchors, AND the explicit unknowns plus what to read next,
so it can still verify the source. `memory.ask` answers a question or abstains;
it never says "here is what I could NOT find, and here is where to look."

`memory.brief` composes `memory.ask` and reshapes its result into a briefing:
the answer, the evidence rows it rests on (each with its provenance tier, so an
inferred/unvouched anchor is visible), the unknowns (the abstain reason when
the ask abstained), and concrete next reads (the sessions the answer cited, and
searches for the unknowns). It is a read: it never writes, and it
does not credit reinforcement (an orientation read is not the agent relying on
a memory).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from server.memory.api.models_common import _Identity


class BriefEvidence(BaseModel):
    """One anchor the brief rests on. `source_class` carries the provenance
    tier so an inferred (`derived`) or unvouched (`external`)
    anchor is visible in the briefing, not laundered into a flat fact."""

    memory_key: str
    source: str = "fact"  # "fact" | "chunk"
    source_class: str = ""  # asserted | extracted | observed | derived | ...
    verified: bool = False
    when: str = ""  # temporal_anchor, else valid_at; "" when undated
    session_id: str = ""
    excerpt: str = ""


class MemoryBriefInput(_Identity):
    """A task or question to orient on. Same identity as every memory op."""

    # The task or question to orient on, passed to `memory.ask`.
    query: str = Field(min_length=1, max_length=2000)
    # How many rows `memory.ask` retrieves; also caps `evidence`.
    top_k: int = Field(default=15, ge=1, le=100)
    # Tag filter passed to `memory.ask`; empty = no tag filter.
    tags: list[str] = Field(default_factory=list, max_length=64)


class MemoryBriefOutput(BaseModel):
    # Echo of the request's query.
    query: str = ""
    # The answer `memory.ask` synthesised from the memories; '' when none.
    answer: str = ""
    # True when `memory.ask` abstained: the memories did not answer it.
    abstained: bool = False
    # "memory" = every sentence from the memories; "mixed" = an inferred /
    # bridged sentence was marked; "" = no synthesis.
    grounding: str = ""
    # The rows the answer cited (every retrieved row when it cited none), at most
    # `top_k`.
    evidence: list[BriefEvidence] = Field(default_factory=list)
    # What the memories did NOT cover: the abstain reason when the ask
    # abstained.
    unknowns: list[str] = Field(default_factory=list)
    # Concrete next steps: the sessions the answer cited (open them), and
    # searches for the unknowns.
    next_reads: list[str] = Field(default_factory=list)
