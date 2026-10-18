"""The tag predicate the list reads share, so memory and documents fold case the same way."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

# folded_any: one of the tags in any case; folded_all: every tag in any case; exact_any: one
# tag as written, as every memory.search leg matches.
TagMatch = Literal["folded_any", "folded_all", "exact_any"]


def tag_filter(tags: Sequence[str], params: dict[str, Any], *, match: TagMatch) -> str:
    """The predicate for rows carrying `tags` under `match`; binds `tags`."""
    params["tags"] = [str(t) for t in tags]
    if match == "exact_any":
        return "hasAny(tags, {tags:Array(String)})"
    fn = "hasAll" if match == "folded_all" else "hasAny"
    row = "arrayMap(t -> lowerUTF8(t), tags)"
    asked = "arrayMap(t -> lowerUTF8(t), {tags:Array(String)})"
    return f"{fn}({row}, {asked})"
