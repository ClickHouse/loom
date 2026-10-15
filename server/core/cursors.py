"""Opaque keyset pagination cursors, shared by every paginated op
(audit.list, conversation.list, knowledge.list, …).

The cursor encodes a (DateTime, str) keyset pair. The `extra` field is
whatever stable tiebreaker the caller paginates on — request_id,
msg_id, source_uri, etc. — to break ties when multiple rows share the
same timestamp.
"""

from __future__ import annotations

import base64
from datetime import datetime

from server.core.errors import ValidationError


def encode_cursor(ts: datetime, *extra: str) -> str:
    """Opaque base64 of `<iso-ts>|<extra>|…` for keyset pagination; each extra part
    is one tiebreaker column. A caller whose part may hold `|` reads the others from
    the right (audit.list's request id comes first, its 64-hex row hash last)."""
    raw = "|".join((ts.isoformat(), *extra))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Reverse of `encode_cursor`. Returns (ts, extra).

    A cursor this module did not issue is the caller's error, not the
    server's: raises `ValidationError` (422), never a bare decode error the
    transport would answer as a 500."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts_str, _, extra = raw.partition("|")
        return datetime.fromisoformat(ts_str), extra
    except ValueError:
        raise ValidationError("cursor is not one this op issued") from None
