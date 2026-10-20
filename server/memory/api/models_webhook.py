"""webhook.register / list / remove / deliveries — wire models.

A namespace admin registers a URL that is told when memory in the namespace
changes. Each event carries ids and metadata, never content; the receiver reads
the memory back with its own token.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.memory.domain.webhook_events import EVENT_TYPES, EventType

DeliveryStatus = Literal["pending", "delivered", "dead"]


class WebhookRegisterInput(BaseModel):
    org: str
    namespace: str
    # Where events are POSTed: an https URL (http only where the deployment allows it)
    # whose host resolves to public addresses. Registering the same URL again
    # replaces its secret_ref and events.
    url: str = Field(..., min_length=1, max_length=2048)
    # Name of the environment variable holding the signing secret (never the secret):
    # `LOOM_WEBHOOK_SECRET_<ORG>__<NAME>`, ORG the org id upper-cased with every byte
    # other than a-z and 0-9 as `_` plus two hex digits.
    secret_ref: str = Field(..., min_length=1, max_length=256)
    # The event types to send; default every one.
    events: list[EventType] = Field(default_factory=lambda: list(EVENT_TYPES), min_length=1)


class WebhookInfo(BaseModel):
    webhook_id: str
    url: str
    secret_ref: str
    events: list[str]
    created_by: str = ""
    created_at: datetime | None = None
    # Whether this server holds the variable secret_ref names; events wait until it does.
    secret_present: bool = False


class WebhookRegisterOutput(BaseModel):
    webhook: WebhookInfo


class WebhookListInput(BaseModel):
    org: str
    namespace: str


class WebhookListOutput(BaseModel):
    webhooks: list[WebhookInfo] = Field(default_factory=list)


class WebhookRemoveInput(BaseModel):
    org: str
    namespace: str
    # The webhook_id webhook.register or webhook.list returned.
    webhook_id: str = Field(..., min_length=1, max_length=64)


class WebhookRemoveOutput(BaseModel):
    # False when the namespace has no live webhook with that id.
    removed: bool


class WebhookDeliveriesInput(BaseModel):
    org: str
    namespace: str
    # Only this webhook's events; "" = every webhook of the namespace.
    webhook_id: str = Field(default="", max_length=64)
    # Only events in this state; None = every state.
    status: DeliveryStatus | None = None
    # At most this many events, newest first, 1-500.
    limit: int = Field(default=100, ge=1, le=500)


class WebhookDelivery(BaseModel):
    event_id: str
    webhook_id: str
    event_type: str
    memory_key: str
    memory_id: str
    version: int
    occurred_at: datetime
    # pending (waiting for its next try), delivered (a 2xx came back) or dead
    # (out of attempts).
    status: str
    attempts: int
    next_try_at: datetime
    # The last HTTP status the receiver answered; 0 when none arrived.
    last_status: int
    # Why the last attempt failed when no status arrived ("" otherwise).
    last_error: str


class WebhookDeliveriesOutput(BaseModel):
    deliveries: list[WebhookDelivery] = Field(default_factory=list)
