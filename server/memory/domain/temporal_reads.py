"""Pure pieces of the bi-temporal read contract: read tokens, the (V, K) pair a
request resolves to, and the per-key answer a reduced assertion set gives."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from server.core.errors import ValidationError
from server.memory.domain import temporal_model as tm

_TOKEN_PREFIX = "rt1."
# A K this far ahead of the server clock is still "now" (client clock skew).
_SKEW = timedelta(seconds=2)


def encode_token(seqs: Mapping[str, int]) -> str:
    if not seqs:
        return ""
    raw = json.dumps(dict(sorted(seqs.items())), separators=(",", ":")).encode()
    return _TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_token(token: str) -> dict[str, int]:
    """namespace -> sequence; a malformed token is a 422."""
    if not token:
        return {}
    if not token.startswith(_TOKEN_PREFIX):
        raise ValidationError("read_token is not a loom read token")
    body = token[len(_TOKEN_PREFIX) :]
    try:
        parsed = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return {str(k): int(v) for k, v in parsed.items()}
    except (ValueError, TypeError, AttributeError) as e:
        raise ValidationError("read_token is malformed") from e


@dataclass(frozen=True)
class Axes:
    """The resolved read: world time V, knowledge time K (None = the head)."""

    valid: datetime
    known: datetime | None

    @property
    def historical(self) -> bool:
        return self.known is not None


def resolve_axes(
    valid_as_of: datetime | None,
    known_as_of: datetime | None,
    *,
    now: datetime | None = None,
) -> Axes:
    """`known_as_of` alone reads the world at K; `valid_as_of` alone reads it as
    recorded now. A future K is refused."""
    now = now or datetime.now(UTC)
    k = tm.utc(known_as_of)
    if k is not None and k > now + _SKEW:
        raise ValidationError(
            "known_as_of is in the future: loom cannot know what it has not recorded"
        )
    v = tm.utc(valid_as_of) or k or now
    return Axes(valid=v, known=k)


def reference_time(args: Any) -> datetime | None:
    """The instant a time-anchored read asks about: V, else K; None for a live read."""
    return getattr(args, "valid_as_of", None) or getattr(args, "known_as_of", None)


def answer(
    assertions: Iterable[tm.Assertion],
    v: datetime,
    *,
    policy: tm.ValidityPolicy = "compat",
    include_statuses: frozenset[str] = frozenset({"active", "conflicted"}),
) -> tuple[list[tm.Assertion], bool]:
    """(assertions that apply at V, disputed). Expired-at-V and unserved statuses
    are left out: expiry and status are assessed history, read at their K."""
    # Filtered first: a proposed or expired snapshot never hides the served one.
    live = [
        a
        for a in assertions
        if a.status in include_statuses and (a.expires_at is None or a.expires_at > v)
    ]
    held = tm.valid_now(live, v, policy=policy)
    return held, tm.disputed(held)


def to_epoch_ns(d: datetime) -> int:
    d = tm.utc(d) or d
    return int(d.timestamp() * 1_000_000) * 1000
