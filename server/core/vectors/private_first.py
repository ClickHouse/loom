"""The two passes of a private-first vector search over several namespaces,
for every plane that fans out (memory, knowledge)."""

from __future__ import annotations


def private_first_passes(
    namespace: str, namespaces: list[str], visibility: dict[str, str]
) -> tuple[list[str], list[str]]:
    """The two passes of a private-first search over `namespaces`: the
    addressed `namespace` and the private ones, then the public ones. Both are
    empty when `visibility` is empty or names no private namespace: read them
    all at once. A namespace without an answer counts as private."""
    private = {ns for ns in namespaces if visibility.get(ns, "private") == "private"}
    if not visibility or not private:
        return [], []
    first = [ns for ns in namespaces if ns in private or ns == namespace]
    return first, [ns for ns in namespaces if ns not in first]
