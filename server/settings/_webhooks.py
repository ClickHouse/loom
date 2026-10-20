"""Memory webhooks: their delivery worker and the URLs they may reach."""

from __future__ import annotations

from pydantic import BaseModel, Field


class WebhookSettings(BaseModel):
    # Deliver the signed memory-change events of loom.memory_webhook_outbox to
    # each namespace's registered webhooks (the memory_webhook_deliver worker).
    memory_webhook_deliver_enabled: bool = True
    # Seconds between memory webhook delivery passes while events are going out.
    memory_webhook_deliver_interval_seconds: int = Field(5, ge=1)
    # Seconds between memory webhook delivery passes that found nothing due.
    memory_webhook_deliver_idle_interval_seconds: int = Field(30, ge=1)
    # POSTs per event before it is marked dead (listed by webhook.deliveries). Retries back
    # off to one an hour, so 60 span about 2.3 days. The 2.75-day deadline marks any event
    # still undelivered dead whatever this is, so a lower value only gives up sooner.
    memory_webhook_max_attempts: int = Field(60, ge=1)
    # Failed POSTs in a row after which the delivery worker passes a memory webhook over
    # for a cooldown (kept per process; a 2xx resets it).
    memory_webhook_failures_before_cooldown: int = Field(5, ge=1)
    # Seconds of a memory webhook's first cooldown; each one after it doubles, up to
    # memory_webhook_max_cooldown_seconds.
    memory_webhook_cooldown_seconds: int = Field(60, ge=1)
    # Seconds a memory webhook's cooldown grows to at most.
    memory_webhook_max_cooldown_seconds: int = Field(3600, ge=1)
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
