"""The decision statement: its prose, every part kept whole."""

from __future__ import annotations

import json

import pytest

from server.memory.app import decisions as svc
from server.memory.domain import decision_statement as stmt


def test_the_statement_leaves_out_the_category_and_field_labels() -> None:
    value = stmt.memory_value(
        {"category": "storage:schema.sql", "scenario": "Spill to disk?", "outcome": "pin"}
    )
    assert value == "Spill to disk? Decided pin."


def test_memory_value_reads_as_a_statement() -> None:
    value = stmt.memory_value(
        {
            "category": "deploy",
            "scenario": "Roll back or hotfix the image?",
            "outcome": "rollback",
            "reasoning": "Rollback is 2 minutes; the fix is untested.",
            "alternatives": ["hotfix forward — rejected: untested", "wait."],
        }
    )
    assert value == (
        "Roll back or hotfix the image? "
        "Decided rollback because Rollback is 2 minutes; the fix is untested. "
        "Rejected: hotfix forward — rejected: untested; wait."
    )
    # A stored record joins alternatives with newlines; no reasoning, no alternatives.
    assert stmt.memory_value({"outcome": "pin", "alternatives": "", "scenario": "s"}) == (
        "s. Decided pin."
    )
    # A reasoning that is the outcome, case and whitespace aside, is a ban stated with no
    # reason: no "because".
    ban = {
        "outcome": "Client-side retries only; no server retry setting.",
        "reasoning": "client-side retries only;\nno server retry setting",
    }
    assert stmt.memory_value(ban) == "Decided Client-side retries only; no server retry setting."
    assert not stmt.states_reason(ban) and stmt.states_reason({"outcome": "a", "reasoning": "b"})
    assert stmt.memory_value({"outcome": "Never retry", "reasoning": "Never retry!"}) == (
        "Decided Never retry."
    )
    # A long ban keeps its end.
    long_ban = (
        "Every merge of a spilled run " + "reads its blocks in order, " * 8 + "and must not seek."
    )
    assert stmt.memory_value({"outcome": long_ban, "reasoning": long_ban}) == f"Decided {long_ban}"
    # The caller's casing is kept as written, proper nouns included.
    assert stmt.memory_value({"outcome": "Postgres for the ledger", "reasoning": "ClickHouse"}) == (
        "Decided Postgres for the ledger because ClickHouse."
    )
    failed = stmt.memory_value({"outcome": "pin", "realized_outcome": "broke.", "success": False})
    assert failed == "Decided pin. Realized: broke (failed)."
    # An outcome of full stops alone is kept rather than emptied.
    assert stmt.memory_value({"outcome": "..."}) == "Decided ..."


@pytest.mark.parametrize("ch", ["x", "回", "Р"])
def test_every_part_at_its_record_limit_is_stored_whole(ch: str) -> None:
    # The record's own field limits bound the statement; only the embedding reads an excerpt.
    from server.memory.domain.embed_text import embedding_texts
    from server.memory.domain.excerpt import EXCERPT_LIMIT

    word = ch + " "
    alts = [(f"option {i} " + word * 150)[: svc.ALTERNATIVE_CHARS].strip() for i in range(10)]
    payload = {
        "scenario": word * 1000,
        "outcome": word * 500,
        "reasoning": word * 2000,
        "alternatives": alts,
        "realized_outcome": word * 1000,
        "success": True,
    }
    value = stmt.memory_value(payload)
    for part in (payload["scenario"], payload["outcome"], payload["reasoning"], *alts):
        assert part.strip() in value
    assert value.endswith(f"Realized: {payload['realized_outcome'].strip()} (succeeded).")
    assert "..." not in value and "…" not in value
    key = svc.decision_key("c", "s")
    _, embed_text, _ = embedding_texts("decision", value, json.dumps(value), key)
    assert len(embed_text) <= EXCERPT_LIMIT and embed_text.endswith(f" (#{key})")


def test_outcome_drops_its_own_full_stop() -> None:
    value = stmt.memory_value({"outcome": "Use sorted runs.", "reasoning": "They merge."})
    assert value == "Decided Use sorted runs because They merge."


def test_every_statement_part_drops_its_trailing_punctuation() -> None:
    value = stmt.memory_value(
        {
            "scenario": "rollback:",
            "outcome": "pin the image",
            "reasoning": "it is fast;",
            "realized_outcome": ".",
        }
    )
    assert value.startswith("rollback. Decided pin the image")
    assert "because it is fast." in value
    assert "Realized: ." in value


def test_a_part_ending_in_a_spaced_full_stop_leaves_no_space() -> None:
    value = stmt.memory_value({"scenario": "s", "outcome": "use x .", "alternatives": ["a", "b ."]})
    assert "Decided use x." in value and "Rejected: a; b." in value


def test_a_reason_written_with_because_is_not_repeated() -> None:
    value = stmt.memory_value({"outcome": "pin", "reasoning": "Because the image drifts."})
    assert value == "Decided pin because the image drifts."


def test_a_punctuation_only_alternative_is_kept_as_written() -> None:
    value = stmt.memory_value({"outcome": "pin", "alternatives": ["wait.", "..."]})
    assert value == "Decided pin. Rejected: wait; ..."


@pytest.mark.parametrize(
    ("outcome", "shown"),
    [
        ("wait for it…", "wait for it…"),
        ("wait for it…;", "wait for it…"),
        ("wait for it...", "wait for it..."),
        ("wait for it...;", "wait for it..."),
        ("wait for it... .", "wait for it..."),
        ("wait for it,", "wait for it."),
    ],
)
def test_a_part_keeps_a_trailing_ellipsis_and_no_other_ending(outcome: str, shown: str) -> None:
    value = stmt.memory_value({"scenario": "s", "outcome": outcome})
    assert value.endswith(f"Decided {shown}")


@pytest.mark.parametrize(
    ("reasoning", "shown"),
    [
        ("Because.", "Decided pin."),
        ("Because...", "Decided pin."),
        ("Because, the image drifts", "because the image drifts"),
        ("Becausewise it drifts", "because Becausewise it drifts"),
    ],
)
def test_a_leading_because_is_dropped_on_a_word_boundary(reasoning: str, shown: str) -> None:
    value = stmt.memory_value({"outcome": "pin", "reasoning": reasoning})
    assert shown in value
