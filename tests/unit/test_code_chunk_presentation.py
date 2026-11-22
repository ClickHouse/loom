"""Response bodies retain source and distinct context without repeated doc comments."""

import pytest

from server.code.domain.units import budget_units, estimate_unit_tokens, unit_from_chunk


def test_a_doc_prefix_already_in_the_source_is_served_once() -> None:
    source = 'def route():\n    """Build the route."""\n    return 1\n'
    row = {
        "path": "src/routes.py",
        "kind": "function",
        "start_line": 10,
        "end_line": 12,
        "content": "Build the route.\n" + source,
    }
    unit = unit_from_chunk(row)
    served, tokens = budget_units([unit], 0)
    assert served[0].body == source
    assert tokens == estimate_unit_tokens(served[0])
    assert unit.pointer == "src/routes.py:10-12"
    assert unit.body == row["content"]
    assert row["content"] == "Build the route.\n" + source


@pytest.mark.parametrize(
    "prefix",
    ["# … continued from def route():\n", "The route belongs to the public API.\n", ""],
)
def test_context_absent_from_the_source_is_kept(prefix: str) -> None:
    content = prefix + "    return 1\n"
    unit = unit_from_chunk(
        {"kind": "function", "start_line": 12, "end_line": 12, "content": content}
    )
    served, _ = budget_units([unit], 0)
    assert served[0].body == content


@pytest.mark.parametrize(
    "mapping",
    [
        {"kind": "class_skeleton", "start_line": 1, "end_line": 3},
        {"kind": "function", "start_line": 1, "end_line": 20},
        {"kind": "function", "start_line": 0, "end_line": 0},
    ],
)
def test_unmapped_source_keeps_its_presentation(mapping: dict) -> None:
    content = 'Build the route.\ndef route():\n    """Build the route."""\n'
    served, _ = budget_units([unit_from_chunk({**mapping, "content": content})], 0)
    assert served[0].body == content
