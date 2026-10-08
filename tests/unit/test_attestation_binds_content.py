"""A row counts as verified only while its attestation binds its current
content: verified_sha256 set and equal to content_sha256. memory.verify, the
only writer of an attestation, always writes the hash with the actor, so a
legacy row with an actor and no hash is not verified."""

from __future__ import annotations

import pytest

from server.memory.domain.trust import attests
from server.memory.store.sql import attested_sql

H1, H2 = "a" * 64, "b" * 64


@pytest.mark.parametrize(
    ("by", "vsha", "csha", "want"),
    [
        ("human:alice", H1, H1, True),
        ("process:ci", H1, H1, True),
        ("human:alice", H1, H2, False),  # content changed after the attestation
        ("human:alice", "", H1, False),  # legacy: an actor, no attested hash
        ("human:alice", "0", "0", False),  # neither hash set
        ("", H1, H1, False),  # no actor
    ],
)
def test_attests(by: str, vsha: str, csha: str, want: bool) -> None:
    assert attests(by, vsha, csha) is want


def test_the_sql_form_checks_the_same_three_things() -> None:
    sql = attested_sql("m.")
    assert "m.verified_by != ''" in sql
    assert "m.verified_sha256 NOT IN ('', '0')" in sql
    assert "m.verified_sha256 = m.content_sha256" in sql


def test_every_sql_reader_of_the_attestation_uses_the_shared_form() -> None:
    """The orphan-repair mirror and memory.ask's trusted-premise read."""
    import inspect

    from server.memory.app.ask import reader
    from server.memory.store import sweeps

    assert attested_sql("m.") in sweeps._EMBEDDING_MIRROR_COLUMNS
    src = inspect.getsource(reader)
    assert "attested_sql()" in src and "verified_by != ''" not in src


def test_the_bulk_mirror_derives_verified_from_its_version_row() -> None:
    from server.memory.app.bulk_rows import _row_attested

    assert _row_attested({"verified_by": "human:a", "verified_sha256": H1, "content_sha256": H1})
    assert not _row_attested(
        {"verified_by": "human:a", "verified_sha256": H1, "content_sha256": H2}
    )
    assert not _row_attested({"content_sha256": H1})  # a first write
