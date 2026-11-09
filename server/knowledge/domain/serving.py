"""Which chunks a knowledge head serves, under the Postgres authority.

ClickHouse reads a chunk as live until something retires it, so it also reads as live
the older version's chunks while their retirement is still owed, a deleted document's
while its tombstone is, and the chunks a write inserted before its commit (one still to
commit, one refused, one whose cleanup failed). The head says which of them it serves:
docs/architecture/storage-authority.md, "Knowledge search".
"""

from __future__ import annotations

from typing import NamedTuple


class Serving(NamedTuple):
    """What a source's head serves: whether it is deleted, its revision, and the
    revision and version of the last commit that inserted its chunks (None when no
    commit recorded them: a head of a build that does not, or one whose writes inserted
    no chunks). The two are written together: both None or both set."""

    deleted: bool
    revision: int
    chunks_revision: int | None
    chunks_version: int | None


def serves(head: Serving | None, revision: int, doc_version: int) -> bool:
    """Whether a chunk written as `revision` (0: a chunk that recorded none) at
    `doc_version` is the head's. A source with no head (a document written before its
    namespace had heads) serves what ClickHouse holds."""
    if head is None:
        return True
    if head.deleted:
        return False
    if revision and revision != head.revision:
        return False
    # Recorded for this revision only: a commit that does not record them (a build that
    # does not) leaves the ones of an earlier revision, which say nothing about this one.
    if head.chunks_revision == head.revision:
        return doc_version == head.chunks_version
    return True
