"""The `embedding` operation the registry serves; wire models are in models_embedding.py."""

from __future__ import annotations

from server.insights.api.models_embedding import EmbeddingEmbedInput, EmbeddingEmbedOutput
from server.insights.app.embedding_serve import handle_embedding_embed
from server.operations.base import Operation, Role

embedding_embed = Operation(
    name="embedding.embed",
    maturity="experimental",
    input_model=EmbeddingEmbedInput,
    output_model=EmbeddingEmbedOutput,
    handler=handle_embedding_embed,
    idempotent=True,
    budget_p99_ms=3000,
    required_role=Role.VIEWER,
    audit=False,
    skip_namespace_authz=True,
)

OPERATIONS = [embedding_embed]
