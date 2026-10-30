"""What `memory.import` derives from an imported item, with no I/O: the key the item
is stored under and the outcome its bulk-write result means.

An imported item was distilled by another memory system and is stored as it came.
Its key is a digest of its text, so the same item lands on the same key however
often it is imported, and a re-run of an import writes nothing new.
"""

from __future__ import annotations

from typing import Final, Literal

from server.core.hashing import sha256_text

# Every imported item's key starts with this; the rest is a digest of its text.
IMPORT_KEY_PREFIX: Final = "import:"
# The hex characters of the text's SHA-256 a key keeps (96 bits), as the recall
# importer's content-hash keys keep.
IMPORT_KEY_HEX: Final = 24

Outcome = Literal["written", "reinforced", "skipped", "refused"]

# The reason of a skipped entry whose key already holds its text: a re-run.
UNCHANGED: Final = "unchanged"
# The outcome of an entry whose text an earlier entry of the same request carries: it
# shares that entry's key, and only the earlier entry is written.
REPEATED: Final[tuple[Outcome, str]] = ("skipped", "repeated")
# The note of an entry naming a category the namespace has not registered.
CATEGORY_UNREGISTERED: Final = "category_unregistered"

# A bulk-write verdict that refuses an entry, by the check that refused it: the
# blacklist, a pinned or a read_only-locked row under the key, or the conflict check
# finding the text too close to a pinned memory.
_REFUSED_BY: Final[dict[str, str]] = {
    "blacklisted": "blacklist",
    "pinned": "pinned",
    "locked": "locked",
    "rejected": "conflict",
}
# The status a written entry's verdict stores its row with when it is not `active`.
_STORED_AS: Final[dict[str, str]] = {"duplicate": "duplicate", "conflict": "conflicted"}


def import_key(value: str) -> str:
    """The key an item with text `value` is stored under: `import:` and the first 24
    hex characters of the SHA-256 of the text as UTF-8, taken as given (the recall
    importer hashes content the same way)."""
    return IMPORT_KEY_PREFIX + sha256_text(value)[:IMPORT_KEY_HEX]


def entry_outcome(
    *, key: str, version: int, verdict: str, held_by: str | None
) -> tuple[Outcome, str]:
    """The outcome and reason of one entry from its bulk-write result: `version` (0
    when nothing was inserted), `verdict` (its `conflict_verdict`) and `held_by` (the
    key of the live row identity dedup found holding the same text, if any).

    A written entry's reason is the status its row was stored with when that is not
    `active`. A merge, or the same text under another key, reinforced the memory that
    holds the fact. An entry whose own key already holds its text, in this session or
    another of the identity, is skipped."""
    if version:
        return "written", _STORED_AS.get(verdict, "")
    if verdict in _REFUSED_BY:
        return "refused", _REFUSED_BY[verdict]
    if verdict == "merge":
        return "reinforced", "merge"
    if held_by is not None and held_by != key:
        return "reinforced", "identity_duplicate"
    return "skipped", UNCHANGED


def holds_item(outcome: Outcome, reason: str) -> bool:
    """Whether an entry's key holds its item once the write is done: written now, or
    there already."""
    return outcome == "written" or reason == UNCHANGED
