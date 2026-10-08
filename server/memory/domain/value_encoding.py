"""Spot a memory value an agent base64-encoded before writing it.

Loom stores `value` verbatim and hashes it server-side, so an agent that
wraps its content as base64 — whole, or inside an envelope such as
``{"encoding": "…base64…", "sha256": "…", "payload": "<base64>"}`` — gains
nothing and loses everything downstream: the embedding is of base64 noise,
fact extraction sees no facts, dedup and conflicts cannot compare, and the
viewer shows a wall of characters. The write still succeeds (a value is a
value); the response carries one advisory line so the agent can correct
itself in-band, and the viewer offers a decode.

Cheap by construction: values under 64 characters are never inspected, the
envelope check is one JSON parse of at most 256 KB, and the decode probes the
first 4 KB of the candidate.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

_MIN_LEN = 64
_MAX_JSON_PARSE = 256 * 1024
_PROBE_BYTES = 4096
_B64_RE = re.compile(r"^[A-Za-z0-9+/\s]+={0,2}$")
_URLSAFE_B64_RE = re.compile(r"^[A-Za-z0-9_\-\s]+={0,2}$")
_PAYLOAD_KEYS = ("payload", "data", "content", "body", "value", "blob")

ADVISORY = (
    "value is base64-encoded ({where}): Loom stores the value verbatim and hashes it "
    "server-side (content_sha256), so encoding buys nothing and defeats search, fact "
    "extraction, dedup and the viewer — write plain text or JSON instead"
)


def _decodes_to_text(candidate: str) -> bool:
    """True when the first 4 KB of `candidate` is valid base64 that decodes to
    mostly printable UTF-8 — the shape of an encoded document, not a hash or
    a token."""
    s = re.sub(r"\s+", "", candidate)
    if len(s) < _MIN_LEN or len(s) % 4 != 0:
        return False
    if not (_B64_RE.match(s) or _URLSAFE_B64_RE.match(s)):
        return False
    probe = s[: _PROBE_BYTES - (_PROBE_BYTES % 4)]
    if "-" in probe or "_" in probe:
        probe = probe.replace("-", "+").replace("_", "/")
    try:
        raw = base64.b64decode(probe, validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    if not text:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
    return printable / len(text) >= 0.9


def _envelope_advisory(obj: dict[str, Any]) -> str | None:
    encoding = str(obj.get("encoding", "") or obj.get("content_encoding", "") or "").lower()
    for key in _PAYLOAD_KEYS:
        inner = obj.get(key)
        if isinstance(inner, str) and len(inner) >= _MIN_LEN and _decodes_to_text(inner):
            where = f"envelope field '{key}'"
            if encoding:
                where += f", encoding='{encoding}'"
            return ADVISORY.format(where=where)
    if "base64" in encoding:
        return ADVISORY.format(where=f"envelope encoding='{encoding}'")
    return None


def encoded_value_advisory(value: Any) -> str | None:
    """One advisory line when `value` is base64 (whole or enveloped), else None.

    `memory.set` accepts any JSON value: a string is checked both ways, a
    dict is checked as an envelope, and numbers, booleans, lists and None
    are never base64."""
    if isinstance(value, dict):
        return _envelope_advisory(value)
    if not isinstance(value, str) or len(value) < _MIN_LEN:
        return None
    stripped = value.strip()
    if stripped.startswith("{") and len(stripped) <= _MAX_JSON_PARSE:
        try:
            obj = json.loads(stripped)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            return _envelope_advisory(obj)
    if _decodes_to_text(stripped):
        return ADVISORY.format(where="whole value")
    return None
