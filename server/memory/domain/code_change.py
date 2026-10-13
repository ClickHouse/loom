"""What a namespace's code-linked memories go through when their code changes.

A memory about code describes something Loom indexes, so once that code is gone,
serving it as current is wrong. Loom never writes the memory; the namespace owner picks
what reads do:

  retire_removed (default)  search leaves out a memory whose linked code is all gone;
                            `memory.get` still shows it, and it is back the moment
                            the code is; changes are flagged
  flag                      links are marked, every read still serves the memory
  revise                    as retire_removed, and a changed claim is re-checked
                            against the new code by the LLM: one that still holds
                            is re-anchored, a contradicted one gets a suggested
                            correction on its link

A memory a person pinned, locked or verified is only flagged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal, get_args

from server.memory.domain.trust import verification_state

# a link's states from best to worst: a memory shows its worst link's
STATE_RANK = {"fresh": 0, "moved": 1, "changed": 2, "unresolved": 3, "missing": 4}

CodeChangePolicy = Literal["retire_removed", "flag", "revise"]
POLICIES: tuple[CodeChangePolicy, ...] = get_args(CodeChangePolicy)
RETIRE_REMOVED, FLAG, REVISE = POLICIES


def policy_of(raw: str) -> CodeChangePolicy:
    return next((p for p in POLICIES if p == raw), RETIRE_REMOVED)


def code_removed(states: Sequence[str]) -> bool:
    """Every code link of a memory is missing: the code it is about is gone."""
    return bool(states) and all(s == "missing" for s in states)


def held(row: Mapping[str, Any]) -> bool:
    """A person's hold on the memory: pinned, locked or verified by a human."""
    verified = verification_state(
        row.get("verified_by"), row.get("verified_sha256"), row.get("sha")
    )
    return bool(row.get("pinned") or row.get("lock_mode") or verified == "human")


def hidden(states: Sequence[str], complete: bool, policy: str, is_held: bool) -> bool:
    """Search leaves the memory out: all its code is gone, no link was cut by the caps,
    and neither a `flag` policy nor a person's hold keeps it."""
    return policy != FLAG and not is_held and complete and code_removed(states)
