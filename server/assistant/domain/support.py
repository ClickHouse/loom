"""How far an assistant answer rests on what Loom holds: its `support`, from the codes its
citation checks ended with and what its own tool calls found. Pure: native evidence receipts are
verified in the app layer. The rule and its accepted residuals are in
docs/architecture/assistant.md#support."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Native receipt checks record whether this answer read the cited source.
VERIFIED = "verified"
UNREAD = "unread"

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNCHECKED = "unchecked"
NONE = "none"

# The sentence the agent ends an abstention with (server/assistant/domain/instructions.py).
ABSTENTION = "nothing stored in loom answers this"
_EMPHASIS = re.compile(r"[*_`~]+")
_SPACE = re.compile(r"\s+")


def is_abstention(text: str) -> bool:
    """Whether `text` holds the abstention sentence, in any case, with or without its
    period or Markdown emphasis."""
    plain = _SPACE.sub(" ", _EMPHASIS.sub("", text)).casefold()
    return ABSTENTION in plain


@dataclass(frozen=True)
class RunCalls:
    """The successful operations that returned data and those that ran, including misses."""

    returned: frozenset[str]
    ran: frozenset[str]


@dataclass(frozen=True)
class CallRow:
    """A successful tool receipt and its projected found/hit signals, empty when absent."""

    operation: str
    out_found: str = ""
    out_hits: str = ""

    @property
    def found(self) -> bool:
        """The call found data unless its row says it found nothing (a miss, an empty list,
        an abstention)."""
        return self.out_found != "false" and self.out_hits != "0"


def derive(rows: list[CallRow]) -> RunCalls:
    """The outcomes of the answer's request-owned successful receipts."""
    return RunCalls(
        frozenset(r.operation for r in rows if r.found),
        frozenset(r.operation for r in rows),
    )


def support_of(
    codes: list[str],
    *,
    text: str,
    calls: RunCalls,
    citable: frozenset[str],
    no_data: frozenset[str],
    unknown_names: list[str] | None = None,
) -> str:
    """The answer's support. `codes` are its citations' receipt verdicts; `calls` are
    outcomes derived from this answer's successful tool receipts;
    `citable` the ops
    whose rows an answer can cite; `no_data` the ops that return no namespace data;
    `unknown_names` the op and setting names it writes that the build neither serves nor
    documents.

    - none: no text; or it cites nothing and is an abstention, or its calls were read and
      none was citable or found data;
    - supported: a citation verified;
    - unsupported: it names an op or setting the build neither serves nor documents (one
      it may have invented), whatever it cites; a citation failed for a reason of its
      own; or it cites nothing although a citable op's call found data, or ran and found
      nothing while no other call found data (a claim nothing backs);
    - unchecked: what it read has no citation form.
    """
    if not text.strip():
        return NONE
    if unknown_names:
        return UNSUPPORTED
    if codes:
        return SUPPORTED if VERIFIED in codes else UNSUPPORTED
    if is_abstention(text):
        return NONE
    found = calls.returned - no_data
    if found & citable:
        return UNSUPPORTED
    if found:
        return UNCHECKED
    if (calls.ran - no_data) & citable:
        return UNSUPPORTED
    return NONE
