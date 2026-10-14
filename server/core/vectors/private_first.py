"""The two passes of a private-first vector search over several namespaces,
for every plane that fans out (memory, knowledge)."""

from __future__ import annotations


def private_first_passes(
    namespace: str, namespaces: list[str], visibility: dict[str, str]
) -> tuple[list[str], list[str]]:
    """The two passes of a private-first search over `namespaces`: the
    addressed `namespace` and the private ones, then the public ones. A
    namespace missing from `visibility` counts as private. Both passes are
    empty, meaning read them all at once, when `visibility` is empty or every
    one of `namespaces` is answered public."""
    private = {ns for ns in namespaces if visibility.get(ns, "private") == "private"}
    if not visibility or not private:
        return [], []
    first = [ns for ns in namespaces if ns in private or ns == namespace]
    return first, [ns for ns in namespaces if ns not in first]
