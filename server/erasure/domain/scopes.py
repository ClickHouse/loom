"""The scope values of erasure requests as `loom.erasure_requests` holds them.

Pure and I/O-free: the erasure plane validates and runs its sagas by them, and the
knowledge plane's move of originals to the bucket reads the ledger by them.
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
