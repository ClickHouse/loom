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


def _download_response(
    data: bytes,
    *,
    filename: str,
    content_type: str,
    inline: bool,
    source: str,
) -> Response:
    """The bytes of a `knowledge.download` answer with its headers.

    `inline=true` lets the /me preview render PDFs in an iframe; the default
    is attachment so plain GETs download rather than surprise-render. The
    file name is reduced to printable ASCII without quotes or backslashes
    so it cannot break out of the Content-Disposition header.
    """
    safe_name = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    disposition = "inline" if inline else "attachment"
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe_name}"',
            "Content-Length": str(len(data)),
            # no-store: the row can be repointed at new bytes.
            "Cache-Control": "no-store",
            "X-Loom-Download-Source": source,
        },
    )


def attach(app: Any) -> None:
    app.include_router(download_router)
