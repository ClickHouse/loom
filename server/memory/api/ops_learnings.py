"""Operations over the wire models in `server.memory.api.models_learnings`."""

from __future__ import annotations

from server.memory.api.models_learnings import (
    LearningsListInput,
    LearningsListOutput,
    LearningsQualityInput,
    LearningsQualityOutput,
    LearningsReviewInput,
    LearningsReviewOutput,
)
from server.memory.app.learnings import (
    handle_learnings_list,
    handle_learnings_quality,
    handle_learnings_review,
)
from server.operations.base import Operation, Role

learnings_list = Operation(
    name="learnings.list",
    maturity="experimental",
    input_model=LearningsListInput,
    output_model=LearningsListOutput,
    handler=handle_learnings_list,
    idempotent=True,
    budget_p99_ms=80,
    required_role=Role.VIEWER,
    audit=False,
)

learnings_review = Operation(
    name="learnings.review",
    maturity="experimental",
    input_model=LearningsReviewInput,
    output_model=LearningsReviewOutput,
    handler=handle_learnings_review,
    idempotent=True,  # second 'promote' on already-promoted is a no-op
    budget_p99_ms=150,
    required_role=Role.ADMIN,
    audit=True,
)


learnings_quality = Operation(
    name="learnings.quality",
    maturity="experimental",
    input_model=LearningsQualityInput,
    output_model=LearningsQualityOutput,
    handler=handle_learnings_quality,
    idempotent=True,
    budget_p99_ms=120,
    required_role=Role.VIEWER,
    audit=False,
)


OPERATIONS: list[Operation] = [learnings_list, learnings_review, learnings_quality]
