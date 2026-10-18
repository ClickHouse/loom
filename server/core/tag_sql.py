"""The tag predicate the list reads share, so memory and documents fold case the same way."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

# any: one of the tags in any case; all: every tag in any case; exact: one tag as written,
# as every memory.search leg matches.
TagMatch = Literal["any", "all", "exact"]


def tag_filter(tags: Sequence[str], params: dict[str, Any], *, match: TagMatch = "any") -> str:
    """The predicate for rows carrying `tags` under `match`; binds `tags`."""
    params["tags"] = [str(t) for t in tags]
    if match == "exact":
        return "hasAny(tags, {tags:Array(String)})"
    fn = "hasAll" if match == "all" else "hasAny"
    row = "arrayMap(t -> lowerUTF8(t), tags)"
    asked = "arrayMap(t -> lowerUTF8(t), {tags:Array(String)})"
    return f"{fn}({row}, {asked})"
