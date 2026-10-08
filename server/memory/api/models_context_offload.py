"""context.offload / context.fetch — park a large tool result outside the
model context and read it back by key, in slices or by pattern.

The model context is the most expensive place to keep a 3,000-line log:
it is paid on every turn until compaction, and compaction loses it. The
working-memory scratchpad is small and always resident — the opposite of
what a blob wants. `context.offload` stores the content once (ZSTD, TTL)
and returns a one-line pointer carrying the key, size and a summary;
`context.fetch` returns a character slice or the lines matching a regex,
so the agent reads what it needs. Scope is the namespace: another agent in
it can fetch what this one parked (a working-memory block can name the key).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from server.memory.api.models_common import _Identity

MAX_CONTENT_CHARS = 8_000_000
MAX_FETCH_CHARS = 64_000
DEFAULT_FETCH_CHARS = 4_000
DEFAULT_TTL_SECONDS = 7 * 86_400


class ContextOffloadInput(_Identity):
    # The text to park; larger than `LOOM_CONTEXT_OFFLOAD_MAX_BYTES` (UTF-8 bytes) is a 413.
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    # Empty = content-addressed (`blob/<sha256 prefix>`): parking the same
    # output twice yields the same key and one stored row.
    blob_key: str = Field(default="", max_length=200, pattern=r"^[A-Za-z0-9_./:-]*$")
    # Media type stored with the content and returned by `context.fetch`.
    content_type: str = Field(default="text/plain", max_length=80)
    # Caller-provided one-liner (the tool name and what it returned). Empty =
    # the first 200 characters, flattened. No LLM call is made.
    summary: str = Field(default="", max_length=2000)
    # How long the blob stays fetchable, in seconds (1 minute to 90 days).
    ttl_seconds: int = Field(default=DEFAULT_TTL_SECONDS, ge=60, le=90 * 86_400)


class ContextOffloadOutput(BaseModel):
    # The key to fetch the content by: the one given, or `blob/<sha256 prefix>`.
    blob_key: str
    # Size of the stored content in UTF-8 bytes.
    content_bytes: int
    # Length of the stored content in characters.
    content_chars: int
    # SHA-256 hex digest of the content.
    sha256: str
    # The summary stored with the blob: the one given, or the content's first 200
    # characters.
    summary: str
    # When the blob stops being fetchable.
    expires_at: datetime
    # Ready to stand in for the content in the transcript.
    pointer: str


class ContextFetchInput(_Identity):
    # The key `context.offload` returned.
    blob_key: str = Field(min_length=1, max_length=200)
    # Character offset to start the slice at; with `pattern`, how many matches to skip.
    offset: int = Field(default=0, ge=0)
    # At most this many characters of content per call (1-64000).
    limit: int = Field(default=DEFAULT_FETCH_CHARS, ge=1, le=MAX_FETCH_CHARS)
    # Regex (Python syntax, case-insensitive). When set, the response is the
    # matching lines as `lineno: text`, up to `limit` characters, and
    # `offset` is a line offset into the matches instead of a char offset.
    pattern: str = Field(default="", max_length=500)
    # With `pattern`, lines of context shown around each match (0-10); context lines
    # are numbered `lineno-` and matches `lineno:`.
    context_lines: int = Field(default=0, ge=0, le=10)


class ContextFetchOutput(BaseModel):
    # False when no live, unexpired blob has this key in the namespace.
    found: bool
    # Echo of the input `blob_key`.
    blob_key: str
    # The character slice, or with `pattern` the matching lines as `lineno: text`.
    content: str = ""
    # Media type stored with the blob.
    content_type: str = ""
    # The summary stored with the blob.
    summary: str = ""
    # Length of the whole stored content in characters.
    total_chars: int = 0
    # Line count of the whole stored content.
    total_lines: int = 0
    # Echo of the input `offset`.
    offset: int = 0
    # -1 when the slice (or the match list) is exhausted.
    next_offset: int = -1
    # With `pattern`, how many lines of the whole content match; 0 otherwise.
    matched_lines: int = 0
    # When the blob stops being fetchable; None when not found.
    expires_at: datetime | None = None
