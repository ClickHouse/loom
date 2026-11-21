"""An admitted crawl proves all fixed read grants and each distinct write denial."""

from __future__ import annotations

import asyncio

from server.clients import gdrive
from server.clients import google_workspace as google
from server.core.ch import set_query_tag
from server.knowledge.app.crawl_expansion import Standing


async def require_read_only(
    account: gdrive.ServiceAccount,
    *,
    subject: str,
    standing: Standing,
    deadline: float,
    max_bytes: int,
) -> None:
    set_query_tag("knowledge.crawl.delegation")
    async with asyncio.timeout_at(deadline):
        for read in gdrive.ReadScope:
            await standing.validate()
            await google.delegated_token(
                account, subject=subject, scope=read, deadline=deadline, max_bytes=max_bytes
            )
            await standing.validate()
        for write in gdrive.WriteScope:
            await standing.validate()
            await google.require_write_denied(
                account, subject=subject, scope=write, deadline=deadline, max_bytes=max_bytes
            )
            await standing.validate()
