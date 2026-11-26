"""Cross-file consistency checks for the course retrieval worksheet labels.

These checks establish fixture consistency only. They do not grade retrieval
quality or decide whether an arbitrary question is answerable.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

RETRIEVAL_DIR = Path(__file__).resolve().parents[2] / "examples" / "course-starter" / "retrieval"
DEV_FILE = "development-labels.jsonl"
EVAL_FILE = "evaluation-labels.jsonl"
LABEL_FILES = (DEV_FILE, EVAL_FILE)
NOTES_FILE = "notes.md"
REQUIRED_FIELDS = (
    "id",
    "split",
    "question",
    "answerable",
    "document",
    "section",
    "evidence_substring",
)
EXPECTED_COUNTS = (6, 4, 2)  # rows, answerable, unanswerable per split

Rows = list[dict[str, Any]]
Supplied = tuple[dict[str, Rows], str, list[str]]


def parse_jsonl(name: str, text: str) -> tuple[Rows, list[str]]:
    """Parse JSONL text, reporting the filename and row of each invalid record."""
    rows: Rows = []
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        where = f"{name} row {number}"
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"{where}: invalid JSON ({exc.msg})")
            row = {}
        else:
            if not isinstance(row, dict):
                errors.append(f"{where}: record must be a JSON object")
                row = {}
        rows.append(row)
    return rows, errors


def split_sections(markdown: str) -> dict[str, str]:
    """Map each '## ' heading to the text beneath it."""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(lines) for name, lines in sections.items()}


def check_row(name: str, number: int, row: dict[str, Any], sections: dict[str, str]) -> list[str]:
    where = f"{name} row {number}"
    missing = [field for field in REQUIRED_FIELDS if field not in row]
    if missing:
        return [f"{where}: missing fields {missing}"]

    errors: list[str] = []
    for field in ("id", "question"):
        value = row[field]
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{where}: {field} must be a nonempty string")
    if not isinstance(row["answerable"], bool):
        return [*errors, f"{where}: answerable must be a boolean"]

    expected_split = name.removesuffix("-labels.jsonl")
    if row["split"] != expected_split:
        errors.append(
            f"{where}: split {row['split']!r} does not match the filename "
            f"(expected {expected_split!r})"
        )

    if not row["answerable"]:
        for field in ("document", "section", "evidence_substring"):
            if row[field] is not None:
                errors.append(f"{where}: {field} must be null for an unanswerable row")
        return errors

    if row["document"] != NOTES_FILE:
        errors.append(f"{where}: document must be {NOTES_FILE!r}")
    section, evidence = row["section"], row["evidence_substring"]
    section_ok = isinstance(section, str) and section in sections
    evidence_ok = isinstance(evidence, str) and bool(evidence.strip())
    if not section_ok:
        errors.append(f"{where}: section {section!r} does not exist in {NOTES_FILE}")
    if not evidence_ok:
        errors.append(f"{where}: evidence_substring must be a nonempty string")
    if section_ok and evidence_ok and evidence not in sections[section]:
        errors.append(f"{where}: evidence_substring {evidence!r} is not in section {section!r}")
    return errors


def validate_labels(files: dict[str, Rows], notes_text: str) -> list[str]:
    sections = split_sections(notes_text)
    errors: list[str] = []
    first_seen: dict[str, str] = {}
    for name, rows in files.items():
        for number, row in enumerate(rows, start=1):
            errors.extend(check_row(name, number, row, sections))
            row_id = row.get("id")
            if isinstance(row_id, str):
                if row_id in first_seen:
                    errors.append(
                        f"{name} row {number}: duplicate id {row_id!r} "
                        f"(first seen at {first_seen[row_id]})"
                    )
                else:
                    first_seen[row_id] = f"{name} row {number}"
        counts = (
            len(rows),
            sum(1 for r in rows if r.get("answerable") is True),
            sum(1 for r in rows if r.get("answerable") is False),
        )
        if counts != EXPECTED_COUNTS:
            errors.append(
                f"{name}: expected (rows, answerable, unanswerable) {EXPECTED_COUNTS}, got {counts}"
            )
    return errors


@pytest.fixture
def supplied() -> Supplied:
    files: dict[str, Rows] = {}
    parse_errors: list[str] = []
    for name in LABEL_FILES:
        rows, errors = parse_jsonl(name, (RETRIEVAL_DIR / name).read_text("utf-8"))
        files[name] = rows
        parse_errors.extend(errors)
    notes_text = (RETRIEVAL_DIR / NOTES_FILE).read_text("utf-8")
    return files, notes_text, parse_errors


def test_supplied_retrieval_labels_are_consistent(supplied: Supplied) -> None:
    files, notes_text, parse_errors = supplied
    assert parse_errors == []
    assert validate_labels(files, notes_text) == []


def test_invalid_json_is_reported_with_filename_and_row() -> None:
    rows, errors = parse_jsonl(DEV_FILE, '{"id": "dev-01"}\n{not json}\n')
    assert len(rows) == 2
    assert len(errors) == 1
    assert errors[0].startswith(f"{DEV_FILE} row 2:")


def _duplicate_id(files: dict[str, Rows]) -> None:
    files[EVAL_FILE][0]["id"] = "dev-01"


def _wrong_split(files: dict[str, Rows]) -> None:
    files[DEV_FILE][0]["split"] = "evaluation"


def _missing_section(files: dict[str, Rows]) -> None:
    files[DEV_FILE][0]["section"] = "No such section"


def _evidence_in_wrong_section(files: dict[str, Rows]) -> None:
    # dev-02's evidence belongs to "Fixture reset", not "Reservation window".
    files[DEV_FILE][0]["evidence_substring"] = files[DEV_FILE][1]["evidence_substring"]


def _populated_unanswerable_evidence(files: dict[str, Rows]) -> None:
    files[DEV_FILE][4]["evidence_substring"] = "up to fourteen days in advance"


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_duplicate_id, "duplicate id 'dev-01'"),
        (_wrong_split, "does not match the filename"),
        (_missing_section, "does not exist in notes.md"),
        (_evidence_in_wrong_section, "is not in section 'Reservation window'"),
        (_populated_unanswerable_evidence, "must be null for an unanswerable row"),
    ],
    ids=[
        "duplicate_id",
        "wrong_split",
        "missing_section",
        "evidence_in_wrong_section",
        "populated_unanswerable_evidence",
    ],
)
def test_inconsistent_labels_are_rejected(
    supplied: Supplied,
    mutate: Callable[[dict[str, Rows]], None],
    expected: str,
) -> None:
    files, notes_text, _ = supplied
    broken = copy.deepcopy(files)
    mutate(broken)
    errors = validate_labels(broken, notes_text)
    assert any(expected in error for error in errors), errors
