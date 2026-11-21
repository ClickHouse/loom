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

_SYSTEM_SOURCES = frozenset({"processes", "tables", "constraints", "clusters", "row_policies"})
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


def _classify(statement: str, database: str) -> tuple[str, str]:
    """Accept unrelated targets, or a structurally proved non-narrowing policy.

    The caller excludes the analyst user and enabled analyst role. Catalog-level
    pairing must also prove unrestricted access for every other user.
    """
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
            return ("refuse", "")
        db, table = tokens[5], tokens[7]
        db_wild, table_wild = db.kind == "*", table.kind == "*"
        relevant = (
            (db_wild or db.text == database)
            and (table_wild or table.text in {"agent_events", "agent_events_private"})
        ) or ((db_wild or db.text == "system") and (table_wild or table.text in _SYSTEM_SOURCES))
        if not relevant:
            return ("unrelated", "")
        tail = tokens[8:]
        # SHOW CREATE omits the default permissive clause.
        if _words(tail[:2], ("AS", "PERMISSIVE")):
            tail = tail[2:]
        if not _words(tail[:3], ("FOR", "SELECT", "USING")):
            return ("refuse", "")
        opened, separators = [], []
        closing = {")": "(", "]": "[", "}": "{"}
        for index, token in enumerate(tail[3:], 3):
            if token.kind in {"(", "[", "{"}:
                opened.append(token.kind)
            elif token.kind in closing:
                if not opened or opened.pop() != closing[token.kind]:
                    return ("refuse", "")
            elif not opened and _words([token], ("TO",)):
                separators.append(index)
        if opened or len(separators) != 1:
            return ("refuse", "")
        split = separators[0]
        expression, recipients = tail[3:split], tail[split + 1 :]
        if not expression:
            return ("refuse", "")
        if (
            len(recipients) == 1
            and recipients[0].kind in _NAMES
            and recipients[0].text == ANALYST_USER
        ):
            if (
                not db_wild
                and db.text == database
                and not table_wild
                and table.text in others_policy_tables()
                and tokens[3].text == POLICY_NAME
            ):
                return ("analyst", table.text)
            return ("refuse", "")
        unrestricted = (
            not db_wild
            and db.text == database
            and not table_wild
            and table.text in others_policy_tables()
            and tokens[3].text == OTHERS_POLICY_NAME
            and len(expression) == 1
            and expression[0].kind == "number"
            and expression[0].text == "1"
            and _words(recipients[:2], ("ALL", "EXCEPT"))
            and len(recipients) == 3
            and recipients[2].kind in _NAMES
            and recipients[2].text == ANALYST_USER
        )
        return ("others", table.text) if unrestricted else ("refuse", "")
    except Exception:  # noqa: BLE001 — parser errors can contain secret policy expressions
        return ("refuse", "")


def observer_policies_allow(statements: tuple[str, ...], database: str) -> bool:
    """Prove an unrestricted observer policy for every relevant policy-bearing table.

    An analyst-only policy can implicitly deny every other user. Its generated
    unrestricted companion is required even though the analyst role is disabled.
    System and wildcard policies refuse instead of guessing effective role rules.
    """
    classified = [_classify(statement, database) for statement in statements]
    if any(kind == "refuse" for kind, _table in classified) or len(set(statements)) != len(
        statements
    ):
        return False
    others = {table for kind, table in classified if kind == "others"}
    return all(kind != "analyst" or table in others for kind, table in classified)
