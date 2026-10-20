"""The `mentor` operations the registry serves; wire models are in models_mentor.py."""

from __future__ import annotations

from server.insights.api.models_mentor import (
    MentorBriefInput,
    MentorBriefOutput,
)
from server.insights.app.mentor_brief import handle_mentor_brief
from server.operations.base import Operation, Role

mentor_brief = Operation(
    name="mentor.brief",
    input_model=MentorBriefInput,
    output_model=MentorBriefOutput,
    handler=handle_mentor_brief,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
)

OPERATIONS: list[Operation] = [mentor_brief]
