"""A merge that drops a date, quantity, identifier, name or
negation one source carried is refused; a merge that keeps them all passes."""

from __future__ import annotations

from server.memory.domain.merge_guard import distinguishing_values, merge_preserves


def test_values_are_extracted_by_kind() -> None:
    v = distinguishing_values(
        "On 12 May 2023 Caroline moved to Paris and the cache cap became 512 MB (PR #117959, v2.3.1); "
        "she does not eat dairy."
    )
    assert "12 may 2023" in v["dates"] and "2023" not in v["dates"]
    assert "512 mb" in v["quantities"]
    assert "#117959" in v["identifiers"] and "v2.3.1" in v["identifiers"]
    assert "caroline" in v["names"] and "paris" in v["names"]
    assert v["negation"] == {"negated"}
    assert distinguishing_values("she likes tea")["negation"] == set()


def test_merge_that_keeps_every_value_passes() -> None:
    a = "Caroline adopted a dog named Max on 3 March 2023."
    b = "Caroline's dog Max weighs 30 kg."
    merged = "Caroline adopted her dog Max on 3 March 2023; Max weighs 30 kg."
    assert merge_preserves([a, b], merged).ok


def test_merge_that_drops_a_date_or_number_is_refused() -> None:
    a = "Caroline adopted a dog named Max on 3 March 2023."
    b = "Caroline's dog Max weighs 30 kg."
    v = merge_preserves([a, b], "Caroline has a dog named Max who weighs 30 kg.")
    assert not v.ok and v.lost[0]["dates"] == ["3 march 2023"]
    assert "source[0] dates: 1" in v.counts()
    v2 = merge_preserves([a, b], "Caroline adopted Max on 3 March 2023.")
    assert not v2.ok and v2.lost[1]["quantities"] == ["30 kg"]


def test_negation_and_identifier_must_survive() -> None:
    a = "The user does not want notifications on weekends."
    b = "Notifications go to #ops-alerts."
    assert not merge_preserves(
        [a, b], "The user wants notifications on weekends via #ops-alerts."
    ).ok
    assert merge_preserves(
        [a, b], "The user does not want weekend notifications; they go to #ops-alerts."
    ).ok
    assert not merge_preserves([b], "Notifications go to the ops channel.").ok


def test_later_entry_wins_is_still_a_loss_the_guard_reports() -> None:
    """The consolidation prompt lets a later entry override an earlier one; the
    guard reports the dropped value so the caller decides. It does not know
    which side is current — that is the prompt's job, and refusing here is the
    conservative default the setting can lift."""
    v = merge_preserves(["Jon lives in Boston.", "Jon lives in Austin."], "Jon lives in Austin.")
    assert not v.ok and v.lost[0]["names"] == ["boston"]


def test_counts_name_the_kinds_lost_and_never_the_values() -> None:
    v = merge_preserves(["We met on 3 March 2023."], "We met.")
    assert v.counts() == "source[0] dates: 1" and "march" not in v.counts()
