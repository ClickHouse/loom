"""The hard tag predicate a list statement filters by, shared by the memory and the
document stores so both fold case the same way."""

from __future__ import annotations

from collections.abc import Sequence


def tag_filter(
    tags: Sequence[str], *, any_case: bool = False, every: bool = False
) -> tuple[str, list[str]]:
    """The predicate over a row's `tags` and its values: any one of `tags` (OR), or with
    `every` all of them; exact, or with `any_case` both sides lowercased by ClickHouse's
    `lowerUTF8`. The asked tags are bound as typed: one fold, in one place, so the two sides
    match whatever case mapping the server's release applies."""
    fn = "hasAll" if every else "hasAny"
    values = [str(t) for t in tags]
    if any_case:
        row = "arrayMap(t -> lowerUTF8(t), tags)"
        asked = "arrayMap(t -> lowerUTF8(t), {tags:Array(String)})"
        return f"{fn}({row}, {asked})", values
    return f"{fn}(tags, {{tags:Array(String)}})", values
