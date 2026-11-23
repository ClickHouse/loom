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


@pytest.mark.parametrize("quote", ['"""', "'''"])
@pytest.mark.parametrize("prefix", ["Routes and", "Routes and handlers.", "Continuation context"])
def test_python_class_skeleton_keeps_its_source_docstring(quote: str, prefix: str) -> None:
    source = (
        "class Router(Base):\n"
        f"    {quote}Routes and handlers.{quote}\n"
        "    def route(self):\n"
        "    # … body elided (indexed as its own chunk) …\n"
    )
    unit = unit_from_chunk(
        {
            "path": "routes.py",
            "scope": "routes::Router",
            "kind": "class_skeleton",
            "start_line": 10,
            "end_line": 28,
            "content": prefix + "\n" + source,
        }
    )
    served, tokens = budget_units([unit], 0)
    expected = unit.body if prefix == "Continuation context" else source
    assert served[0].body == expected
    assert served[0].pointer == unit.pointer
    assert tokens == estimate_unit_tokens(served[0])
    assert unit.body == prefix + "\n" + source


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


@pytest.mark.parametrize("granularity", ["file", "skeleton", "card"])
def test_an_expanded_body_is_not_mapped_as_a_source_chunk(granularity: str) -> None:
    content = 'Build the route.\ndef route():\n    """Build the route."""\n'
    unit = unit_from_chunk(
        {"kind": "function", "start_line": 1, "end_line": 2, "content": content}
    ).model_copy(update={"granularity": granularity})
    served, _ = budget_units([unit], 0)
    assert served[0].body == content
