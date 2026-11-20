"""Pure SCIM token syntax and cell configuration policy."""

from __future__ import annotations

import re

_NAMED = re.compile(r"lsc_([a-z0-9-]{1,32})_[0-9a-f]{32,}")


def tokens(raw: str) -> list[str]:
    """Configured bearer entries; duplicates count and blank entries do not."""
    return [token.strip() for token in raw.split(",") if token.strip()]


def token_cell(token: str) -> str | None:
    """The named cell, None for an unnamed token; malformed reserved names refuse."""
    if not token.startswith("lsc_"):
        return None
    match = _NAMED.fullmatch(token)
    if match is None:
        raise ValueError("a named SCIM token has invalid syntax")
    return match[1]


def cell_policy_problem(raw: str, name: str, first: tuple[bool, str] | None) -> str | None:
    """A refusal, '' for valid policy, or None while first identity is unread."""
    if not name:
        return ""
    configured = tokens(raw)
    if not configured:
        return ""
    if len(configured) > 2:
        return "a cell accepts at most two configured SCIM tokens"
    unnamed = False
    for token in configured:
        try:
            named = token_cell(token)
        except ValueError:
            return "a configured named SCIM token has invalid syntax"
        if named is None:
            unnamed = True
        elif named != name:
            return "a configured SCIM token names another cell"
    if first is None:
        return None
    if first[1] != name:
        return "the SCIM first identity names another cell"
    if unnamed and not first[0]:
        return "an unnamed SCIM token is accepted only on the first cell"
    return ""
