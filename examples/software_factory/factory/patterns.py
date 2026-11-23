"""The same bounded regex engine for ordinary search and optional indexed grep."""

from typing import Any


def compile_regex(pattern: str) -> Any:
    import re2

    options = re2.Options()
    options.log_errors = False
    try:
        return re2.compile(pattern, options=options)
    except re2.error as error:
        raise ValueError(f"Invalid RE2 pattern: {error}") from error
