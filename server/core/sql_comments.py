"""The leading `--` and `/* */` comments of a SQL statement, as a regex prefix."""

from __future__ import annotations

# A comment body holds no `*/`, so a failed match backtracks once per comment.
# ClickHouse nests `/* */`; this does not: the text after a comment's first `*/` is the statement.
LEADING_COMMENTS = r"^\s*(?:--[^\n]*\n\s*|/\*(?:[^*]|\*(?!/))*\*/\s*)*"

__all__ = ["LEADING_COMMENTS"]
