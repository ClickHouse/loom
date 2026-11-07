"""Which later versions of a memory's key a redaction scrubs with the one it names.

A copy writer (an entity pass, a lock, a touch), a delete and a restore each copy the
head they read, so the text of the redacted version lives on in the versions after it
until the key holds other text (a restatement of the same value is a copy too). Those
copies are scrubbed with it, so neither the history nor a restore of a deleted key
brings the text back. The key's live head is the memory's present and is left alone, as
the redacted version would be.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

REDACTED_VALUE = '"[REDACTED]"'  # a JSON string, as memory.set stores a string value


class Version(NamedTuple):
    version: int
    content_sha256: str


def later_copies(history: Sequence[Version], version: int, *, live: int | None) -> list[int]:
    """The versions right after `version` that carry its text, stopping at the first with
    other text and at the live head (`live`, None for none). A copy already redacted is
    named again, so a retry reaches every store a failed call left. `history` holds the
    key's versions from `version` on, ascending; a version read twice counts once."""
    seen: dict[int, Version] = {}
    for v in history:
        seen.setdefault(v.version, v)
    ordered = list(seen.values())
    if (
        not ordered
        or ordered[0].version != version
        or not is_content_hash(ordered[0].content_sha256)
    ):
        return []  # a row with no hash names no text to follow
    copies: list[int] = []
    for v in ordered[1:]:
        if live is not None and v.version >= live:
            break  # the live head, or a copy of it written since
        if v.content_sha256 != ordered[0].content_sha256:
            break  # other text
        copies.append(v.version)
    return copies


def is_content_hash(value: str) -> bool:
    """A real content hash, not the placeholder of a row written before hashes."""
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)
