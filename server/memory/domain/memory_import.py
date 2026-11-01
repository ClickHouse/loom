"""What `memory.import` decides with no I/O: what the stored versions of a key say
before the write, and how an entry repeating an earlier one of the request answers.
The key itself is `digest_keys.import_key`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

Outcome = Literal["written", "reinforced", "skipped", "refused"]
# Why an entry was not plainly written; each value is documented on `_ImportEntryResult`.
Reason = Literal[
    "",
    "blacklist",
    "pinned",
    "locked",
    "conflict",
    "merge",
    "identity_duplicate",
    "unchanged",
    "deleted",
    "superseded",
    "rejected",
    "proposed",
    "repeated",
    "unknown",
    "duplicate",
    "conflicted",
]

# The reasons of a skipped entry: its key holds the text already (a re-run), an earlier
# entry of the request with the same text was written, the key's newest version is a
# tombstone (its owner deleted it), superseded (a newer fact retired it), rejected (marked
# wrong) or a proposal on trial (decided only by memory.proposal_decide), or a status the
# import does not recognise.
UNCHANGED: Final[Reason] = "unchanged"
REPEATED: Final[Reason] = "repeated"
DELETED: Final[Reason] = "deleted"
UNKNOWN: Final[Reason] = "unknown"
# Why an entry's category is not linked to its key; '' when it is.
Note = Literal[
    "", "category_unregistered", "category_refused", "category_not_linked", "category_rescinded"
]
# The namespace has not registered the category.
CATEGORY_UNREGISTERED: Final[Note] = "category_unregistered"
# The token is bounded to one user, and a link has no user axis.
CATEGORY_REFUSED: Final[Note] = "category_refused"
# The key does not hold the item (refused, reinforced elsewhere or left as it is).
CATEGORY_NOT_LINKED: Final[Note] = "category_not_linked"
# The owner rescinded the key's link to the category; the import leaves it rescinded.
CATEGORY_RESCINDED: Final[Note] = "category_rescinded"

# A head row's status → the reason the import skips its key. A live row that holds its
# fact maps to '' and goes through the bulk write, which decides what happens to it.
_SKIP_OF_STATUS: Final[dict[str, Reason]] = {
    "active": "",
    "duplicate": "",
    "conflicted": "",
    "superseded": "superseded",
    "rejected": "rejected",
    "proposed": "proposed",
}


def skip_reasons(heads: Iterable[Mapping[str, Any]]) -> dict[str, Reason]:
    """Key → why the import leaves it as it is, from its newest version (rows with
    `memory_key`, `deleted` and `head_status`): `deleted`, the row's status, `unknown`
    for a status it does not recognise, or '' to write it."""
    out: dict[str, Reason] = {}
    for row in heads:
        status = str(row["head_status"] or "")
        reason = DELETED if row["deleted"] else _SKIP_OF_STATUS.get(status, UNKNOWN)
        out[str(row["memory_key"])] = reason
    return out


def repeat_outcome(first: tuple[Outcome, Reason]) -> tuple[Outcome, Reason]:
    """How an entry repeating an earlier entry's text answers: skipped as `repeated`
    when the earlier one was written, else as the earlier one did (a refusal, a
    reinforcement, a key left as it is)."""
    return ("skipped", REPEATED) if first[0] == "written" else first


def holds_item(outcome: Outcome, reason: Reason) -> bool:
    """Whether an entry's key holds its item once the write is done: written now, there
    already, or written by an earlier entry of the request."""
    return outcome == "written" or (outcome == "skipped" and reason in (UNCHANGED, REPEATED))
