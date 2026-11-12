"""The leading `--` and `/* */` comments of a SQL statement, as a regex prefix."""

from __future__ import annotations

# A comment body holds no `*/`, so each comment ends at its first one and a statement
# that fails to match backtracks in linear time. ClickHouse nests `/* */` comments;
# this does not, so the text after a nested comment's first `*/` reads as no keyword.
LEADING_COMMENTS = r"^\s*(?:--[^\n]*\n\s*|/\*(?:[^*]|\*(?!/))*\*/\s*)*"

__all__ = ["LEADING_COMMENTS"]
