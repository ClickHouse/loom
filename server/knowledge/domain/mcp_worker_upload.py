"""Validate raw refresh bytes before the existing ingest pipeline hashes its payload."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.knowledge.domain.mcp_worker_refresh import RefreshRejected

if TYPE_CHECKING:
    from server.knowledge.domain.mcp_worker_refresh import DocumentTask, RefreshScope

_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_RAW_BYTES = 4_000_000
_MAX_REVISION_BYTES = 2048


@dataclass(frozen=True, slots=True, repr=False)
class RawVersion:
    """Digest of the exact UTF-8 body; this is not an ingest digest."""

    sha256: str
    revision: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.sha256, str) or not _SHA256.fullmatch(self.sha256):
            raise RefreshRejected("invalid raw refresh digest")
        if self.revision is None:
            return
        try:
            if (
                isinstance(self.revision, str)
                and len(self.revision) <= _MAX_REVISION_BYTES
                and len(self.revision.encode()) <= _MAX_REVISION_BYTES
            ):
                return
        except UnicodeError:
            pass
        raise RefreshRejected("invalid raw refresh revision")


@dataclass(frozen=True, slots=True, repr=False)
class CurrentRawCopy:
    """Current accepted copy read under the validated scope and generation."""

    scope: RefreshScope
    resource_id: str
    version: RawVersion


@dataclass(frozen=True, slots=True, repr=False)
class ValidatedRefreshBody:
    version: RawVersion
    body: bytes | None


def validate_refresh_body(
    task: DocumentTask,
    version: RawVersion,
    body: bytes | None,
    *,
    current: CurrentRawCopy | None,
    max_body_bytes: int,
) -> ValidatedRefreshBody:
    """Validate bytes or an exact no-op after lease and owner checks.

    The caller must compare the current copy again when it settles the receipt.
    This does not accept a write or authorize access to a copy.
    """
    if type(max_body_bytes) is not int or not 0 < max_body_bytes <= _MAX_RAW_BYTES:
        raise RefreshRejected("invalid refresh body limit")
    if not isinstance(version, RawVersion):
        raise RefreshRejected("invalid refresh body version")
    if body is None:
        if not version.revision or current != CurrentRawCopy(task.scope, task.resource_id, version):
            raise RefreshRejected("refresh requires the current body")
        return ValidatedRefreshBody(version, None)
    if not isinstance(body, bytes) or not 0 < len(body) <= max_body_bytes:
        raise RefreshRejected("invalid refresh body size")
    if hashlib.sha256(body).hexdigest() != version.sha256:
        raise RefreshRejected("refresh body digest mismatch")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        raise RefreshRejected("refresh body is not UTF-8") from None
    return ValidatedRefreshBody(version, body)
