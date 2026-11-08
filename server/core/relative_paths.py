"""A path's leading `./` segments, removed in one pass.

`str.lstrip("./")` strips characters, not a prefix, and would eat the dot of
a dot-directory. Slicing off one `./` at a time copies the rest of the path
for each one, so a path of many of them costs the square of its length.
"""

from __future__ import annotations


def without_dot_slashes(path: str) -> str:
    """`././src/x` -> `src/x`; `.github/x` is kept."""
    i = 0
    while path.startswith("./", i):
        i += 2
    return path[i:]
