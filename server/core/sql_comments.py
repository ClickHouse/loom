"""The leading `--` and `/* */` comments of a SQL statement, as a regex prefix."""

from __future__ import annotations

# A comment is `/*`, runs of non-`*` each closed by `*`s, then `/`: it ends at its first
# `*/` and every text has one way to match, so a failed match takes linear time.
# ClickHouse nests `/* */`; this does not: the text after a comment's first `*/` is the statement.
LEADING_COMMENTS = r"^\s*(?:--[^\n]*\n\s*|/\*[^*]*\*+(?:[^/*][^*]*\*+)*/\s*)*"

__all__ = ["LEADING_COMMENTS"]
