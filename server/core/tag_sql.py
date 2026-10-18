"""The hard tag predicate a list statement filters by, shared by the memory and the
document stores so both fold case the same way."""

from __future__ import annotations

from collections.abc import Sequence


def tag_filter(tags: Sequence[str], *, every: bool = False) -> tuple[str, list[str]]:
    """The predicate over a row's `tags` and its values: any one of `tags` (OR), or with
    `every` all of them, in any case. Both sides are lowercased by ClickHouse's `lowerUTF8`
    and the asked tags are bound as typed, so the two sides fold with the same case mapping.
    The list reads run under FINAL, where the tags bloom index prunes nothing, so the
    lowercased form costs the same as an exact match."""
    fn = "hasAll" if every else "hasAny"
    row = "arrayMap(t -> lowerUTF8(t), tags)"
    asked = "arrayMap(t -> lowerUTF8(t), {tags:Array(String)})"
    return f"{fn}({row}, {asked})", [str(t) for t in tags]
