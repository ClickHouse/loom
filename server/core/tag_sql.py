"""The tag predicate the list reads share, so memory and documents fold case the same way."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def tag_filter(tags: Sequence[str], params: dict[str, Any], *, every: bool = False) -> str:
    """Rows carrying any one of `tags` (all of them with `every`), in any case; binds `tags`."""
    params["tags"] = [str(t) for t in tags]
    fn = "hasAll" if every else "hasAny"
    row = "arrayMap(t -> lowerUTF8(t), tags)"
    asked = "arrayMap(t -> lowerUTF8(t), {tags:Array(String)})"
    return f"{fn}({row}, {asked})"
