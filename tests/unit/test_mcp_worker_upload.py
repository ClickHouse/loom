"""Refresh settlement must not confuse raw bytes with indexed versions."""

from dataclasses import replace
from hashlib import sha256

import pytest

from server.knowledge.domain.mcp_worker_refresh import DocumentTask, RefreshRejected, RefreshScope
from server.knowledge.domain.mcp_worker_upload import (
    CurrentRawCopy,
    RawVersion,
    validate_refresh_body,
)

_SCOPE = RefreshScope(
    "org", "team", "owner", "source", "connection", "destination", 2, 4, 5, 8, "a" * 64
)
_TASK = DocumentTask("job", _SCOPE, "mcp://fixture/document")
_BODY = "café\r\n".encode()
_VERSION = RawVersion(sha256(_BODY).hexdigest(), "upstream-8")
_CURRENT = CurrentRawCopy(_SCOPE, _TASK.resource_id, _VERSION)


def test_refresh_preserves_exact_utf8_and_verifies_the_raw_digest() -> None:
    validated = validate_refresh_body(_TASK, _VERSION, _BODY, current=None, max_body_bytes=32)
    assert validated.body == _BODY and validated.version == _VERSION
    with pytest.raises(RefreshRejected, match="digest mismatch"):
        validate_refresh_body(
            _TASK, _VERSION, _BODY.replace(b"\r\n", b"\n"), current=None, max_body_bytes=32
        )


def test_refresh_requires_utf8_and_checks_actual_body_bytes() -> None:
    binary = b"\xff\xfe"
    for body, limit, reason in (
        (binary, 32, "not UTF-8"),
        (_BODY, len(_BODY) - 1, "size"),
        (b"", 32, "size"),
    ):
        with pytest.raises(RefreshRejected, match=reason):
            validate_refresh_body(
                _TASK,
                RawVersion(sha256(body).hexdigest(), None),
                body,
                current=None,
                max_body_bytes=limit,
            )


def test_bodyless_refresh_requires_a_revised_current_copy() -> None:
    assert (
        validate_refresh_body(_TASK, _VERSION, None, current=_CURRENT, max_body_bytes=32).body
        is None
    )
    no_revision = replace(_VERSION, revision=None)
    with pytest.raises(RefreshRejected, match="requires the current body"):
        validate_refresh_body(
            _TASK,
            no_revision,
            None,
            current=replace(_CURRENT, version=no_revision),
            max_body_bytes=32,
        )


@pytest.mark.parametrize(
    "current",
    [
        None,
        replace(_CURRENT, resource_id="mcp://fixture/other"),
        replace(_CURRENT, scope=replace(_SCOPE, org="other")),
        replace(_CURRENT, scope=replace(_SCOPE, source_generation=9)),
        replace(_CURRENT, scope=replace(_SCOPE, connection_generation=9)),
        replace(_CURRENT, version=replace(_VERSION, revision="upstream-9")),
        replace(_CURRENT, version=replace(_VERSION, sha256="f" * 64)),
    ],
)
def test_bodyless_refresh_cannot_reuse_different_or_stale_copy_evidence(
    current: CurrentRawCopy | None,
) -> None:
    with pytest.raises(RefreshRejected, match="requires the current body"):
        validate_refresh_body(_TASK, _VERSION, None, current=current, max_body_bytes=32)
