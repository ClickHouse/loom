"""The scope values of erasure requests as `loom.erasure_requests` holds them.

Pure and I/O-free: the erasure plane validates and runs its sagas by them, and the
knowledge plane's move of originals to the bucket and its document restore read the
ledger by them.
"""

from __future__ import annotations

import json


def parse_namespace_list(scope_value: str) -> list[str]:
    """The namespaces of a `namespace_list` scope: a JSON array of strings or
    an object with a `namespaces` array. Duplicates fold; order is kept."""
    try:
        parsed = json.loads(scope_value or "")
    except json.JSONDecodeError as e:
        raise ValueError(f"not JSON ({e.msg})") from None
    raw = parsed.get("namespaces") if isinstance(parsed, dict) else parsed
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise ValueError("expected a JSON array of non-empty strings")
    seen: dict[str, None] = {}
    for x in raw:
        seen.setdefault(x.strip(), None)
    return list(seen)


def names_namespace(scope_type: str, scope_value: str, namespace: str) -> bool | None:
    """Whether a namespace scope names `namespace`: None for a scope that is not a
    namespace's, or a list it cannot read."""
    if scope_type == "namespace":
        return scope_value == namespace
    if scope_type == "namespace_list":
        try:
            return namespace in parse_namespace_list(scope_value)
        except ValueError:
            return None
    return None


def may_cover(scope_type: str, scope_value: str, namespace: str) -> bool:
    """Whether an erasure of this scope may reach rows of `namespace`. A user scope always
    may (a caller narrows those by writer); an unreadable list or unknown scope does too."""
    return names_namespace(scope_type, scope_value, namespace) is not False


def _scope_flag(scope_value: str, key: str, default: bool) -> bool:
    """A flag as stored in a `namespace_list` scope_value object, so a resumed
    saga (the recovery worker re-reads the ledger row) keeps the requester's
    choice; `default` for a bare array or a missing key."""
    try:
        parsed = json.loads(scope_value or "")
    except json.JSONDecodeError:
        return default
    if isinstance(parsed, dict) and key in parsed:
        return bool(parsed[key])
    return default


def parse_external_flag(scope_value: str, default: bool = True) -> bool:
    return _scope_flag(scope_value, "external", default)


def parse_purge_flag(scope_value: str, default: bool = False) -> bool:
    return _scope_flag(scope_value, "purge", default)


def applied_purge(scope_type: str, scope_value: str, requested: bool) -> bool:
    """Whether a saga of this scope deletes rather than tombstones, as it runs: a
    user's as requested, a namespace list's from its scope (`requested` when the
    scope does not say), a single namespace's never."""
    if scope_type == "user_id":
        return requested
    if scope_type == "namespace_list":
        return parse_purge_flag(scope_value, default=requested)
    return False
