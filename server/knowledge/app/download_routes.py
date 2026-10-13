"""``GET /v1/knowledge.download``: a document's original file, or its stored
text, as raw bytes (not a JSON envelope), so the browser's ``<a download>``
has a plain URL."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from server.auth.authz import check_authorized
from server.auth.bearer import verify_bearer_header
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
    (org, namespace) via the same check_authorized the ops use.

    A document with no preserved blob (added through
    `knowledge.add`/`upsert` with `content` only) downloads as Markdown built
    from `loom.documents.content` — the same column `knowledge.get` serves.
    `X-Loom-Download-Source: blob|text` says which path answered.
    """
    from server.storage.blob_store import get_store

    # The HTTP transport's rule: a present bearer is verified whether or not
    # the deployment requires one; a missing one is a 401 only when auth is on.
    auth = verify_bearer_header(request.headers.get("Authorization"))
    if auth is None and settings.auth_actually_required():
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_INVALID", "message": "missing bearer token"},
        )
    try:
        await check_authorized(auth, Role.VIEWER, org=org, namespace=namespace)
    except PermissionError as e:
        raise HTTPException(
            status_code=403,
            detail={"code": "AUTH_INSUFFICIENT", "message": str(e)},
        ) from e

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
    try:
        data = await asyncio.to_thread(get_store().read, doc.blob_path)
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "BLOB_GONE",
                "message": (
                    "the original file is no longer in the blob store; the "
                    "document row references it but the bytes were removed "
                    "out-of-band"
                ),
            },
        ) from e
    return _download_response(
        data,
        filename=doc.original_filename or doc.source_uri or "document.bin",
        content_type=doc.blob_content_type or "application/octet-stream",
        inline=inline,
        source="blob",
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
_DOWNLOAD_CSP = "sandbox; default-src 'none'"


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


def _download_response(
    data: bytes,
    *,
    filename: str,
    content_type: str,
    inline: bool,
    source: str,
) -> Response:
    """The bytes of a `knowledge.download` answer with its headers.

    `inline=true` previews only the passive types `preview_type` allows;
    anything else is an attachment of `application/octet-stream`. A plain
    download keeps its type only when it is one of those passive types. The
    file name is reduced to printable ASCII without quotes or backslashes
    so it cannot break out of the Content-Disposition header.
    """
    safe_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    passive = preview_type(content_type, data)
    if inline and passive:
        disposition, media_type = "inline", passive
    elif passive:
        disposition, media_type = "attachment", _base_type(content_type)
    else:
        disposition, media_type = "attachment", "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "Content-Length": str(len(data)),
            # no-store: the row can be repointed at new bytes.
            "Cache-Control": "no-store",
            "X-Loom-Download-Source": source,
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": _DOWNLOAD_CSP,
        },
    )


def attach(app: Any) -> None:
    app.include_router(download_router)
