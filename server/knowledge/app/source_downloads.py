"""A checked download owns its stream and renews admission before emitting bytes."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from server.auth.context import current_auth
from server.core import request_work
from server.core.ch import sequential_reads, set_query_tag
from server.core.diagnostics import private_work
from server.knowledge.app import source_documents, source_publication
from server.knowledge.app.download_routes import _HEAD_BYTES, _headers
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_documents import CheckedDocument
from server.knowledge.domain.documents import text_download_filename
from server.knowledge.store import blobs
from server.observability import cost_accumulator
from server.observability.ledger import emit_agent_event, required_call_exit


@contextmanager
def download_request(org: str, namespace: str) -> Iterator[None]:
    """The raw HTTP read owns the same required marked ledger attempt as an op."""
    from server.identity.domain.read_scope import request_read_filters
    from server.observability.consumer import bind_current_consumer
    from server.observability.traffic_class import bind_claimed_class

    auth = current_auth()
    scope = request_read_filters(auth)
    bag = request_work.begin()
    bag.admitted = (org, namespace)
    cost_accumulator.begin_op()
    bind_current_consumer({}, auth)
    bind_claimed_class(auth)
    metadata: dict[str, Any] = {
        "op_name": "knowledge.download",
        "identity": {
            "org": org,
            "namespace": namespace,
            "agent": "-",
            "user_id": scope.user_id or "-",
            "session_id": scope.session_id or "-",
        },
        "request_id": str(uuid4()),
        "impersonated_by": auth.impersonated_by if auth is not None else "",
        "transport": "http",
    }
    began = time.perf_counter()
    with required_call_exit(**metadata, run_id=""):
        yield
        if request_work.mirror_marked():
            emit_agent_event(
                **metadata,
                latency_ms=int((time.perf_counter() - began) * 1000),
                success=True,
                error_code="",
            )


class _Unavailable(Exception):
    pass


class _Gone(Exception):
    pass


@dataclass
class _Opened:
    head: bytes
    size: int
    filename: str
    content_type: str
    source: str
    body: AsyncIterator[bytes]


async def _prefixed(
    head: bytes, stream: AsyncIterator[bytes] | None = None
) -> AsyncIterator[bytes]:
    yield head
    if stream is not None:
        async for chunk in stream:
            yield chunk


class CheckedDownload(StreamingResponse):
    def __init__(
        self, plan: ReadPlan, document: CheckedDocument, public_uri: str, *, inline: bool
    ) -> None:
        super().__init__(())
        self.plan = plan
        self.document = document
        self.public_uri = public_uri
        self.inline = inline
        self._began = False
        self._publication = source_publication.retain_document(plan, document)

    async def _require_current(self) -> None:
        refreshed = await source_documents.refresh(self.plan, self.document)
        if refreshed is None:
            raise _Unavailable
        self.document = refreshed

    async def _open(self, stack: AsyncExitStack) -> _Opened:
        await self._require_current()
        doc = self.document.row
        if not doc.blob_path:
            data = doc.content.encode("utf-8")
            return _Opened(
                data,
                len(data),
                text_download_filename(doc.title, self.public_uri),
                "text/markdown",
                "text",
                _prefixed(data),
            )
        stored = await blobs.locate(self.plan.org, doc.namespace, doc.blob_path)
        await self._require_current()
        if stored is None:
            raise _Gone
        stream = await stack.enter_async_context(
            blobs.stream(self.plan.org, doc.namespace, doc.blob_path, stored=stored)
        )
        head = b""
        async for chunk in stream:
            head += chunk
            if len(head) >= _HEAD_BYTES:
                break
        if not head and stored.size:
            await self._require_current()
            raise _Gone
        return _Opened(
            head,
            stored.size,
            doc.original_filename or self.public_uri,
            doc.blob_content_type or "application/octet-stream",
            "blob",
            _prefixed(head, stream),
        )

    async def stream_response(self, send: Send) -> None:
        # StreamingResponse normally sends headers before entering its body iterator.
        await self._require_current()
        self._began = True
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        async for chunk in self.body_iterator:
            # Sending headers or a previous chunk may have waited on a slow reader.
            await self._require_current()
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        set_query_tag("knowledge.mirror.download")
        with private_work(), sequential_reads():
            # Own the context even if the body never starts or the client disconnects.
            try:
                async with AsyncExitStack() as stack:
                    opened = await self._open(stack)
                    self.media_type, headers = _headers(
                        opened.head,
                        opened.size,
                        filename=opened.filename,
                        content_type=opened.content_type,
                        inline=self.inline,
                        source=opened.source,
                    )
                    self.init_headers(headers)
                    self.body_iterator = opened.body
                    await super().__call__(scope, receive, send)
            except ClientDisconnect:
                raise ClientDisconnect from None
            except _Gone:
                raise HTTPException(
                    status_code=410,
                    detail={
                        "code": "BLOB_GONE",
                        "message": (
                            "the document row references an original that is no longer stored"
                        ),
                    },
                ) from None
            except Exception as exc:  # noqa: BLE001 — conditional download failures disclose no private backend detail
                if not isinstance(exc, _Unavailable):
                    self.plan.failed = True
                if self._began:
                    raise RuntimeError("document read unavailable") from None
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "NOT_FOUND",
                        "message": "no document at this source_uri in the namespace",
                    },
                ) from None
            finally:
                if not self._began:
                    source_publication.discard(self._publication)
