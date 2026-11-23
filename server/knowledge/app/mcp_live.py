"""Selected MCP live reads for the existing protected live-search response."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from server.auth.jwt import AuthInfo
from server.clients.mcp_source import MCPEvidence, MCPSourceClient, MCPSourceError
from server.core.ch import set_query_tag
from server.knowledge.app import mcp_source_authority as authority
from server.knowledge.app.mcp_read_receipt import require_read_receipt
from server.knowledge.domain.mcp_identity import resource_uri
from server.knowledge.domain.sources import KnowledgeSource


@dataclass(frozen=True, repr=False)
class LiveHit:
    id: str
    title: str
    url: str
    content: str
    resource_identity_verified: bool
    provider: Literal["mcp"] = "mcp"


@dataclass(frozen=True, repr=False)
class RetainedRead:
    resource_id: str
    content_sha256: str
    title: str


@asynccontextmanager
async def _client(src: KnowledgeSource, auth: AuthInfo) -> AsyncIterator[MCPSourceClient]:
    async def validate() -> None:
        current = await authority.validate_live_reader(src, auth)
        if current.source != src:
            raise authority.MCPAuthorityError()

    bound = await authority.validate_live_reader(src, auth)
    async with bound.connection.client(validate) as client:
        yield client


@dataclass(frozen=True, repr=False)
class Contribution:
    source: KnowledgeSource
    auth: AuthInfo
    hits: tuple[LiveHit, ...]
    reads: tuple[RetainedRead, ...]

    async def check(self) -> None:
        async with _client(self.source, self.auth) as client:
            await client.discover()
            for retained in self.reads:
                set_query_tag("search.query.live.check")
                evidence = await client.read(retained.resource_id)
                digest = require_read_receipt(client, retained.resource_id, evidence)
                if (digest, evidence.title) != (retained.content_sha256, retained.title):
                    raise MCPSourceError("denied")


async def search(
    src: KnowledgeSource, auth: AuthInfo, query: str, *, limit: int
) -> Contribution | None:
    """Read only selected IDs from one bounded mapped-search page; never import."""
    if limit < 1:
        return None
    hits, reads = [], []
    async with _client(src, auth) as client:
        await client.discover()
        if client.config.search is None:
            return None
        selected = {r.id for r in authority.source_contract(src).resources}
        page = await client.search(query)
        seen = set()
        for item in page.items:
            set_query_tag("search.query.live.read")
            if item.resource_id not in selected or item.resource_id in seen:
                continue
            seen.add(item.resource_id)
            evidence = await client.read(item.resource_id)
            digest = require_read_receipt(client, item.resource_id, evidence)
            receipt = evidence.receipt
            assert receipt is not None
            uri = resource_uri(src.source_id, item.resource_id)
            hits.append(
                LiveHit(
                    uri, evidence.title[:1024], uri, evidence.text[:200], receipt.identity_verified
                )
            )
            reads.append(RetainedRead(item.resource_id, digest, evidence.title))
            if len(hits) == limit:
                break
    return Contribution(src, auth, tuple(hits), tuple(reads)) if hits else None


def is_live_source(src: KnowledgeSource) -> bool:
    if src.kind != "mcp":
        return False
    try:
        return authority.source_contract(src).mode == "live"
    except authority.MCPAuthorityError:
        return False


async def read_selected(
    src: KnowledgeSource, auth: AuthInfo, resource_id: str
) -> tuple[MCPEvidence, Contribution]:
    """Read one saved live selection; the caller must retain protected publication."""
    async with _client(src, auth) as client:
        await client.discover()
        evidence = await client.read(resource_id)
        digest = require_read_receipt(client, resource_id, evidence)
    proof = Contribution(src, auth, (), (RetainedRead(resource_id, digest, evidence.title),))
    return evidence, proof
