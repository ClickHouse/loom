"""Operations over the wire models in `server.memory.api.models_webhook`."""

from __future__ import annotations

from server.memory.api.models_webhook import (
    WebhookDeliveriesInput,
    WebhookDeliveriesOutput,
    WebhookListInput,
    WebhookListOutput,
    WebhookRegisterInput,
    WebhookRegisterOutput,
    WebhookRemoveInput,
    WebhookRemoveOutput,
)
from server.memory.app.webhooks import (
    handle_webhook_deliveries,
    handle_webhook_list,
    handle_webhook_register,
    handle_webhook_remove,
    register_audit_subject,
)
from server.operations.base import Operation, Role

# A namespace admin decides where the namespace's change events go: audited
# like inbox.register and grants.create. Registering the same URL again
# replaces it, so a retry is safe.
webhook_register = Operation(
    name="webhook.register",
    maturity="experimental",
    input_model=WebhookRegisterInput,
    output_model=WebhookRegisterOutput,
    handler=handle_webhook_register,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
    audit_subject=register_audit_subject,
)

# ADMIN: the list names the URLs and secret variables of the namespace.
webhook_list = Operation(
    name="webhook.list",
    maturity="experimental",
    input_model=WebhookListInput,
    output_model=WebhookListOutput,
    handler=handle_webhook_list,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=False,
)

webhook_remove = Operation(
    name="webhook.remove",
    maturity="experimental",
    input_model=WebhookRemoveInput,
    output_model=WebhookRemoveOutput,
    handler=handle_webhook_remove,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=True,
)

# The outbox as the namespace's admin sees it: pending, delivered and dead events.
webhook_deliveries = Operation(
    name="webhook.deliveries",
    maturity="experimental",
    input_model=WebhookDeliveriesInput,
    output_model=WebhookDeliveriesOutput,
    handler=handle_webhook_deliveries,
    idempotent=True,
    required_role=Role.ADMIN,
    audit=False,
)

OPERATIONS: list[Operation] = [webhook_register, webhook_list, webhook_remove, webhook_deliveries]
