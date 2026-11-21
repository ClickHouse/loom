"""Exact review permits for proposal activation at the storage commit boundary."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from server.core.errors import AuthInsufficientError
from server.core.hashing import served_row_value, sha256_value

_permits: ContextVar[frozenset[tuple[Any, ...]]] = ContextVar(
    "proposal_review_permits", default=frozenset()
)
_PENDING = frozenset({"proposed"})
_SERVED = frozenset({"active", "conflicted"})


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def _key(row: Any) -> tuple[str, ...]:
    return tuple(
        str(_field(row, c) or "-")
        for c in ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
    )


def _stamp(row: Any) -> tuple[Any, ...]:
    return (
        *_key(row),
        str(_field(row, "memory_id")),
        int(_field(row, "version") or 0),
        str(_field(row, "content_sha256") or ""),
        sha256_value(served_row_value(row))
        if isinstance(row, Mapping)
        else sha256_value(row.value),
    )


def pending_review(row: Any) -> bool:
    if row is None:
        return False
    flag = _field(row, "requires_review")
    status = _field(row, "status")
    # Native history and snapshots written before the flag cannot distinguish a
    # rejected proposal from another rejection; require review for that legacy case.
    return bool((flag if flag is not None else status == "rejected") or status in _PENDING)


@contextmanager
def reviewed_changes(heads: Sequence[Any], rows: Sequence[Mapping[str, Any]]) -> Iterator[None]:
    """Only the authenticated review handler grants these exact head-to-row writes."""
    token = _permits.set(
        frozenset((_stamp(head), _stamp(row)) for head, row in zip(heads, rows, strict=True))
    )
    try:
        yield
    finally:
        _permits.reset(token)


def enforce_review(rows: Sequence[Mapping[str, Any]], current: Mapping[Any, Any]) -> None:
    """No ordinary writer can activate pending content or discard its review state."""
    heads = {_key(head): head for head in current.values() if head is not None}
    for row in rows:
        head = heads.get(_key(row))
        pending = pending_review(head)
        clears = pending and not pending_review(row)
        serves = (
            (pending or pending_review(row))
            and _field(row, "status") in _SERVED
            and _field(row, "tombstoned_at") is None
        )
        if (clears or serves) and (
            head is None or (_stamp(head), _stamp(row)) not in _permits.get()
        ):
            raise AuthInsufficientError(
                "An independent human administrator must approve this proposal "
                "before it can be served."
            )
