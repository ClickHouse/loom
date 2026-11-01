"""What `memory.import` derives from an imported item, with no I/O: the key the item
is stored under, what the stored versions of that key say before the write, and how
an entry repeating an earlier one of the request answers.

An imported item was distilled by another memory system and is stored as it came.
Its key is a digest of its text, so the same item lands on the same key however
often it is imported, and a re-run of an import writes nothing new.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any, Final, Literal

from server.core.hashing import sha256_text

# Every imported item's key starts with this; the rest is a digest of its text.
IMPORT_KEY_PREFIX: Final = "import:"
# The hex characters of the text's SHA-256 a key keeps (96 bits), as the recall
# importer's content-hash keys keep.
IMPORT_KEY_HEX: Final = 24
# A key `import_key` derives, exactly: the prefix and that many lowercase hex characters.
_DIGEST_KEY: Final = re.compile(f"{re.escape(IMPORT_KEY_PREFIX)}[0-9a-f]{{{IMPORT_KEY_HEX}}}")

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
# wrong) or a proposal on trial (decided only by memory.proposal_decide), or a state the
# import does not recognise.
UNCHANGED: Final[Reason] = "unchanged"
REPEATED: Final[Reason] = "repeated"
DELETED: Final[Reason] = "deleted"
UNKNOWN: Final[Reason] = "unknown"
# The notes of an entry naming a category the namespace has not registered, and of one
# naming a category under a token bounded to one user, which links none.
CATEGORY_UNREGISTERED: Final = "category_unregistered"
CATEGORY_REFUSED: Final = "category_refused"

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


def import_key(value: str) -> str:
    """The key an item with text `value` is stored under: `import:` and the first 24
    hex characters of the SHA-256 of the text as UTF-8, taken as given (the recall
    importer hashes content the same way)."""
    return IMPORT_KEY_PREFIX + sha256_text(value)[:IMPORT_KEY_HEX]


def is_digest_key(memory_key: str) -> bool:
    """Whether `memory_key` has exactly the shape `import_key` derives: `import:` and 24
    lowercase hex characters, nothing before or after. Such a key is a digest, not words
    about the item, so the text a row is embedded and lexically indexed by leaves it out
    (`build_excerpt`, `key_axis_text`) whichever write stores the row; a key of any other
    shape keeps its words there."""
    return _DIGEST_KEY.fullmatch(memory_key) is not None


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
