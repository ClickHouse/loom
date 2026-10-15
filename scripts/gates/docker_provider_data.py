"""Seed/check nonempty rebuild surfaces inside the disposable Docker gate."""

import asyncio
import sys

from server.core.ch import get_client
from server.insights.app.provider_command import model_command
from server.settings import settings


async def seed() -> None:
    from server import boot
    from server.core.background import register_buffer_drainer

    register_buffer_drainer("boot-writes", boot.drain_writes)
    # The served width: a coded vector column takes vectors of that length only.
    width = settings.embedding_dim
    vector = [1.0] + [0.0] * (width - 1)
    async with model_command():
        client = get_client()
        await client.execute(
            "INSERT INTO loom.memory_embeddings "
            "(org, namespace, agent, user_id, session_id, memory_key, memory_type, scope, "
            "content_excerpt, embedding_model, embedding_dim, embedding, memory_key_embedding, "
            "memory_key_embedding_dim, verified, tags) SETTINGS async_insert = 0 VALUES "
            "('dev', 'local', 'docker-smoke', '-', '-', 'docker/vector-smoke', 'semantic', "
            "'private', 'Provider rebuild preserves metadata', {model:String}, {dim:UInt16}, "
            "{vec:Array(BFloat16)}, {vec:Array(BFloat16)}, {dim:UInt16}, 1, ['provider-smoke'])",
            {"model": settings.embedding_model, "dim": width, "vec": vector},
        )
        await client.execute(
            "INSERT INTO loom.code_chunks "
            "(org, namespace, embedding_model, path, chunk_id, content, lexemes, embedding, "
            "embedding_dim, start_line, end_line) SETTINGS async_insert = 0 VALUES "
            "('dev', 'code-provider-smoke', {model:String}, 'example.py', "
            "'ace68a73-808a-4b37-9ae0-2ab1a03b1c53', 'def greet(): return 42', "
            "'greet return', {vec:Array(BFloat16)}, {dim:UInt16}, 1, 1)",
            {"model": settings.embedding_model, "dim": width, "vec": vector},
        )


async def verify(model: str, dimensions: int) -> None:
    client = get_client()
    memory = await client.query_one(
        "SELECT embedding_model, embedding_dim, memory_key_embedding_dim, "
        "length(embedding) AS width, verified, tags, content_excerpt "
        "FROM loom.memory_embeddings FINAL WHERE org='dev' AND namespace='local' "
        "AND memory_key='docker/vector-smoke' AND tombstoned_at IS NULL",
    )
    assert memory is not None
    assert memory["embedding_model"] == model and memory["embedding_dim"] == dimensions, memory
    assert memory["memory_key_embedding_dim"] == dimensions and memory["width"] == dimensions
    assert memory["verified"] == 1 and memory["tags"] == ["provider-smoke"]
    assert memory["content_excerpt"] == "Provider rebuild preserves metadata"
    code = await client.query_one(
        "SELECT embedding_model, embedding_dim, length(embedding) AS width, content, start_line "
        "FROM loom.code_chunks FINAL WHERE org='dev' AND namespace='code-provider-smoke' "
        "AND path='example.py' AND tombstoned_at IS NULL",
    )
    assert code is not None
    assert code["embedding_model"] == model and code["embedding_dim"] == dimensions, code
    assert code["width"] == dimensions and code["content"] == "def greet(): return 42"
    assert code["start_line"] == 1
    copies = await client.query_one(
        "SELECT count() AS n FROM system.tables WHERE database='loom' "
        "AND startsWith(name, '_provider_rebuild_')"
    )
    assert copies is not None and copies["n"] == 0, copies
    print(f"PASS provider data: {model}/{dimensions}, memory + code metadata, no retained copies")


async def main() -> None:
    client = get_client()
    await client.connect()
    try:
        if sys.argv[1] == "seed":
            await seed()
        else:
            await verify(sys.argv[1], int(sys.argv[2]))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
