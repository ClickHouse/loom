"""Python import spellings of retained parser scope segments."""

from __future__ import annotations

from collections.abc import Sequence


def import_forms(path: str, parts: Sequence[str]) -> tuple[str, ...]:
    if not path.endswith(".py") or len(parts) < 2:
        return ()
    full = ".".join(parts)
    # Only the file's module scope is implicit. Constructors and directories named
    # __init__ keep their names.
    if path.endswith("/__init__.py") and parts[0].endswith(".__init__"):
        return full, ".".join([parts[0].removesuffix(".__init__"), *parts[1:]])
    return (full,)
