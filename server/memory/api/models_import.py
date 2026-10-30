"""memory.import — memory items another memory system already distilled, written into
the caller's namespace through the bulk write path."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator

from server.memory.api.models_common import _Identity
from server.memory.domain.delete_marks import caller_trace
from server.memory.domain.memory_import import Outcome

# The most entries one memory.import takes: as many as memory.bulk_set conflict-checks in
# one call by default (LOOM_BULK_CONFLICT_CHECK_MAX_ENTRIES), so every entry gets the check.
MAX_IMPORT_ENTRIES = 200  # not a setting: a wire bound the request schema and SDKs publish
# The longest item text, in characters: past the embedder's token cap, about 30,000
# characters, nothing of a value is embedded, so search could not find a longer item.
MAX_IMPORT_VALUE_CHARS = 30_000  # not a setting: a wire bound
# The most tags one item carries, and the longest tag.
MAX_IMPORT_TAGS = 64  # not a setting: a wire bound
MAX_IMPORT_TAG_CHARS = 256  # not a setting: a wire bound
# The longest source pointer and category name an item names.
MAX_IMPORT_TRACE_CHARS = 512  # not a setting: a wire bound
MAX_IMPORT_CATEGORY_CHARS = 256  # not a setting: a wire bound


class _ImportEntry(BaseModel):
    # The item's text, stored verbatim; at least one character that is not whitespace.
    # Its key is `import:` and the first 24 hex characters of the text's SHA-256, so the
    # same text always lands on the same key.
    value: str = Field(..., min_length=1, max_length=MAX_IMPORT_VALUE_CHARS, pattern=r"\S")
    # Labels stored with the item, filterable at recall.
    tags: list[Annotated[str, Field(max_length=MAX_IMPORT_TAG_CHARS)]] = Field(
        default_factory=list, max_length=MAX_IMPORT_TAGS
    )
    # When the source system first knew the item, stored as `valid_at`; the row's
    # `created_at` is the time of the import. None = unknown.
    valid_at: datetime | None = None
    # Where the item came from in the source system (an id or a path); '' stores its key,
    # `import:<digest>`. May not start with `memory.delete_version:`.
    source_trace: str = Field(default="", max_length=MAX_IMPORT_TRACE_CHARS)
    # A category registered in the namespace (namespace.register_category) that the
    # item's key is linked to, as memory.assign_category links one, once the key holds the
    # item. A name the namespace has not registered links nothing and is noted
    # `category_unregistered`; the entry is still imported. '' = none.
    category: str = Field(default="", max_length=MAX_IMPORT_CATEGORY_CHARS)
    # Sensitivity tier; '' = detect from the value, as memory.bulk_set does.
    sensitivity: Literal["", "low", "medium", "high"] = ""

    @field_validator("source_trace")
    @classmethod
    def _source_trace(cls, v: str) -> str:
        return caller_trace(v)


class MemoryImportInput(_Identity):
    """Import memory items another memory system already distilled into the caller's
    namespace, through every check memory.bulk_set makes.

    Each item is stored under a key derived from its text (`import:` and 24 hex
    characters of its SHA-256), as `source_class=imported`, so a re-run of the same
    import writes nothing new. The entries share the caller's identity tuple. A dry run
    makes every check and answers the outcome each entry would have.
    """

    # The items to import, 1 to 200: each one's text, and optionally its tags, the time
    # the source first knew it, its source pointer, a category and a sensitivity tier.
    entries: list[_ImportEntry] = Field(..., min_length=1, max_length=MAX_IMPORT_ENTRIES)
    # Make every check and answer the outcome each entry would have, writing nothing: no
    # memory row, embedding, category link, audit row or usage event.
    dry_run: bool = False


class _ImportEntryResult(BaseModel):
    # The entry's position in the request's `entries`.
    index: int
    # The key the item is stored under, or would be.
    key: str
    # written | reinforced (another memory holds the fact and was reinforced) | skipped
    # (the key holds the item already) | refused (a check refused it).
    outcome: Outcome
    # Why, when the outcome is not a plain write. refused: blacklist | pinned (the key
    # holds another pinned value) | locked (the key is read_only) | conflict (too close to
    # a pinned memory). reinforced: merge (the same fact after canonicalisation) |
    # identity_duplicate (the same text under another key). skipped: unchanged (a
    # re-run) | repeated (an earlier entry carries the same text). written: duplicate or
    # conflicted, the status the row was stored with.
    reason: str = ""
    # `category_unregistered` when the entry names a category the namespace has not
    # registered: nothing was linked. '' otherwise.
    note: str = ""


class _ImportTotals(BaseModel):
    # Entries whose key got a new version.
    written: int = 0
    # Entries another memory already held; that memory was reinforced.
    reinforced: int = 0
    # Entries whose key already held them, or repeated an earlier entry.
    skipped: int = 0
    # Entries a check refused.
    refused: int = 0


class MemoryImportOutput(BaseModel):
    # How many entries ended each way.
    totals: _ImportTotals
    # One result per entry, in request order.
    results: list[_ImportEntryResult]
    # True when nothing was written: each outcome is what the import would do.
    dry_run: bool = False
