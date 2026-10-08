"""A memory key names one attribute; under `newest_wins` the
newest row for a key is the live one, whatever session wrote it."""

from __future__ import annotations

from dataclasses import replace

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.keys import strip_variant_suffixes
from server.memory.app.key_supersession import (
    same_key_victims,
    strip_variant_suffix,
)
from server.memory.domain.extracted_facts import keep_latest_per_key


def _live(mid: str, key: str, session: str, *, pinned: bool = False) -> dict:
    return {
        "memory_id": mid,
        "memory_key": key,
        "namespace": "ns",
        "agent": "a",
        "user_id": "-",
        "session_id": session,
        "pinned": pinned,
    }


def test_older_rows_with_the_same_key_retire_behind_the_new_one() -> None:
    new = [{"memory_id": "n1", "memory_key": "apple_ceo"}]
    live = [
        _live("o1", "apple_ceo", "chunk-00000"),
        _live("n1", "apple_ceo", "chunk-00005"),  # the new row itself
        _live("o2", "abb_group_ceo", "chunk-00009"),  # another key: untouched
    ]
    victims = same_key_victims(new, live)
    assert [(v["memory_id"], v["superseded_by"], v["session_id"]) for v in victims] == [
        ("o1", "n1", "chunk-00000")
    ]


def test_pinned_rows_never_retire_and_the_last_new_row_for_a_key_is_the_newest() -> None:
    new = [{"memory_id": "n1", "memory_key": "k"}, {"memory_id": "n2", "memory_key": "k"}]
    live = [
        _live("o1", "k", "s1"),
        _live("p1", "k", "s2", pinned=True),
        _live("n1", "k", "s3"),
        _live("n2", "k", "s3"),
    ]
    victims = same_key_victims(new, live)
    assert [(v["memory_id"], v["superseded_by"]) for v in victims] == [("o1", "n2")]
    assert same_key_victims([], live) == []
    assert same_key_victims(new, []) == []


def test_variant_suffixes_fold_back_onto_the_attribute_key() -> None:
    assert (
        strip_variant_suffix("andy_warhol_death_location_alternate") == "andy_warhol_death_location"
    )
    assert strip_variant_suffix("user_pr_status_update") == "user_pr_status"
    assert strip_variant_suffix("user_employer_new") == "user_employer"
    # a closed-window fact keeps its own key; two-part keys are whole keys
    assert strip_variant_suffix("user_employer_former") == "user_employer_former"
    assert strip_variant_suffix("user_current") == "user_current"
    facts = [
        ExtractedFact("apple_ceo_updated", "Apple's CEO is Tim Cook."),
        ExtractedFact("apple_ceo", "Apple's CEO is Tim Cook."),
    ]
    out = strip_variant_suffixes(facts)
    assert [f.memory_key for f in out] == ["apple_ceo", "apple_ceo"]
    assert out[1] is facts[1]  # untouched facts are the same object


def test_in_batch_collision_keeps_the_later_statement() -> None:
    facts = [
        {"key": "amy_winehouse_death_location", "value": "Amy Winehouse died in Camden Town."},
        {"key": "hines_ward_position", "value": "Hines Ward plays wide receiver."},
        {"key": "amy_winehouse_death_location", "value": "Amy Winehouse died in Sofia."},
        {"key": "", "value": "no key"},
    ]
    kept, dropped = keep_latest_per_key(facts)
    assert [f["value"] for f in kept] == [
        "Hines Ward plays wide receiver.",
        "Amy Winehouse died in Sofia.",
        "no key",
    ]
    assert dropped == ["amy_winehouse_death_location"]
    kept2, dropped2 = keep_latest_per_key(kept)
    assert kept2 == kept and dropped2 == []
    _ = replace  # ExtractedFact is a dataclass; replace is what strip_variant_suffixes uses


def test_same_key_victims_carries_value() -> None:
    from server.memory.app.key_supersession import same_key_victims

    new = [{"memory_id": "n1", "memory_key": "k", "value": "new"}]
    live = [
        {
            "memory_id": "o1",
            "memory_key": "k",
            "namespace": "ns",
            "agent": "a",
            "user_id": "-",
            "session_id": "s1",
            "pinned": False,
            "value": "old",
        }
    ]
    victims = same_key_victims(new, live)
    assert victims and victims[0]["value"] == "old"
