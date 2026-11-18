"""Public provider ingress, authenticated by provider proof rather than a Loom token."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response

from server.auth import tenancy
from server.core.ch import set_query_tag
from server.knowledge.app.source_push import receive
from server.knowledge.domain.source_push import MAX_BODY_BYTES, DeliveryRefused

router = APIRouter()
CALLBACK_TIMEOUT_S = 4.0  # not a setting: acknowledge inside the provider's five-second limit


@router.post("/hooks/knowledge/{namespace}/{source_id}/{generation}")
async def notification(
    request: Request, org: str, namespace: str, source_id: UUID, generation: UUID
) -> Response:
    set_query_tag("knowledge.source.push")
    try:
        async with asyncio.timeout(CALLBACK_TIMEOUT_S):
            # Public callbacks may use an existing placement, never create one.
            if not await tenancy.is_held(org):
                return Response(status_code=403)
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > MAX_BODY_BYTES:
                    return Response(status_code=413)
                raw.extend(chunk)
            await receive(
                org, namespace, str(source_id), str(generation), bytes(raw), dict(request.headers)
            )
    except DeliveryRefused:
        return Response(status_code=403)
    return Response(status_code=200)


def attach(app: Any) -> None:
    app.include_router(router)
