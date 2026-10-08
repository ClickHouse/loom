"""The procedural type keeps its contract at every door.

A procedure is a situation (the precondition) and at least two steps: the miner
refuses a single step, and `procedure.match` drops rows without `steps`. Single
sentences written through `memory.set` / `memory.bulk_set` as
`memory_type='procedural'` would take the `top_k` slots of every
`procedure.match` and then be dropped, so the plane would match nothing while
looking populated. This module is the one rule, read by every write door and by
the operator backfill.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MIN_STEPS = 2
RETYPE_TO = "semantic"
RETYPE_TAG = "retyped_from:procedural"


def procedure_shape_reason(value: Any) -> str | None:
    """None when `value` is a procedure body; else why it is not."""
    if not isinstance(value, dict):
        return "value is not an object"
    situation = value.get("situation")
    if not isinstance(situation, str) or not situation.strip():
        return "no situation"
    steps = value.get("steps")
    if not isinstance(steps, list):
        return "no steps"
    clean = [s for s in steps if isinstance(s, str) and s.strip()]
    if len(clean) < MIN_STEPS:
        # A body the plane itself wrote carries a graph; evolve may shrink
        # its live steps below two (dead ends dropped) and re-save it, and
        # that is still a procedure.
        graph = value.get("graph")
        nodes = graph.get("nodes") if isinstance(graph, dict) else None
        if isinstance(nodes, list) and len(nodes) >= MIN_STEPS:
            return None
        return f"fewer than {MIN_STEPS} steps"
    return None


@dataclass(frozen=True)
class Retype:
    memory_type: str
    tags: list[str]
    reason: str


def retype_for(memory_type: str, value: Any) -> Retype | None:
    """What a write declaring `memory_type` for `value` becomes. None = keep
    the declared type. A procedural declaration on a non-procedure body
    becomes a searchable semantic row tagged with where it came from — the
    fact is kept, the type is honest, and the procedure plane stays clean."""
    if memory_type != "procedural":
        return None
    reason = procedure_shape_reason(value)
    if reason is None:
        return None
    return Retype(memory_type=RETYPE_TO, tags=[RETYPE_TAG], reason=reason)
