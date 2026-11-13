"""The `knowledge.gaps.*` operation declarations; their wire models are in
`models_gaps`."""

from __future__ import annotations

from server.knowledge.api.models_gaps import KnowledgeGapsListInput, KnowledgeGapsListOutput
from server.knowledge.app.gaps import handle_knowledge_gaps_list
from server.operations.base import Operation, Role

# Admin: a gap's samples are other people's questions.
knowledge_gaps_list = Operation(
    name="knowledge.gaps.list",
    maturity="experimental",
    input_model=KnowledgeGapsListInput,
    output_model=KnowledgeGapsListOutput,
    handler=handle_knowledge_gaps_list,
    idempotent=True,
    required_role=Role.ADMIN,
)

OPERATIONS = [knowledge_gaps_list]
