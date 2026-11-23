"""Response bodies retain source and distinct context without repeated doc comments."""

import pytest

from server.code.domain.units import (
    budget_units,
    estimate_unit_tokens,
    unit_from_chunk,
    unit_from_symbol,
)


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
    "source",
    [
        'class Router:\n    class Router:\n        """class Router:"""\n',
        "# … continued from class Router:\n    class Router:\n"
        '        """# … continued from class Router:"""\n',
    ],
)
def test_nested_class_docstring_keeps_its_outer_context(source: str) -> None:
    unit = unit_from_chunk(
        {
            "path": "routes.py",
            "scope": "routes::Router",
            "kind": "class_skeleton",
            "start_line": 1,
            "end_line": 3,
            "content": source,
        }
    )
    served, _ = budget_units([unit], 0)
    assert served[0].body == source


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


@pytest.mark.parametrize(
    ("body", "signature"),
    [
        ("def validate(ctx, param, value): return value\n", "(ctx, param, value)"),
        (
            "    @decorate\n"
            "    async def validate(self, token: str, /, *, retry=(1, 2)):\n"
            "        return token\n",
            "(self, token: str, /, *, retry=(1, 2))",
        ),
        ("def validate(): pass\n", "()"),
        ("def validate(a) -> lambda: None: pass\n", "(a)"),
        ('def validate(x=f"{value=}"): pass\n', "(x=f'value={value!r}')"),
        ("def validate(a)\n", ""),
        (
            '    def validate(value="""start\n        middle\n    end"""):\n        return value\n',
            "(value='start\\n        middle\\n    end')",
        ),
        (
            '    def validate(value="""start\nend"""):\n        return value\n',
            "(value='start\\nend')",
        ),
        ("def other(a): pass\n", ""),
        ("def validate(:\n", ""),
        ("", ""),
    ],
)
def test_python_compiler_signature_requires_retained_declaration(body: str, signature: str) -> None:
    row = {
        "path": "src/cli.py",
        "qualified_name": "src.cli::validate",
        "kind": "function",
        "symbol_id": "compiler-id",
        "signature": "()",
        "tier": "scip",
    }
    unit = unit_from_symbol(row, body=body)
    assert unit.signature == signature
    assert unit.unit_id == "compiler-id"
    assert unit.body == body
    assert row["signature"] == "()"
    assert unit_from_symbol({**row, "tier": "treesitter"}).signature == "()"
    assert unit_from_symbol({**row, "path": "src/cli.go"}).signature == "()"


def test_long_derived_parameters_leave_room_for_the_source_pointer() -> None:
    default = "x" * 10_000
    body = f"def validate(value={default!r}):\n    return value\n"
    row = {
        "path": "src/cli.py",
        "qualified_name": "src.cli::validate",
        "kind": "function",
        "symbol_id": "compiler-id",
        "signature": "()",
        "tier": "scip",
        "start_line": 1,
        "end_line": 2,
    }
    unit = unit_from_symbol(row, body=body)
    assert unit.body == body
    served, _ = budget_units([unit], 1000)
    (card,) = served
    assert card.elided and not card.signature
    assert (card.path, card.start_line, card.end_line) == ("src/cli.py", 1, 2)
