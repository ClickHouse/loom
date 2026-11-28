"""Where a Python definition's signature and docstring end, read from its first lines."""

from __future__ import annotations

import tokenize
from collections.abc import Sequence
from typing import NamedTuple

PYTHON_SUFFIXES = (".py", ".pyi")
# Lines read from a definition's first line to find the end of its docstring.
DEFINITION_HEADER_SCAN_LINES = 80
# Docstring lines an excerpt shows; a longer docstring keeps its first and last lines.
DOCSTRING_MAX_LINES = 12
_DOCSTRING_TAIL_LINES = 3
_LAYOUT = frozenset({tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT})
_STATEMENT_END = frozenset({tokenize.NEWLINE, tokenize.ENDMARKER})


class DefinitionHeader(NamedTuple):
    """Line counts from a definition's first line."""

    signature: int  # decorators and the signature
    end: int  # the signature and the docstring; equals `signature` without a docstring
    closed: bool  # false when the lines read stop before the docstring's end is known


def python_definition_header(lines: Sequence[str]) -> DefinitionHeader | None:
    """Find the signature and docstring in the first lines of a `def` or `class`.

    The lines may stop inside the body. None when they do not start a definition or
    stop inside its signature.
    """
    rows = iter(lines)
    signature = 0
    statement: list[tokenize.TokenInfo] = []
    try:
        for token in tokenize.generate_tokens(lambda: next(rows, "")):
            if token.type in _LAYOUT:
                continue
            if token.type not in _STATEMENT_END:
                statement.append(token)
            elif signature:
                if not statement:  # the lines stop at the signature
                    return DefinitionHeader(signature, signature, False)
                if all(part.type == tokenize.STRING for part in statement):
                    return DefinitionHeader(signature, statement[-1].end[0], True)
                return DefinitionHeader(signature, signature, True)
            elif not statement or token.type == tokenize.ENDMARKER:
                return None
            elif statement[0].string == "@":
                statement = []
            elif statement[0].string not in ("def", "class", "async"):
                return None
            else:
                signature = token.start[0]
                if statement[-1].string != ":":  # the body shares the signature's line
                    return DefinitionHeader(signature, signature, True)
                statement = []
    except tokenize.TokenError:
        if not signature:
            return None
        # With no token after the signature, the lines stop inside a string left open.
        return DefinitionHeader(signature, signature if statement else len(lines), bool(statement))
    except SyntaxError:  # includes IndentationError
        return None
    return None


def shown_header_lines(header: DefinitionHeader) -> tuple[range, range]:
    """Offsets an excerpt shows before and after the cut in a long docstring."""
    limit = header.signature + DOCSTRING_MAX_LINES
    if header.closed and header.end <= limit:
        return range(header.end), range(header.end, header.end)
    head = range(min(header.end, limit - _DOCSTRING_TAIL_LINES))
    if not header.closed:
        return head, range(header.end, header.end)
    return head, range(header.end - _DOCSTRING_TAIL_LINES, header.end)
