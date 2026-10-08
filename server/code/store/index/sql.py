"""SQL fragments and read helpers the code-plane readers share: batched
point reads, the search-scope and branch predicates, the test-path filter
and its Python twin, LIKE escaping, FixedString hash decoding.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from server.code.domain.search_scope import SearchScope


def _batched(items: list[Any], size: int) -> list[list[Any]]:
    """Fixed-size slices for a parameterised IN list — a file with thousands
    of symbols stays a handful of point reads, not one giant tuple array."""
    return [items[i : i + size] for i in range(0, len(items), size)] if items else []


async def _gather_batched[T](
    items: list[Any],
    size: int,
    read: Callable[[list[Any]], Awaitable[list[T]]],
    *,
    limit: int = 4,
) -> list[list[T]]:
    """The batches of `_batched(items, size)` read together, at most `limit`
    in flight, results in batch order. For the keyed point reads a request
    makes — each batch a handful of granules — the wall is otherwise the
    number of batches times the round trip. Worker rounds keep their
    sequential loops; concurrency there is load."""
    batches = _batched(items, size)
    if len(batches) <= 1:
        return [await read(b) for b in batches]
    gate = asyncio.Semaphore(max(1, limit))

    async def one(b: list[Any]) -> list[T]:
        async with gate:
            return await read(b)

    return list(await asyncio.gather(*(one(b) for b in batches)))


def _scope_sql(scope: SearchScope | None) -> tuple[str, dict[str, Any]]:
    """`AND <pattern> AND <path filters>` for a leg's WHERE, or ("", {})."""
    if scope is None or not scope.constrained:
        return "", {}
    return scope.where_sql()


def _scope_path_sql(scope: SearchScope | None) -> tuple[str, dict[str, Any]]:
    """Path filters only (code.grep carries its own content predicate)."""
    if scope is None or not scope.has_path_filters:
        return "", {}
    conds, params = scope.path_conditions()
    return " ".join("AND " + c for c in conds), params


def _branch_predicate(branch: str) -> str:
    """The per-namespace branch predicate on `code_pr_history`: a row whose
    `target_branch` is filled must name the namespace's branch; a row with ''
    (not yet placed by the backfill) matches any branch. '' branch = no
    predicate. Rendered into the SQL text; `branch` binds as {branch:String}."""
    if not branch:
        return ""
    return "AND (target_branch = '' OR target_branch = {branch:String})"


def is_test_path(path: str) -> bool:
    """The Python twin of `skip_tests_sql` — the same files."""
    p = path or ""
    return (
        p.startswith("tests/")
        or "/tests/" in p
        or "/test/" in p
        or "_test." in p
        or ".test." in p
        or ".spec." in p
    )


def skip_tests_sql(col: str) -> str:
    """The SQL that keeps a read to source files — no `tests/` trees, no
    `*_test.*` / `*.test.*` /
    `*.spec.*` files. Prepend to a WHERE with a leading AND. The `_` of
    `_test.` is escaped: bare, LIKE reads it as any one character and drops
    `latest.cpp` or `src/test.py`, which `is_test_path` keeps."""
    return (
        f" AND NOT (startsWith({col}, 'tests/') OR {col} LIKE '%/tests/%'"
        f" OR {col} LIKE '%/test/%' OR {col} LIKE '%\\\\_test.%'"
        f" OR {col} LIKE '%.test.%' OR {col} LIKE '%.spec.%')"
    )


def like_contains(text: str) -> str:
    """The LIKE pattern that matches exactly the rows whose
    value contains `text` — `%`, `_` and the backslash in the text escaped so
    the gate is a substring test, not a wildcard one."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _hex(value: Any) -> str:
    """A FixedString(64) hash as hex text. The CH client hands FixedString
    back as bytes; str(bytes) is the repr "b'...'", which compares equal to
    itself and so hides the mistake until the value is shown. Decode, strip
    FixedString's NUL padding, never repr."""
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("ascii", errors="ignore").rstrip("\x00")
    return str(value or "").rstrip("\x00")
