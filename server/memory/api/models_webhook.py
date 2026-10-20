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
    """Register a URL that is told when memory in the namespace changes."""

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
    events: list[EventType]
    created_by: str = ""
    created_at: datetime | None = None
    # Whether the replica that answered holds a usable secret in the variable secret_ref
    # names; events wait until the worker's does.
    secret_present: bool = False


class WebhookRegisterOutput(BaseModel):
    # The registration as stored.
    webhook: WebhookInfo


class WebhookListInput(BaseModel):
    """The namespace's memory webhooks."""

    org: str
    namespace: str


class WebhookListOutput(BaseModel):
    # The namespace's live registrations, oldest first.
    webhooks: list[WebhookInfo] = Field(default_factory=list)


class WebhookRemoveInput(BaseModel):
    """Remove one memory webhook; its undelivered events are not sent."""

    org: str
    namespace: str
    # The webhook_id webhook.register or webhook.list returned.
    webhook_id: str = Field(..., min_length=1, max_length=64)


class WebhookRemoveOutput(BaseModel):
    # False when the namespace has no live webhook with that id.
    removed: bool


class WebhookDeliveriesInput(BaseModel):
    """The namespace's webhook events of the last 3 days and their delivery state."""

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
    # (out of attempts, or never sendable: see last_error).
    status: DeliveryStatus
    # POSTs made so far (a wait for a missing secret is not one).
    attempts: int
    # When the next attempt is due (pending), or when the last one was made.
    next_attempt_at: datetime
    # The last HTTP status the receiver answered; 0 when none arrived.
    last_status: int
    # Why the event failed, waits or died when no HTTP status explains it ("" otherwise).
    last_error: str


class WebhookDeliveriesOutput(BaseModel):
    # Events, newest first.
    deliveries: list[WebhookDelivery] = Field(default_factory=list)
