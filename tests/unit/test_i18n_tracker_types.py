"""Tracker contract family spellings retain their source cell and field binding."""

import pytest

from scripts.i18n import translate as t
from scripts.i18n.context import SourceContext
from server.tracker.domain.contract import REQUIRED

PATH = "docs/features/tracker/record-contract.md"
HEADER = "| Column | Family | Meaning |\n| --- | --- | --- |\n"
FIELDS = (
    ("labels", "assignees"),
    ("affected_versions", "fix_versions"),
    ("triage_reproduces_on", "triage_clean_on"),
    ("test_types", "reviewers", "src_components"),
)


def _row(fields, family="Array(String)", meaning=""):
    return "| " + ", ".join(f"`{field}`" for field in fields) + f" | {family} | {meaning} |\n"


@pytest.mark.parametrize("fields", FIELDS)
def test_contract_family_cells_preserve_the_implemented_type(fields):
    contract = REQUIRED["github_prs" if "test_types" in fields else "github_issues"]
    assert all(contract[field] == "Array(String)" for field in fields)
    source = HEADER + _row(fields)
    assert t.differences(source, source, source_path=PATH) == []


@pytest.mark.parametrize("case", ["path", "field", "column", "heading", "prose"])
def test_type_words_are_not_exempt_in_unrelated_source_nodes(case):
    source, path = HEADER + _row(FIELDS[0]), PATH
    if case == "path":
        path = "docs/other.md"
    elif case == "field":
        source = HEADER + _row(("other", "fields"))
    elif case == "column":
        source = HEADER + _row(FIELDS[0], "", "Array(String)")
    elif case == "heading":
        source = "## Array(String)\n"
    else:
        source = HEADER + "| Array(String) | | |\n"
    assert t.differences(source, source, source_path=path)


@pytest.mark.parametrize(
    "fields",
    [("labels",), ("assignees", "labels"), ("labels", "assignees", "other"), ("labels", "labels")],
)
def test_a_type_requires_its_complete_ordered_field_declaration(fields):
    source = HEADER + _row(fields)
    assert t.differences(source, source, source_path=PATH)


def test_a_mutated_source_type_or_descriptive_field_gets_no_protection():
    source = HEADER + _row(FIELDS[0], "Array(Int)")
    assert t.differences(source, source, source_path=PATH)
    source = (HEADER + _row(FIELDS[0])).replace(
        "`labels`, `assignees`", "`labels`, `assignees` example"
    )
    assert t.differences(source, source, source_path=PATH)


def test_type_protection_does_not_exempt_translatable_headers():
    source = HEADER.replace("Family", "Type family") + _row(FIELDS[0])
    assert t.differences(source, source, source_path=PATH)
    assert (
        t.differences(source, source.replace("Type family", "型のファミリー"), source_path=PATH)
        == []
    )


@pytest.mark.parametrize("changed", ["Array(Int)", "Array(String) Array(String)", "配列", ""])
def test_family_type_spelling_and_multiplicity_cannot_change(changed):
    source = HEADER + _row(FIELDS[0])
    assert t.differences(source, HEADER + _row(FIELDS[0], changed), source_path=PATH)


def test_family_qualifiers_and_neighboring_descriptions_still_need_translation():
    source = HEADER + _row(FIELDS[0], "Array(String) required", "The assigned labels.")
    assert t.differences(source, source, source_path=PATH)
    output = source.replace("required", "必須").replace(
        "The assigned labels.", "割り当てたラベル。"
    )
    assert t.differences(source, output, source_path=PATH) == []


@pytest.mark.parametrize("case", ["field", "column", "row"])
def test_family_type_cannot_move_away_from_its_source_binding(case):
    source = HEADER + _row(FIELDS[0]) + _row(("unrelated",), "", "Other values.")
    output = source.replace("Other values.", "別の値。")
    if case == "field":
        output = output.replace("`labels`, `assignees`", "`unrelated`").replace(
            "`unrelated` |  |", "`labels`, `assignees` |  |"
        )
    elif case == "column":
        output = output.replace("Array(String) |  |", " | Array(String) |")
    else:
        output = output.replace("Array(String) |  |", " |  |").replace(
            " |  | 別の値。", " | Array(String) | 別の値。"
        )
    assert t.differences(source, output, source_path=PATH)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_headerless_family_request_uses_the_complete_source_context(newline):
    english = (HEADER + _row(FIELDS[0], meaning="The assigned labels.")).replace("\n", newline)
    row = _row(FIELDS[0], meaning="The assigned labels.").rstrip("\n")
    start = english.index(row)
    scope = SourceContext(PATH, english, start, start + len(row))
    output = row.replace("The assigned labels.", "割り当てたラベル。")
    assert t.differences(row, output, source_context=scope) == []
    assert t.differences(row, output.replace("Array(String)", "Array(Int)"), source_context=scope)
