"""The `promoted_from` entry: built and parsed back, a key holding `/` included."""

from __future__ import annotations

import pytest

from server.memory.domain.promotion_source import parse_promotion_source, promotion_source


@pytest.mark.parametrize("key", ["arr_note", "definition/arr", "a/b/c"])
def test_an_entry_parses_back_to_its_fields(key: str) -> None:
    entry = promotion_source("o", "n", "bot", "-", "-", key)
    assert parse_promotion_source(entry) == {
        "org": "o",
        "namespace": "n",
        "agent": "bot",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
    }


@pytest.mark.parametrize("entry", ["", "o/n/a/u", "o/n/a/u/s/"])
def test_a_malformed_entry_is_none(entry: str) -> None:
    assert parse_promotion_source(entry) is None


def test_an_identity_part_holding_a_slash_is_read_into_the_key() -> None:
    # The format cannot tell, so the key takes everything after the fifth `/`.
    parsed = parse_promotion_source("o/n/-/-/run/7/definition/arr")
    assert parsed is not None
    assert (parsed["session_id"], parsed["memory_key"]) == ("run", "7/definition/arr")


@pytest.mark.parametrize("field", ["agent", "user_id", "session_id", "memory_key"])
def test_consent_covers_every_ambiguous_user_interpretation(field):
    from server.memory.domain.promotion_source import promotion_users

    identity = {
        "org": "o/div",
        "namespace": "n/team",
        "agent": "bot",
        "user_id": "alice",
        "session_id": "session",
        "memory_key": "definition/arr",
    }
    identity[field] += "/segment"
    users = promotion_users([promotion_source(**identity)], identity["org"], identity["namespace"])
    assert users is not None and identity["user_id"] in users
    if field == "user_id":
        assert "alice" in users and "alice/segment" in users


@pytest.mark.parametrize(
    "entries", [[], ["wrong/prefix/a/alice/s/k"], ["o/n/a/alice/s/"], ["o/n/" + "x/" * 1000 + "k"]]
)
def test_incomplete_source_denial_proof_cannot_authorize_plaintext(entries):
    from server.memory.domain.promotion_source import promotion_users

    assert promotion_users(entries, "o", "n") is None


@pytest.mark.parametrize(
    "field", ["org", "namespace", "agent", "user_id", "session_id", "memory_key"]
)
def test_positive_proof_finds_the_actual_identity_despite_slashes(field):
    from server.memory.domain.promotion_source import promotion_keys

    identity = dict(
        org="o",
        namespace="n",
        agent="bot",
        user_id="alice",
        session_id="s",
        memory_key="definition/arr",
    )
    identity[field] += "/team"
    keys = promotion_keys([promotion_source(**identity)], identity["org"], identity["namespace"])
    assert keys is not None and tuple(identity.values()) in keys


@pytest.mark.parametrize("key", ["path/", "/", "a//", "//", "a//b/"])
@pytest.mark.parametrize("user", ["", "alice/team", "alice"])
def test_complete_source_key_may_end_in_a_slash(key, user):
    from server.memory.domain.promotion_source import promotion_keys, promotion_users

    identity = ("o", "n", "agent", user, "session", key)
    entry = promotion_source(*identity)
    assert user in promotion_users([entry], "o", "n")
    assert identity in promotion_keys([entry], "o", "n")
