"""`embedding.embed` and `llm.complete`: a read-replica embeds and completes
through the main Loom's providers.

Only a read-replica's own service token calls them (`REPLICA_SUBJECT_PREFIX`,
minted with the deployment's signing keys, which the replica holds), so the
ops never spend the provider budget on another caller's text.
"""

from __future__ import annotations

from server.auth.context import current_auth
from server.core.errors import AuthInsufficientError, ValidationError
from server.core.read_replica import REPLICA_SUBJECT_PREFIX
from server.embedding.embedder import get_embedder
from server.insights.api.models_embedding import (
    MAX_TEXT_CHARS,
    EmbeddingEmbedInput,
    EmbeddingEmbedOutput,
)
from server.settings import settings


def require_replica_caller(op_name: str) -> None:
    """Refuse every caller but a read-replica's own service token, and refuse
    on a replica: these ops spend the main Loom's provider budget for it."""
    auth = current_auth()
    if settings.auth_actually_required() and (
        auth is None
        or not auth.is_service_account
        or not auth.subject.startswith(REPLICA_SUBJECT_PREFIX)
    ):
        raise AuthInsufficientError(f"{op_name} serves read-replicas only")
    if settings.read_replica:
        raise ValidationError(f"{op_name} is served by the main Loom, not a read-replica")


async def handle_embedding_embed(input: EmbeddingEmbedInput) -> EmbeddingEmbedOutput:
    require_replica_caller("embedding.embed")
    if any(len(t) > MAX_TEXT_CHARS for t in input.texts):
        raise ValidationError(f"each text is at most {MAX_TEXT_CHARS} characters")
    embedder = get_embedder()
    if input.mode == "batch":
        vectors = await embedder.embed_many(list(input.texts))
    else:
        vectors = [await embedder.embed(t) for t in input.texts]
    return EmbeddingEmbedOutput(model=embedder.model_name, dim=embedder.dim, vectors=vectors)
