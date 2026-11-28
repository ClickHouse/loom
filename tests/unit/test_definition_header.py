"""A Python definition's signature and docstring are found in its first lines."""

from __future__ import annotations

import pytest

from server.code.domain.definition_header import (
    DOCSTRING_MAX_LINES,
    DefinitionHeader,
    python_definition_header,
    shown_header_lines,
)


def _lines(text: str) -> list[str]:
    return text.splitlines(keepends=True)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '    def record_once(self, func):\n        """Call once.\n\n        Not twice.\n'
            '        """\n\n        def wrapper(state):\n',
            DefinitionHeader(1, 5, True),
        ),
        (
            "@first\n@second(\n    option,\n)\nasync def handle(\n    request,\n) -> None:\n"
            "    # why\n\n    'One line.'  # trailing\n    return None\n",
            DefinitionHeader(7, 10, True),
        ),
        ('class Scaffold:\n    r"""Shared."""\n    name = None\n', DefinitionHeader(1, 2, True)),
        ('def joined():\n    "one" "two"\n    return 1\n', DefinitionHeader(1, 2, True)),
        ("def plain(value):\n    return value\n", DefinitionHeader(1, 1, True)),
        ("def inline(): return 1\n", DefinitionHeader(1, 1, True)),
        ('def called():\n    """Not a docstring.""".strip()\n', DefinitionHeader(1, 1, True)),
        ('def formatted():\n    f"""No {docstring}."""\n', DefinitionHeader(1, 1, True)),
        ("def cut():\n    return call(\n        first,\n", DefinitionHeader(1, 1, True)),
        # the lines stop inside the docstring, or before the body
        ('def long():\n    """Starts here\n    and goes on\n', DefinitionHeader(1, 3, False)),
        ("def unread(\n    value,\n):\n", DefinitionHeader(3, 3, False)),
    ],
)
def test_signature_and_docstring_extent(source: str, expected: DefinitionHeader) -> None:
    assert python_definition_header(_lines(source)) == expected


@pytest.mark.parametrize(
    "source",
    [
        "",
        "name = value\n",
        "def unfinished(\n    value,\n",
        "@decorator\n",
        "    @decorator\n  def dedented(): pass\n",
    ],
)
def test_lines_that_do_not_hold_a_complete_signature_have_no_header(source: str) -> None:
    assert python_definition_header(_lines(source)) is None


def test_a_long_docstring_keeps_its_first_and_last_lines() -> None:
    signature = 2
    exact = DefinitionHeader(signature, signature + DOCSTRING_MAX_LINES, True)
    assert shown_header_lines(exact) == (range(14), range(14, 14))

    longer = DefinitionHeader(signature, signature + DOCSTRING_MAX_LINES + 1, True)
    head, tail = shown_header_lines(longer)
    assert (head, tail) == (range(11), range(12, 15))
    assert len(head) + len(tail) == signature + DOCSTRING_MAX_LINES

    unread = DefinitionHeader(signature, 80, False)
    assert shown_header_lines(unread) == (range(11), range(80, 80))
    assert shown_header_lines(DefinitionHeader(3, 3, False)) == (range(3), range(3, 3))
