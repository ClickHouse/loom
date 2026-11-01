"""memory.import — memory items another memory system already distilled, written into
the caller's namespace through the bulk write path."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from server.memory.api.models_common import _Identity
from server.memory.domain.delete_marks import caller_trace
from server.memory.domain.memory_import import Note, Outcome, Reason

# The most entries one call takes; the bulk write conflict-checks every one of them,
# whatever LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES caps a memory.bulk_set at.
MAX_IMPORT_ENTRIES = 200  # not a setting: a wire bound the request schema publishes
# The longest item text, in characters, a bound on what one row stores. Only the first
# 1,200 characters of its JSON encoding (`excerpt.EXCERPT_LIMIT`) are embedded and indexed.
MAX_IMPORT_VALUE_CHARS = 30_000  # not a setting: a wire bound
# The most tags one item carries, and the longest tag.
MAX_IMPORT_TAGS = 64  # not a setting: a wire bound
MAX_IMPORT_TAG_CHARS = 256  # not a setting: a wire bound
# The longest source pointer and category name an item names.
MAX_IMPORT_TRACE_CHARS = 512  # not a setting: a wire bound
MAX_IMPORT_CATEGORY_CHARS = 256  # not a setting: a wire bound


class _MemoryImportEntry(BaseModel):
    # The item's text, stored verbatim; at least one character that is not whitespace.
    # Its key is `import:` and the first 24 hex characters of the text's SHA-256, so the
    # same text always lands on the same key.
    value: str = Field(..., max_length=MAX_IMPORT_VALUE_CHARS, pattern=r"\S")
    # Labels stored with the item, filterable at recall.
    tags: list[Annotated[str, Field(max_length=MAX_IMPORT_TAG_CHARS)]] = Field(
        default_factory=list, max_length=MAX_IMPORT_TAGS
    )
    # When the source system first knew the item, stored as `valid_at`; the row's
    # `created_at` is the time of the import. None = unknown.
    valid_at: datetime | None = None
    # The item's id or path in the source system; '' stores its key instead.
    source_trace: str = Field(default="", max_length=MAX_IMPORT_TRACE_CHARS)
    # A category the namespace registered to link the item's key to; '' = none (the
    # result's `note` says when it is not linked).
    category: str = Field(default="", max_length=MAX_IMPORT_CATEGORY_CHARS)
    # Sensitivity tier; '' = detect from the value, as memory.bulk_set does.
    sensitivity: Literal["", "low", "medium", "high"] = ""

    @field_validator("source_trace")
    @classmethod
    def _source_trace(cls, v: str) -> str:
        return caller_trace(v)


class MemoryImportInput(_Identity):
    """Import items another memory system distilled, through every check a write makes.

    The checks are dedupe, conflict, blacklist, pin and lock, and the items land in the
    caller's namespace. Each item is stored under a key derived
    from its text (`import:` and 24 hex characters of its SHA-256), as
    `source_class=imported`, so a re-run of the same import writes nothing new. The
    entries share the caller's identity tuple. A dry run makes every check and answers
    the outcome each entry would have.
    """

    # The items to import, 1 to 200: each one's text, and optionally its tags, the time
    # the source first knew it, its source pointer, a category and a sensitivity tier.
    entries: list[_MemoryImportEntry] = Field(..., min_length=1, max_length=MAX_IMPORT_ENTRIES)
    # Make every check and answer each entry's outcome, writing nothing.
    dry_run: bool = False

    @model_validator(mode="after")
    def _repeats_match(self) -> MemoryImportInput:
        # An entry repeating an earlier entry's text is not written again, so it may not
        # carry other labels that would be dropped.
        first: dict[str, int] = {}
        for i, entry in enumerate(self.entries):
            j = first.setdefault(entry.value, i)
            if self.entries[j] != entry:
                raise ValueError(f"entries {j} and {i} have the same value but different fields")
        return self


class _MemoryImportResult(BaseModel):
    # The entry's position in the request's `entries`.
    index: int
    # The key the item is stored under, or would be.
    key: str
    # written | held (another memory holds the fact; nothing is written) | skipped
    # (the key is left as it is; `reason` says why) | refused (a check refused it).
    outcome: Outcome
    # Why the entry was not plainly written; the values are listed in
    # docs/features/memory/writing.md#importing-from-another-memory-system.
    reason: Reason = ""
    # The key holding the text, for an identity_duplicate entry; '' otherwise.
    held_by: str = ""
    # Why the entry's category is not linked to its key, listed in the same section;
    # '' when it is linked.
    note: Note = ""


class _MemoryImportTotals(BaseModel):
    # Entries whose key got a new version.
    written: int = 0
    # Entries another memory already held.
    held: int = 0
    # Entries whose key was left as it is, or that repeated an entry written earlier.
    skipped: int = 0
    # Entries a check refused.
    refused: int = 0


class MemoryImportOutput(BaseModel):
    # How many entries ended each way.
    totals: _MemoryImportTotals
    # One result per entry, in request order.
    results: list[_MemoryImportResult]
    # True for a dry run: each outcome is what the import would do, and nothing was written.
    dry_run: bool = False
