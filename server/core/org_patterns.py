"""The exact-name and trailing-star org patterns used by end-user issuers."""

from __future__ import annotations


def matches(pattern: str, org: str) -> bool:
    """Match one literal org; only the pattern's trailing star has special meaning."""
    return org.startswith(pattern[:-1]) if pattern.endswith("*") else org == pattern


def overlap(a: str, b: str) -> bool:
    """Whether two exact names or prefix patterns can match the same org."""
    if a.endswith("*") and b.endswith("*"):
        return a[:-1].startswith(b[:-1]) or b[:-1].startswith(a[:-1])
    if a.endswith("*"):
        return matches(a, b)
    if b.endswith("*"):
        return matches(b, a)
    return a == b
