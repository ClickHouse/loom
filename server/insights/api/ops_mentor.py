"""The `mentor` operations the registry serves; wire models are in models_mentor.py."""

from __future__ import annotations

from server.insights.api.models_mentor import (
    MentorBriefInput,
    MentorBriefOutput,
    MentorGuideInput,
    MentorGuideOutput,
)
from server.insights.app.mentor_brief import handle_mentor_brief
from server.insights.app.mentor_guide import handle_mentor_guide
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

mentor_guide = Operation(
    name="mentor.guide",
    maturity="experimental",
    input_model=MentorGuideInput,
    output_model=MentorGuideOutput,
    handler=handle_mentor_guide,
    idempotent=True,
    required_role=Role.VIEWER,
    audit=False,
    skip_namespace_authz=True,
    deployment_scoped=True,
)

OPERATIONS: list[Operation] = [mentor_brief, mentor_guide]
