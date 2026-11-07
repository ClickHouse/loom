"""The versions a redaction scrubs with the one it names: the copies of its text right
after it, up to a value written anew or the key's live head."""

from __future__ import annotations

import pytest

from server.memory.domain.redaction_lineage import Version, later_copies

A, B = "a" * 64, "b" * 64


def _history(*shas: str) -> list[Version]:
    return [Version(v, sha) for v, sha in enumerate(shas, start=1)]


@pytest.mark.parametrize(
    ("history", "live", "copies"),
    [
        # Deleted right after (the tombstone copied the text), or the key's live head is
        # another memory's; then copied and deleted.
        (_history(A, A), None, [2]),
        (_history(A, A, A, A), None, [2, 3, 4]),
        # The live head copies the text (or a restore brought it back): the copies before.
        (_history(A, A, A), 3, [2]),
        # A copy written after the caller read the head is the head's, not older.
        (_history(A, A, A), 2, []),
        # Rewritten: the rest copied other text.
        (_history(A, B, B), None, []),
        (_history(A, A, B), 3, [2]),
        # Written again later: those versions belong to the later write.
        (_history(A, B, A, A), None, []),
        # No later version at all.
        (_history(A), None, []),
    ],
)
def test_the_copies_of_a_version_are_scrubbed_up_to_a_new_value_or_the_live_head(
    history: list[Version], live: int | None, copies: list[int]
) -> None:
    assert later_copies(history, 1, live=live) == copies


def test_a_version_with_no_hash_follows_nothing() -> None:
    """A row written before content hashes, or a version the history does not hold,
    names no text to follow."""
    no_hash = [Version(1, "0" + "\x00" * 63), Version(2, "0" + "\x00" * 63)]
    assert later_copies(no_hash, 1, live=None) == []
    assert later_copies(_history(A, A), 7, live=None) == []


def test_a_version_read_twice_counts_once() -> None:
    """A version table read before its merge can hold a row twice."""
    assert later_copies([Version(1, A), Version(1, A), Version(2, A)], 1, live=None) == [2]
