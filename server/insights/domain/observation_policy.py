"""Conservative classification of native SHOW CREATE ROW POLICIES output.

The access store supplies these definitions directly. Unknown syntax refuses;
neither policy names nor a filtered system-table catalog establishes visibility.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from server.insights.domain.analysis import (
    ANALYST_USER,
    OTHERS_POLICY_NAME,
    POLICY_NAME,
    others_policy_tables,
)

_NAMES = frozenset({"word", "identifier"})
_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<quoted>'(?:[^'\\]|\\.|'')*'|"
    r'`(?:[^`\\]|\\.|``)*`|"(?:[^"\\]|\\.|"")*")'
    r"|(?P<word>[^\W\d]\w*)|(?P<number>[0-9]+)|(?P<symbol>[^\s])"
)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str


def _tokens(statement: str) -> list[_Token]:
    """Read only native policy framing; expressions stay opaque and never execute."""
    result = []
    for match in _TOKEN.finditer(statement):
        kind, text = match.lastgroup, match.group()
        if kind == "space":
            continue
        if kind == "quoted":
            quote = text[0]
            if quote == "'":
                kind = "string"
            else:
                # Unknown identifier escapes must not misclassify a relevant target.
                if "\\" in text:
                    raise ValueError("unsupported identifier escape")
                kind, text = "identifier", text[1:-1].replace(quote * 2, quote)
        elif kind == "symbol":
            if text not in ".()[]{}+-*/%=<>!,:|&^~" or statement.startswith(
                ("--", "/*"), match.start()
            ):
                raise ValueError("unsupported native policy syntax")
            kind = text
        assert kind is not None
        result.append(_Token(kind, text))
    return result


def _words(tokens: list[_Token], words: tuple[str, ...]) -> bool:
    return len(tokens) == len(words) and all(
        token.kind == "word" and token.text.upper() == word
        for token, word in zip(tokens, words, strict=True)
    )


def _permits_rewrite(statement: str, database: str, tables: frozenset[str]) -> bool:
    """Check the target and recipients before rewriting Loom's own policies."""
    try:
        tokens = _tokens(statement)
        if (
            len(tokens) < 8
            or not _words(tokens[:3], ("CREATE", "ROW", "POLICY"))
            or tokens[3].kind not in _NAMES
            or not _words(tokens[4:5], ("ON",))
            or tokens[5].kind not in _NAMES | {"*"}
            or tokens[6].kind != "."
            or tokens[7].kind not in _NAMES | {"*"}
        ):
            return False
        db, table = tokens[5], tokens[7]
        db_wild, table_wild = db.kind == "*", table.kind == "*"
        if not db_wild and db.text != database:
            return True
        if not table_wild and table.text not in tables:
            return True
        if db_wild or table_wild:
            return False
        tail = tokens[8:]
        if _words(tail[:2], ("AS", "RESTRICTIVE")):
            return tokens[3].text not in (POLICY_NAME, OTHERS_POLICY_NAME)
        # SHOW CREATE omits the default permissive clause.
        if _words(tail[:2], ("AS", "PERMISSIVE")):
            tail = tail[2:]
        if not _words(tail[:3], ("FOR", "SELECT", "USING")):
            return False
        opened, separators = [], []
        closing = {")": "(", "]": "[", "}": "{"}
        for index, token in enumerate(tail[3:], 3):
            if token.kind in {"(", "[", "{"}:
                opened.append(token.kind)
            elif token.kind in closing:
                if not opened or opened.pop() != closing[token.kind]:
                    return False
            elif not opened and _words([token], ("TO",)):
                separators.append(index)
        if opened or len(separators) != 1:
            return False
        split = separators[0]
        expression, recipients = tail[3:split], tail[split + 1 :]
        if not expression:
            return False
        if (
            len(recipients) == 1
            and recipients[0].kind in _NAMES
            and recipients[0].text == ANALYST_USER
        ):
            return tokens[3].text == POLICY_NAME
        return (
            tokens[3].text == OTHERS_POLICY_NAME
            and len(expression) == 1
            and expression[0].kind == "number"
            and expression[0].text == "1"
            and _words(recipients[:2], ("ALL", "EXCEPT"))
            and len(recipients) == 3
            and recipients[2].kind in _NAMES
            and recipients[2].text == ANALYST_USER
        )
    except Exception:  # noqa: BLE001 — parser errors can contain secret policy expressions
        return False


def analyst_policy_catalog_allows_writes(statements: tuple[str, ...], database: str) -> bool:
    """Check native database-scoped definitions before replacing Loom's policies."""
    tables = frozenset(others_policy_tables())
    return len(set(statements)) == len(statements) and all(
        _permits_rewrite(statement, database, tables) for statement in statements
    )
