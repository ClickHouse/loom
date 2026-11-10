"""How far an assistant answer rests on what Loom holds: its `support`, from the codes its
citation checks ended with and what its own tool calls found. Pure: the reads are in the
app layer (server/assistant/app/support.py). The rule and its accepted residuals are in
docs/architecture/assistant.md#support."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Why one citation ended as it did (never on the wire; the wire carries the display reason).
VERIFIED = "verified"
# The check could not say: the ledger or document read failed or ran past the bound, the
# answer had no run id, or it was past the citations one answer checks.
NOT_RUN = "not_run"
PAST_CAP = "past_cap"
# This deployment does not record what memory reads return (the exposure ledger is off).
NOT_RECORDED = "not_recorded"
# Code citations are not checked.
CODE = "code"
# The citation itself failed: its key was not returned to this answer, its document or
# section is not there, its statement did not run in this answer.
UNREAD = "unread"
NO_DOCUMENT = "no_document"
NO_SECTION = "no_section"
SQL_NOT_RUN = "sql_not_run"

CITATION_FAULTS = frozenset({UNREAD, NO_DOCUMENT, NO_SECTION, SQL_NOT_RUN})

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNCHECKED = "unchecked"
NONE = "none"

# The sentence the agent ends an abstention with (integrations/librechat/agent.yaml).
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
    """What an answer's tool calls left: the ops whose calls found data, the ops whose calls
    ran (found or not), and whether any call failed or was refused."""

    returned: frozenset[str]
    failed: bool
    ran: frozenset[str]


def support_of(
    codes: list[str],
    *,
    text: str,
    calls: RunCalls | None,
    citable: frozenset[str],
    no_data: frozenset[str],
) -> str:
    """The answer's support. `codes` are its citations' codes; `calls` what its tool calls
    left (None when they could not be read; never read for a cited, empty or abstaining
    answer); `citable` the ops whose rows an answer can cite; `no_data` the ops that return
    no namespace data.

    - none: no text; or it cites nothing and is an abstention, or its calls were read and
      none was citable, found data or failed;
    - supported: a citation verified;
    - unsupported: a citation failed for a reason of its own, or it cites nothing although
      a citable op's call found data, or ran and found nothing while no other call found
      data or failed (a claim nothing backs);
    - unchecked: otherwise (its checks could not say, what it read has no citation form, or
      a call failed).
    """
    if not text.strip():
        return NONE
    if codes:
        if VERIFIED in codes:
            return SUPPORTED
        if CITATION_FAULTS & set(codes):
            return UNSUPPORTED
        return UNCHECKED
    if is_abstention(text):
        return NONE
    if calls is None:
        return UNCHECKED
    found = calls.returned - no_data
    if found & citable:
        return UNSUPPORTED
    if found or calls.failed:
        return UNCHECKED
    if (calls.ran - no_data) & citable:
        return UNSUPPORTED
    return NONE
