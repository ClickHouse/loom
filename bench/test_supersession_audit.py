"""CH-free tests for bench/supersession_audit.py's pure core."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.supersession_audit import (
    VersionPair,
    pair_versions,
    parse_label,
    summarize,
)


def _row(key: str, version: int, value: str, session: str = "s1") -> dict:
    return {
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": session,
        "memory_key": key,
        "version": version,
        "value": value,
        "created_at": f"t{version}",
    }


def test_consecutive_versions_of_one_identity_become_pairs() -> None:
    rows = [_row("k", 3, "c"), _row("k", 1, "a"), _row("k", 2, "b"), _row("solo", 1, "x")]
    pairs = pair_versions(rows)
    assert [(p.older_version, p.newer_version, p.older_value, p.newer_value) for p in pairs] == [
        (1, 2, "a", "b"),
        (2, 3, "b", "c"),
    ]


def test_identity_spans_sessions_and_duplicate_version_rows_collapse() -> None:
    # a later session writing the same key IS the supersession (0186 / 0260)
    rows = [
        _row("k", 1, "a", "s1"),
        _row("k", 2, "b", "s1"),
        _row("k", 3, "c", "s2"),
        _row("k", 2, "b", "s1"),
    ]  # RMT duplicate of version 2
    pairs = pair_versions(rows)
    assert [(p.older_value, p.newer_value) for p in pairs] == [("a", "b"), ("b", "c")]
    # a different agent is a different identity
    assert pair_versions([_row("k", 1, "a"), {**_row("k", 2, "b"), "agent": "other"}]) == []


def test_parse_label_accepts_only_the_six_words() -> None:
    assert parse_label(" Corrects.") == "corrects"
    assert parse_label("unrelated\n") == "unrelated"
    assert parse_label("the newer one extends") == ""
    assert parse_label("") == ""


def test_summary_rates_count_reuse_and_restatement_as_false_supersession() -> None:
    pairs = [
        VersionPair("n", "k", 1, 2, "a", "b", label=lab)
        for lab in ("restates", "restates", "unrelated", "narrows", "corrects", "extends", "")
    ]
    s = summarize(pairs)
    assert s.pairs == 6  # the unlabelled pair is not counted
    assert s.labels["restates"] == 2 and s.labels["unrelated"] == 1
    assert round(s.false_supersession_rate, 3) == 0.5
    assert round(s.omission_rate, 3) == round(1 / 6, 3)
