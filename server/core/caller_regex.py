"""Regexes a caller sends (code.grep, code.search, context.fetch), compiled with RE2.

RE2 matches in time linear in the input, so no pattern can backtrack for
minutes on a crafted line, and it is the engine ClickHouse's `match()` uses,
so a pattern means the same thing in SQL and in Python. Constructs RE2 does
not have (backreferences, lookaround) are refused, not emulated.
"""

from __future__ import annotations

from typing import Any

import re2

from server.core.errors import ValidationError


def compile_caller_regex(
    pattern: str,
    *,
    case_sensitive: bool = True,
    dot_nl: bool = False,
    match_only: bool = False,
    max_mem: int | None = None,
) -> Any:
    """The compiled RE2 pattern, or ValidationError (422) naming what RE2 refused.
    `dot_nl` lets `.` match a newline, as ClickHouse's `match()` compiles;
    `match_only` keeps no capture groups (a yes/no search); `max_mem` bounds the
    memory RE2 keeps for the pattern, its DFA cache included."""
    options = re2.Options()
    options.case_sensitive = case_sensitive
    options.dot_nl = dot_nl
    options.never_capture = match_only
    if max_mem is not None:
        options.max_mem = max_mem
    options.log_errors = False
    try:
        return re2.compile(pattern, options)
    except re2.error as e:
        reason = e.args[0] if e.args else ""
        if isinstance(reason, bytes):
            reason = reason.decode("utf-8", "replace")
        raise ValidationError(
            f"pattern is not a valid RE2 regular expression ({reason}); "
            "RE2 has no backreferences or lookaround"
        ) from None
