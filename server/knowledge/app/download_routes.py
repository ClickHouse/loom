"""``GET /v1/knowledge.download``: a document's original file, or its stored
text, as raw bytes (not a JSON envelope), so the browser's ``<a download>``
has a plain URL."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from server.auth.authz import check_acted_for, check_authorized
from server.auth.bearer import missing_bearer, verify_bearer_header
from server.auth.context import set_current_auth
from server.auth.gateway import effective_auth, personal_namespace
from server.core.ch import sequential_reads
from server.identity.app.restricted_access import note_restricted_access
from server.knowledge.domain.documents import text_download_filename
from server.knowledge.store import documents as documents_repo
from server.operations.base import Role
from server.settings import settings

download_router = APIRouter()


@download_router.get("/v1/knowledge.download")
async def knowledge_download(
    request: Request,
    org: str,
    namespace: str,
    source_uri: str,
    inline: bool = False,
) -> Response:
    """Stream a document's original uploaded file, or its stored text.

    GET-shaped (not POST/JSON like the ops) so the browser's <a download> has a
    plain URL and gets raw bytes, not a JSON envelope. Auth: bearer + VIEWER on
    (org, namespace) via the same check_authorized the ops use; an admitted
    read of a restricted namespace leaves its audit row, as an op's does.

    A document with no preserved blob (added through
    `knowledge.add`/`upsert` with `content` only) downloads as Markdown built
    from `loom.documents.content` — the same column `knowledge.get` serves.
    `X-Loom-Download-Source: blob|text` says which path answered.
    """
    # The HTTP transport's rule: a present bearer is verified whether or not
    # the deployment requires one; a missing one is a 401 only when auth is on.
    auth = verify_bearer_header(request.headers.get("Authorization"), acts_for_end_users=True)
    if auth is None and settings.auth_actually_required():
        raise missing_bearer()
    try:
        if auth is not None:
            auth = effective_auth(auth, request.headers.get("X-Loom-On-Behalf-Of") or "")
        set_current_auth(auth)  # the reader, as an op's transport binds it (the audit row's token)
        namespace = personal_namespace(auth, org, namespace)
        await check_acted_for(auth, org)  # what the op gate checks before it authorizes
        await check_authorized(auth, Role.VIEWER, org=org, namespace=namespace)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail={"code": "VALIDATION_FAILED", "message": str(e)},
        ) from e
    except PermissionError as e:
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTH_INSUFFICIENT", "message": str(e)},
        ) from e
    await note_restricted_access(
        auth, op="knowledge.download", org=org, namespace=namespace, transport="http"
    )

    doc = await documents_repo.get_one(org, namespace, source_uri)
    if doc is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "NOT_FOUND",
                "message": "no document at this source_uri in the namespace",
            },
        )
    if not doc.blob_path:
        # Text-only document: the stored text is the document, so serve it.
        return _download_response(
            doc.content.encode("utf-8"),
            filename=text_download_filename(doc.title, doc.source_uri),
            content_type="text/markdown",
            inline=inline,
            source="text",
        )
    response = await _original_response(org, namespace, doc, inline=inline)
    if response is None:
        # An upload through another replica may not have reached this one yet.
        with sequential_reads():
            response = await _original_response(org, namespace, doc, inline=inline)
    if response is None:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "BLOB_GONE",
                "message": "the document row references an original that is no longer stored",
            },
        )
    return response


# An original up to this size is read whole (and a ClickHouse connection freed)
# before the answer is sent; a larger one streams, from ClickHouse under
# `ch.STREAM_SLOTS` or from the bucket.
_BUFFER_MAX_BYTES = 4 * 1024 * 1024


async def _original_response(
    org: str, namespace: str, doc: documents_repo.DocumentRow, *, inline: bool
) -> Response | None:
    """The answer carrying `doc`'s original, or None when no row or object holds it whole."""
    from server.knowledge.store import blobs

    sha = doc.blob_path
    stored = await blobs.locate(org, namespace, sha)
    if stored is None:
        return None
    size = stored.size
    filename = doc.original_filename or doc.source_uri or "document.bin"
    content_type = doc.blob_content_type or "application/octet-stream"
    if size <= _BUFFER_MAX_BYTES:
        data = await blobs.read(org, namespace, sha, stored=stored)
        if data is None:  # missing or short of its size
            return None  # the route reads once more, then answers 410
        return _download_response(
            data, filename=filename, content_type=content_type, inline=inline, source="blob"
        )
    return await _blob_response(
        blobs.stream(org, namespace, sha, stored=stored),
        size,
        filename=filename,
        content_type=content_type,
        inline=inline,
    )


# Types a browser renders without running script. Anything else (HTML, SVG,
# XML, PDF, unknown) is never served inline: an uploaded file is attacker-chosen
# bytes with an attacker-chosen label, on the dashboard's own origin.
_TEXT_TYPES = frozenset({"text/plain", "text/markdown", "text/csv", "application/json"})
# A binary type is previewed only when the bytes start the way that format
# does, so HTML labelled image/png is not rendered as anything.
_BINARY_MAGIC = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),
}
# Opening the URL on its own (not through the dashboard) still runs no script.
_DOWNLOAD_CSP = "sandbox; default-src 'none'; frame-ancestors 'none'"


def _base_type(content_type: str) -> str:
    return content_type.split(";")[0].strip().lower()


def preview_type(content_type: str, data: bytes) -> str | None:
    """The Content-Type an inline preview is served with, or None when the
    file must download instead. Text kinds are all served as plain text."""
    base = _base_type(content_type)
    if base in _TEXT_TYPES:
        return "text/plain; charset=utf-8"
    magic = _BINARY_MAGIC.get(base)
    if magic and data.startswith(magic):
        if base == "image/webp" and data[8:12] != b"WEBP":
            return None
        return base
    return None


def _headers(
    head: bytes, size: int, *, filename: str, content_type: str, inline: bool, source: str
) -> tuple[str, dict[str, str]]:
    """The media type and headers of a `knowledge.download` answer whose body
    starts with `head`.

    `inline=true` previews only the passive types `preview_type` allows;
    anything else is an attachment of `application/octet-stream`. A plain
    download keeps its type only when it is one of those passive types. The
    file name is reduced to printable ASCII without quotes or backslashes
    so it cannot break out of the Content-Disposition header.
    """
    safe_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    passive = preview_type(content_type, head)
    if inline and passive:
        disposition, media_type = "inline", passive
    elif passive:
        disposition, media_type = "attachment", _base_type(content_type)
    else:
        disposition, media_type = "attachment", "application/octet-stream"
    return media_type, {
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
        "Content-Length": str(size),
        # no-store: the row can be repointed at new bytes.
        "Cache-Control": "no-store",
        "X-Loom-Download-Source": source,
        # Pinned here too: a download is never sniffed into another type, whatever the
        # middleware sends.
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _DOWNLOAD_CSP,
    }


def _download_response(
    data: bytes, *, filename: str, content_type: str, inline: bool, source: str
) -> Response:
    """A buffered answer: a small original, or the stored text of a document
    without one."""
    media_type, headers = _headers(
        data,
        len(data),
        filename=filename,
        content_type=content_type,
        inline=inline,
        source=source,
    )
    return Response(content=data, media_type=media_type, headers=headers)


# Bytes read before the headers are chosen: enough for every magic in _BINARY_MAGIC.
_HEAD_BYTES = 12


async def _blob_response(
    opened: AbstractAsyncContextManager[AsyncIterator[bytes]],
    size: int,
    *,
    filename: str,
    content_type: str,
    inline: bool,
) -> StreamingResponse | None:
    """Stream an original from ClickHouse or the bucket without holding it in
    memory. The first chunks are read before the answer starts, so a failed
    read is a plain error, a row or object deleted since its size was read is
    None, and the preview type sees the file's opening bytes; the stream closes
    when the body is sent or the client goes away."""
    stack = AsyncExitStack()
    try:
        chunks = await stack.enter_async_context(opened)
        head = b""
        async for chunk in chunks:
            head += chunk
            if len(head) >= _HEAD_BYTES:
                break
    except BaseException:
        await stack.aclose()
        raise
    if not head:
        await stack.aclose()
        return None

    async def body() -> AsyncIterator[bytes]:
        async with stack:
            if head:
                yield head
            async for chunk in chunks:
                yield chunk

    media_type, headers = _headers(
        head, size, filename=filename, content_type=content_type, inline=inline, source="blob"
    )
    return StreamingResponse(body(), media_type=media_type, headers=headers)


def attach(app: Any) -> None:
    app.include_router(download_router)
