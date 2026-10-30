"""What `memory.import` derives from an imported item, with no I/O: the key the item
is stored under, what the stored versions of that key say before the write, and how
an entry repeating an earlier one of the request answers.

An imported item was distilled by another memory system and is stored as it came.
Its key is a digest of its text, so the same item lands on the same key however
often it is imported, and a re-run of an import writes nothing new.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from server.core.hashing import sha256_text

# Every imported item's key starts with this; the rest is a digest of its text.
IMPORT_KEY_PREFIX: Final = "import:"
# The hex characters of the text's SHA-256 a key keeps (96 bits), as the recall
# importer's content-hash keys keep.
IMPORT_KEY_HEX: Final = 24

Outcome = Literal["written", "reinforced", "skipped", "refused"]

# The reasons of a skipped entry: its key holds the text already (a re-run), an earlier
# entry of the request with the same text was written, the key's newest version is a
# tombstone (its owner deleted it), superseded (a newer fact retired it), rejected (marked
# wrong) or a proposal on trial (decided only by memory.proposal_decide), or a state the
# import does not recognise.
UNCHANGED: Final = "unchanged"
REPEATED: Final = "repeated"
DELETED: Final = "deleted"
SUPERSEDED: Final = "superseded"
REJECTED: Final = "rejected"
PROPOSED: Final = "proposed"
UNKNOWN: Final = "unknown"
# The notes of an entry naming a category the namespace has not registered, and of one
# naming a category under a token bounded to one user, which links none.
CATEGORY_UNREGISTERED: Final = "category_unregistered"
CATEGORY_REFUSED: Final = "category_refused"

# The statuses of a live row that holds its fact: a key whose newest version has one is
# written through the bulk write, which decides what happens to it.
_HOLDING: Final = frozenset({"active", "duplicate", "conflicted"})
# The statuses that leave a key as it is, each the reason the entry is skipped.
_LEFT_AS_IS: Final = frozenset({SUPERSEDED, REJECTED, PROPOSED})


def import_key(value: str) -> str:
    """The key an item with text `value` is stored under: `import:` and the first 24
    hex characters of the SHA-256 of the text as UTF-8, taken as given (the recall
    importer hashes content the same way)."""
    return IMPORT_KEY_PREFIX + sha256_text(value)[:IMPORT_KEY_HEX]


@dataclass(frozen=True)
class KeyState:
    """What the stored versions of one key say before an import writes it: `skip_reason`
    is why the import leaves the key as it is, from its newest version across every
    session the caller's token may read (a tombstone, a superseded, rejected or proposed
    row, or a status the import does not recognise), or '' to write it."""

    skip_reason: str = ""


def key_states(heads: Iterable[Mapping[str, Any]]) -> dict[str, KeyState]:
    """Each key's state, from the newest version of the key in each session (rows with
    `memory_key`, `head_version`, `deleted` and `head_status`)."""
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for row in heads:
        by_key.setdefault(str(row["memory_key"]), []).append(row)
    out: dict[str, KeyState] = {}
    for key, rows in by_key.items():
        newest = max(rows, key=lambda r: int(r["head_version"]))
        status = str(newest["head_status"] or "")
        if newest["deleted"]:
            reason = DELETED
        elif status in _LEFT_AS_IS:
            reason = status
        elif status in _HOLDING:
            reason = ""
        else:
            reason = UNKNOWN
        out[key] = KeyState(skip_reason=reason)
    return out


def repeat_outcome(first: tuple[Outcome, str]) -> tuple[Outcome, str]:
    """How an entry repeating an earlier entry's text answers: skipped as `repeated`
    when the earlier one was written, else as the earlier one did (a refusal, a
    reinforcement, a key left as it is)."""
    return ("skipped", REPEATED) if first[0] == "written" else first


def holds_item(outcome: Outcome, reason: str) -> bool:
    """Whether an entry's key holds its item once the write is done: written now, there
    already, or written by an earlier entry of the request."""
    return outcome == "written" or (outcome == "skipped" and reason in (UNCHANGED, REPEATED))
