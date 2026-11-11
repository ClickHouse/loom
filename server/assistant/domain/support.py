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
    ran (found or not), whether any call failed or was refused, and whether the run held
    more rows than one read takes (`cut`: what was read is a part of it)."""

    returned: frozenset[str]
    failed: bool
    ran: frozenset[str]
    cut: bool = False


# The rows one read of a run takes; a read asks for one more, so a run past it is known cut.
RUN_ROWS = 200  # not a setting: a bound on one read of a run, not a behaviour to tune


@dataclass(frozen=True)
class CallRow:
    """One ledger row of an answer's tool call (`ledger.run_rows`, `ledger.live_rows`): its
    op, whether it succeeded, and what its output said it found (`out_found`, `out_hits`,
    "" when the row has none)."""

    operation: str
    success: bool
    out_found: str = ""
    out_hits: str = ""

    @property
    def found(self) -> bool:
        """The call found data unless its row says it found nothing (a miss, an empty list,
        an abstention)."""
        return self.success and self.out_found != "false" and self.out_hits != "0"


def kept_rows(rows: list[CallRow]) -> tuple[list[CallRow], bool]:
    """The rows a run is judged on, and whether it was cut: a read takes RUN_ROWS + 1, and
    more than RUN_ROWS is a cut run, judged on its first RUN_ROWS (its label and its steps
    alike)."""
    return rows[:RUN_ROWS], len(rows) > RUN_ROWS


def derive(rows: list[CallRow]) -> RunCalls:
    """The run's calls from its rows (kept_rows)."""
    kept, cut = kept_rows(rows)
    return RunCalls(
        frozenset(r.operation for r in kept if r.found),
        any(not r.success for r in kept),
        frozenset(r.operation for r in kept if r.success),
        cut=cut,
    )


def support_of(
    codes: list[str],
    *,
    text: str,
    calls: RunCalls | None,
    citable: frozenset[str],
    no_data: frozenset[str],
    unknown_names: list[str] | None = None,
) -> str:
    """The answer's support. `codes` are its citations' codes; `calls` what its tool calls
    left (None when they could not be read; on the non-streamed path never read for a
    cited, empty or abstaining answer, or one naming an unknown name); `citable` the ops
    whose rows an answer can cite; `no_data` the ops that return no namespace data;
    `unknown_names` the op and setting names it writes that the build neither serves nor
    documents.

    - none: no text; or it cites nothing and is an abstention, or its calls were read and
      none was citable, found data or failed;
    - supported: a citation verified;
    - unsupported: it names an op or setting the build neither serves nor documents (one
      it may have invented), whatever it cites; a citation failed for a reason of its
      own; or it cites nothing although a citable op's call found data, or ran and found
      nothing while no other call found data or failed (a claim nothing backs);
    - unchecked: otherwise (its checks could not say, what it read has no citation form, a
      call failed, or its run was cut, unless a citable op's call found data).
    """
    if not text.strip():
        return NONE
    if unknown_names:
        return UNSUPPORTED
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
    # A cut run is a part of the run: only a decided outcome holds.
    if found or calls.failed or calls.cut:
        return UNCHECKED
    if (calls.ran - no_data) & citable:
        return UNSUPPORTED
    return NONE
