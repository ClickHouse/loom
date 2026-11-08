"""``POST /v1/knowledge.upload``: a file's raw bytes, verified and stored, and
the scoped `upload_id` that `knowledge.ingest` names.

The body is the file itself (no JSON, no base64), read in chunks up to
`knowledge_upload_max_bytes`; a larger body is refused before anything is
stored. When the caller sends `sha256`, the bytes must hash to it. The format
comes from the file name and media type (`format_of`), never from reading the
bytes: a legacy Office name is refused here, and content that is another format
than its name is refused by the extractor when the job runs (`format_mismatch`).
The original is stored as any other (`loom.document_blobs`, or the bucket) and
the upload row holds it from the originals sweep until it expires.

The route bounds its own body (the request-body cap passes it through) and is
admitted as an op is: after authorization it draws `knowledge.add`'s rate-limit
buckets, registers the namespace as `knowledge.ingest`'s transport would
(`maybe_autoregister`), and holds one of the org's in-flight slots while it reads and
stores.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from server.auth.authz import check_acted_for, check_authorized
from server.auth.bearer import missing_bearer, verify_bearer_header
from server.auth.context import set_current_auth
from server.auth.gateway import effective_auth, personal_namespace
from server.core import tenant_slots
from server.core.document_formats import format_of
from server.identity.app.namespace_autoregister import maybe_autoregister
from server.identity.app.restricted_access import note_restricted_access
from server.operations.base import Role
from server.settings import settings

upload_router = APIRouter()


def _refuse(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def _read_body(request: Request, limit: int) -> bytes:
    """The request body, refused with 413 as soon as it passes `limit`, and
    with 400 when it ends before the `Content-Length` it declared."""
    header = request.headers.get("content-length") or ""
    declared = int(header) if header.isdigit() else None
    if declared is not None and declared > limit:
        raise _refuse(413, "PAYLOAD_TOO_LARGE", f"the file is over {limit} bytes")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _refuse(413, "PAYLOAD_TOO_LARGE", f"the file is over {limit} bytes")
    if declared is not None and len(body) != declared:
        raise _refuse(400, "VALIDATION_FAILED", "the body ended before its Content-Length")
    # One copy: the ClickHouse client writes only `bytes` into a String column.
    # The org's in-flight slot bounds how many uploads hold one at a time.
    return bytes(body)


def _bucket_org(auth: Any, org: str) -> str:
    """The org a call draws its limits from: the verified token's, else the
    request's (a wildcard-org service account), as for an op."""
    token_org = getattr(auth, "org", None)
    return str(token_org) if token_org and token_org != "*" else org


def _admit(auth: Any, org: str, namespace: str) -> None:
    """`knowledge.add`'s rate-limit buckets, as the transports draw them for an op, as
    interactive traffic (an upload is not classified)."""
    from server.core.ratelimit import admit_route

    admit_route(
        "knowledge.add",
        namespace,
        org=_bucket_org(auth, org),
        actor=getattr(auth, "subject", None),
        label="knowledge.upload",
    )


@upload_router.post("/v1/knowledge.upload")
async def knowledge_upload(
    request: Request,
    org: str,
    namespace: str,
    filename: str,
    content_type: str = "",
    sha256: str = "",
) -> Any:
    """Store one file for `knowledge.ingest`. Auth: bearer + WRITER on (org,
    namespace), as the ops check it; an admitted upload to a restricted
    namespace leaves the audit row an op leaves. Answers the upload's id,
    sha256, size, detected format and expiry."""
    auth = verify_bearer_header(request.headers.get("Authorization"), acts_for_end_users=True)
    if auth is None and settings.auth_actually_required():
        raise missing_bearer()
    name = filename.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        raise _refuse(400, "VALIDATION_FAILED", "filename is required")
    # Named from the file name and media type, never by reading the bytes: the
    # server parses no original. One whose content is another format is refused
    # by the extractor at ingest (`format_mismatch`).
    try:
        fmt = format_of(name, content_type)
    except ValueError as e:
        raise _refuse(415, "UNSUPPORTED_FILE", str(e)) from e
    try:
        if auth is not None:
            auth = effective_auth(auth, request.headers.get("X-Loom-On-Behalf-Of") or "")
        set_current_auth(auth)  # the upload's author, as an op's transport binds it
        namespace = personal_namespace(auth, org, namespace)
        await check_acted_for(auth, org)  # what the op gate checks before it authorizes
        await check_authorized(auth, Role.WRITER, org=org, namespace=namespace)
    except ValueError as e:
        raise _refuse(400, "VALIDATION_FAILED", str(e)) from e
    except PermissionError as e:
        raise _refuse(403, "AUTH_INSUFFICIENT", str(e)) from e
    await note_restricted_access(
        auth, op="knowledge.upload", org=org, namespace=namespace, transport="http"
    )
    _admit(auth, org, namespace)
    # Register before the upload's own rows can make a fresh name read as in use.
    from server.knowledge.api.ops_knowledge import knowledge_ingest

    await maybe_autoregister(knowledge_ingest, org=org, namespace=namespace, auth=auth)
    with tenant_slots.hold(_bucket_org(auth, org)):
        return await _store(request, org, namespace, name, fmt, content_type, sha256)


async def _store(
    request: Request,
    org: str,
    namespace: str,
    name: str,
    fmt: str,
    content_type: str,
    sha256: str,
) -> JSONResponse:
    """Read, check and store the file; answers the upload."""
    from server.knowledge.app.writes import authorship_of
    from server.knowledge.store import blobs, ingestions

    data = await _read_body(request, settings.knowledge_upload_max_bytes)
    if not data:
        raise _refuse(400, "VALIDATION_FAILED", "the file is empty")
    digest = hashlib.sha256(data).hexdigest()
    if sha256 and sha256.lower() != digest:
        raise _refuse(422, "CHECKSUM_MISMATCH", "the bytes do not hash to the sha256 sent")
    author = authorship_of(agent="", session_id="", source_class=None)
    await blobs.put(org, namespace, data, uploader=author["user_id"])
    row = ingestions.upload_row(
        org=org,
        namespace=namespace,
        sha256=digest,
        size=len(data),
        filename=name,
        content_type=content_type.strip(),
        fmt=fmt,
        author=author,
        ttl_s=settings.knowledge_upload_ttl_seconds,
    )
    await ingestions.insert_upload(row)
    return JSONResponse(
        {
            "upload_id": row["upload_id"],
            "namespace": namespace,
            "sha256": digest,
            "size": len(data),
            "filename": name,
            "format": fmt,
            "expires_at": row["expires_at"].isoformat() + "Z",
        },
        status_code=201,
    )


def attach(app: Any) -> None:
    app.include_router(upload_router)
