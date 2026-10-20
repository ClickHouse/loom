"""Memory webhooks: their delivery worker and the URLs they may reach."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WebhookSettings(BaseModel):
    # Deliver the signed memory-change events of loom.memory_webhook_outbox to
    # each namespace's registered webhooks (the memory_webhook_deliver worker).
    memory_webhook_deliver_enabled: bool = True
    # Seconds between passes while events are going out.
    memory_webhook_deliver_interval_seconds: int = 5
    # Seconds between passes that found nothing due.
    memory_webhook_deliver_idle_interval_seconds: int = 30
    # POSTs per event before it is marked dead (listed by webhook.deliveries). Retries back
    # off to one an hour, so 60 span about 2.3 days of the outbox's 3-day keep; 70 is the most
    # that fit inside it.
    memory_webhook_max_attempts: int = Field(60, ge=1, le=70)
    # Seconds between each process's reads of the registered webhooks; a webhook
    # registered or removed on another process applies to writes here within it.
    memory_webhook_refresh_seconds: float = Field(10.0, gt=0)
    # True lets a memory webhook use plain http (a local receiver in development);
    # false: https only.
    memory_webhook_allow_http: bool = False
    # Comma-separated hostnames or IP literals (exact, any case) a memory webhook may
    # reach although they resolve to a private, loopback or link-local address; "" = none.
    memory_webhook_private_hosts: str = ""

    def memory_webhook_private_host_set(self) -> frozenset[str]:
        return frozenset(
            h.strip().lower() for h in self.memory_webhook_private_hosts.split(",") if h.strip()
        )
