"""Present the result of the protected read without a stored fallback."""

import asyncio

from server.core.diagnostics import private_work
from server.knowledge.api.models_source_live import (
    KnowledgeSourceLiveDocument,
    KnowledgeSourceLiveInput,
    KnowledgeSourceLiveOutput,
)
from server.knowledge.app.mcp_worker_authority import RequestedResource
from server.knowledge.app.mcp_worker_protected import LiveSourceDirectory, protected_live_read
from server.knowledge.app.mcp_worker_read import WorkerReadRefused
from server.knowledge.app.mcp_worker_runtime import current_directory

_LIVE_DEADLINE_SECONDS = 8


async def read_source_live(
    input: KnowledgeSourceLiveInput, directory: LiveSourceDirectory
) -> KnowledgeSourceLiveOutput:
    with private_work():
        try:
            target = RequestedResource(input.org, input.namespace, input.source, input.resource_id)
            deadline = asyncio.get_running_loop().time() + _LIVE_DEADLINE_SECONDS
            document = await protected_live_read(target, directory, deadline=deadline)
        except WorkerReadRefused as error:
            return KnowledgeSourceLiveOutput(status=error.reason)
        return KnowledgeSourceLiveOutput(
            status="ok",
            document=KnowledgeSourceLiveDocument(
                source_id=document.source_id,
                resource_id=document.resource_id,
                uri=document.uri,
                source_link=document.source_link,
                fetched_at=document.fetched_at,
                title=document.title,
                text=document.text,
                mime_type=document.mime_type,
            ),
        )


async def handle_knowledge_source_live(
    input: KnowledgeSourceLiveInput,
) -> KnowledgeSourceLiveOutput:
    directory = current_directory()
    if directory is None:
        return KnowledgeSourceLiveOutput(status="unavailable")
    return await read_source_live(input, directory)
