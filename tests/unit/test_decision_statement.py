"""The decision statement: its prose, the clips that fit it to the embedded excerpt and
memory.for_file's line, and the cuts that never leave half a token."""

from __future__ import annotations

import json

import pytest

from server.memory.app import decisions as svc
from server.memory.domain import decision_statement as stmt
from server.memory.domain.files import detect_value_files


def test_the_statement_leaves_out_the_category_and_field_labels() -> None:
    value = stmt.memory_value(
        {"category": "storage:schema.sql", "scenario": "Spill to disk?", "outcome": "pin"}
    )
    assert value == "Spill to disk? Decided pin."
    # The category's slug is not in the text, so no file is detected from it.
    assert detect_value_files(value) == []


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
    # With no reason stated the outcome takes the reasoning's room: a long ban keeps its end.
    long_ban = (
        "Every merge of a spilled run " + "reads its blocks in order, " * 8 + "and must not seek."
    )
    assert len(long_ban) > stmt.STATEMENT_OUTCOME_CHARS
    assert stmt.memory_value({"outcome": long_ban, "reasoning": long_ban}) == f"Decided {long_ban}"
    # The caller's casing is kept as written, proper nouns included.
    assert stmt.memory_value({"outcome": "Postgres for the ledger", "reasoning": "ClickHouse"}) == (
        "Decided Postgres for the ledger because ClickHouse."
    )
    failed = stmt.memory_value({"outcome": "pin", "realized_outcome": "broke.", "success": False})
    assert failed == "Decided pin. Realized: broke (failed)."
    # An outcome of full stops alone is kept rather than emptied.
    assert stmt.memory_value({"outcome": "..."}) == "Decided ..."


def test_the_pre_edit_line_reaches_the_outcome() -> None:
    # memory.for_file prints a statement's first 220 characters: a long scenario is
    # clipped, so the outcome is always in that line.
    value = stmt.memory_value({"scenario": "Roll back?", "outcome": "roll back " * 100})
    assert "Decided roll back" in value[:60]
    long = stmt.memory_value({"scenario": "s " * 1000, "outcome": "pin"})
    assert "Decided pin" in long[:220]


def test_outcome_drops_its_own_full_stop() -> None:
    value = stmt.memory_value({"outcome": "Use sorted runs.", "reasoning": "They merge."})
    assert value == "Decided Use sorted runs because They merge."


@pytest.mark.parametrize("ch", ["x", "回"])
def test_a_statement_with_every_part_at_its_cap_is_embedded_whole(ch: str) -> None:
    from server.memory.domain.embed_text import embedding_texts
    from server.memory.domain.excerpt import EXCERPT_LIMIT

    word = ch + " "  # one-character words: each part is cut at its cap or one below
    payload = {
        "scenario": word * 1000,
        "outcome": word * 1000,
        "reasoning": word * 2000,
        "alternatives": [word * 150] * 10,
        "realized_outcome": word * 1000,
        "success": True,
    }
    value = stmt.memory_value(payload)
    key = svc.decision_key("c", "s")  # every key is as long: the suffix it adds is fixed
    assert len(json.dumps(value)) <= EXCERPT_LIMIT - len(f" (#{key})")
    _, embed_text, _ = embedding_texts("decision", value, json.dumps(value), key)
    assert json.dumps(value) in embed_text and value.endswith("(succeeded).")


def test_unspaced_cjk_text_keeps_its_leading_content() -> None:
    from server.memory.domain.excerpt import EXCERPT_LIMIT

    value = stmt.memory_value(
        {
            "scenario": "回" * 500,
            "outcome": "決" * 500,
            "reasoning": "理" * 1000,
        }
    )
    assert value.startswith("回回") and "Decided 決決" in value and "because 理理" in value
    key = svc.decision_key("c", "s")
    assert len(json.dumps(value)) <= EXCERPT_LIMIT - len(f" (#{key})")


def test_the_pre_edit_line_shows_the_outcome_at_full_caps() -> None:
    """memory.for_file prints the stored (JSON-quoted) value, cut at 220 characters."""
    from server.memory.app.for_file import _LINE_CHARS, _one_line

    outcome = "spill into sorted runs and merge them with a fan-in of eight"
    value = stmt.memory_value(
        {
            "scenario": "how the final DISTINCT spills to disk " * 10,
            "outcome": outcome,
            "reasoning": "sorted runs reuse the ORDER BY merge " * 20,
        }
    )
    line = _one_line(json.dumps(value), _LINE_CHARS)
    assert outcome[:60] in line


def test_spaced_non_ascii_text_is_cut_at_a_space() -> None:
    words = ["Решение", "принято", "Größe"]
    value = stmt.memory_value({"outcome": "pin", "reasoning": " ".join(words * 60)})
    reasoning = value.split(" because ", 1)[1].split("...", 1)[0]
    assert value.count("...") == 1 and set(reasoning.split()) <= set(words)


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


def test_spaced_korean_is_cut_at_a_space() -> None:
    reasoning = "데이터베이스를 사용합니다 " * 40
    value = stmt.memory_value({"scenario": "s", "outcome": "o", "reasoning": reasoning})
    kept = value.split("because ", 1)[1].split("...", 1)[0]
    assert set(kept.split()) <= {"데이터베이스를", "사용합니다"}


def test_a_cut_list_ends_without_its_separator() -> None:
    alts = [f"option {i} rejected for a stated reason" for i in range(10)]
    value = stmt.memory_value({"scenario": "s", "outcome": "o", "alternatives": alts})
    assert ";..." not in value


def test_a_cut_part_drops_a_trailing_dash() -> None:
    alts = [f"hotfix forward {i} — rejected" for i in range(20)]
    value = stmt.memory_value({"scenario": "s", "outcome": "o", "alternatives": alts})
    assert "—..." not in value and "-..." not in value


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
    ("field", "text", "shown"),
    [
        # A spaced ASCII term does not cost the CJK text after it its head.
        (
            "scenario",
            "使用 ClickHouse 存储日志数据" + "存储日志数据" * 20,
            "使用 ClickHouse 存储",
        ),
        # Nor does a spaced CJK word.
        ("reasoning", "原因 " + "存储日志数据" * 100, "because 原因 存储日志数据存储"),
        # Unspaced Korean after an ASCII term is cut at the space, never mid-word.
        ("scenario", "CI 파이프라인설정을변경합니다", "CI... Decided o"),
        # Full-width sentence punctuation is not doubled.
        ("scenario", "部署失败怎么办？", "部署失败怎么办？ Decided o"),
        ("outcome", "使用ClickHouse。", "Decided 使用ClickHouse."),
    ],
)
def test_wide_text_keeps_its_head_and_no_half_word(field: str, text: str, shown: str) -> None:
    payload = {"scenario": "s", "outcome": "o", field: text}
    assert shown in stmt.memory_value(payload)


def test_a_cyrillic_scenario_is_clipped_by_its_encoded_length() -> None:
    """The caps count the stored, JSON-encoded form, where each Cyrillic letter takes six."""
    value = stmt.memory_value({"scenario": "Решение " * 40, "outcome": "o"})
    assert value.startswith("Решение... Decided o")


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
