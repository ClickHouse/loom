"""Opaque keyset pagination cursors, shared by every paginated op
(audit.list, conversation.list, knowledge.list, grants.list, …).

The cursor encodes a (DateTime, str) keyset pair. The `extra` field is
whatever stable tiebreaker the caller paginates on — request_id,
msg_id, source_uri, etc. — to break ties when multiple rows share the
same timestamp.

A bound cursor (`encode_bound_cursor`) also names the scope it was issued
for, the op and the identity it lists, under a MAC keyed by the root
secret: it reads back only in that scope, and a position nobody issued is
refused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Sequence
from datetime import datetime

from server.core.errors import ValidationError


def encode_cursor(ts: datetime, extra: str) -> str:
    """Opaque base64 of `<iso-ts>|<extra>` for keyset pagination."""
    raw = f"{ts.isoformat()}|{extra}"
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


def encode_bound_cursor(scope: Sequence[str], ts: datetime, extra: str) -> str:
    """`encode_cursor` bound to `scope` (the op name, then the identity it
    lists): base64 of `<mac>|<iso-ts>|<extra>`."""
    raw = f"{ts.isoformat()}|{extra}"
    return base64.urlsafe_b64encode(f"{_mac(scope, raw)}|{raw}".encode()).decode("ascii")


def decode_bound_cursor(cursor: str, scope: Sequence[str]) -> tuple[datetime, str]:
    """Reverse of `encode_bound_cursor` under the same `scope`. A cursor
    issued for another scope, edited, or not issued here is the caller's
    error: `ValidationError` (422)."""
    try:
        text = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        mac, _, raw = text.partition("|")
        ts_str, _, extra = raw.partition("|")
        if hmac.compare_digest(mac.encode(), _mac(scope, raw).encode()):
            return datetime.fromisoformat(ts_str), extra
    except ValueError:
        pass
    raise ValidationError("cursor is not one this op issued for this request")


def _mac(scope: Sequence[str], raw: str) -> str:
    """The MAC of a position in `scope`, keyed by the root secret, which every
    process of a deployment holds; rotating it retires the cursors issued
    before."""
    from server.settings import settings

    key = hashlib.sha256(b"loom.cursor:" + str(settings.jwt_secret).encode()).digest()
    msg = json.dumps([*scope, raw], separators=(",", ":")).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]
