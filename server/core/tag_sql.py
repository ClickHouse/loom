"""The tag predicate memory.list and knowledge.list share, so both fold case the same way.

memory.proposals, namespace.list and every other tag filter match tags exactly.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

# folded_any: one of the tags in any case; folded_all: every tag in any case; exact_any: one
# tag as written, as every memory.search leg matches.
TagMatch = Literal["folded_any", "folded_all", "exact_any"]


@dataclass(frozen=True)
class TagFilter:
    """The tags a read keeps and how a row's tags must match them."""

    tags: tuple[str, ...]
    match: TagMatch

    def __post_init__(self) -> None:
        # No filter is None, so an empty tuple never reaches the SQL.
        if not self.tags:
            raise ValueError("a TagFilter needs at least one tag")


def list_tag_filter(tags: Sequence[str], every: bool) -> TagFilter | None:
    """A list op's filter: every tag when `tags_every` is set, otherwise any one, in any case."""
    kept = tuple(t for t in tags if t)
    if not kept:
        return None
    return TagFilter(kept, "folded_all" if every else "folded_any")


def tag_where(tag_filter: TagFilter, params: dict[str, Any]) -> str:
    """The predicate for rows carrying the filter's tags under its match; binds `tags`."""
    params["tags"] = list(tag_filter.tags)
    if tag_filter.match == "exact_any":
        return "hasAny(tags, {tags:Array(String)})"
    fn = "hasAll" if tag_filter.match == "folded_all" else "hasAny"
    row = "arrayMap(t -> lowerUTF8(t), tags)"
    asked = "arrayMap(t -> lowerUTF8(t), {tags:Array(String)})"
    return f"{fn}({row}, {asked})"
