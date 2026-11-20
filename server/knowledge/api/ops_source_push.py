"""Namespace-admin setup for authenticated source sync notifications."""

from server.knowledge.api.models_source_push import (
    PushConfigureInput,
    PushConfirmInput,
    PushStatusInput,
    PushStatusOutput,
)
from server.knowledge.app.source_push import (
    audit_subject,
    handle_configure,
    handle_confirm,
    handle_status,
)
from server.operations.base import Operation, Role

OPERATIONS = [
    Operation(
        name="knowledge.source.push_configure",
        maturity="experimental",
        input_model=PushConfigureInput,
        output_model=PushStatusOutput,
        handler=handle_configure,
        idempotent=False,
        required_role=Role.ADMIN,
        audit=True,
        audit_subject=audit_subject,
    ),
    Operation(
        name="knowledge.source.push_status",
        maturity="experimental",
        input_model=PushStatusInput,
        output_model=PushStatusOutput,
        handler=handle_status,
        # A pending challenge must never enter the ordinary idempotency cache.
        idempotent=False,
        required_role=Role.ADMIN,
    ),
    Operation(
        name="knowledge.source.push_confirm",
        maturity="experimental",
        input_model=PushConfirmInput,
        output_model=PushStatusOutput,
        handler=handle_confirm,
        idempotent=False,
        required_role=Role.ADMIN,
        audit=True,
        audit_subject=audit_subject,
    ),
]
