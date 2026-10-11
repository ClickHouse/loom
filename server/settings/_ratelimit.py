"""Rate limits: per-actor and per-traffic-class buckets, shared across web workers."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from server.settings._env import JsonObject


class RateLimitSettings(BaseModel):
    # Per-actor rate-limit bucket, keyed (namespace, token subject), as a
    # fraction of the namespace limit. 1.0 = no actor bucket; 0.6 leaves a
    # second credential budget while one sweeps.
    ratelimit_actor_fraction: float = 1.0
    # Share rate buckets across one server's web workers via a memory-mapped
    # file, so a keep-alive client pinned to one worker sees the full cap.
    # auto = when web_workers > 1; on = always; off = per-process buckets
    # divided by replicas × workers.
    ratelimit_shared: Literal["auto", "on", "off"] = "auto"
    # Bucket file; "" = <runtime dir>/loom-ratelimit-<parent pid>.buckets
    # ($XDG_RUNTIME_DIR/loom, else /run/loom, else /tmp). The file is 0600 and
    # never opened through a symlink. Set it for two servers sharing one cap.
    ratelimit_shared_path: str = ""
    # Table slots (32 bytes each). Every process of a server must agree; a
    # mismatch falls back to per-process buckets and shows in /v1/health.
    ratelimit_shared_slots: int = Field(default=16384, ge=1)
    # JSON {class: fraction}: a traffic class carved into its own bucket of the
    # base cap × fraction, so a bench sweep never spends production's budget.
    # Class = token `traffic_class` claim, else namespace row, else request
    # hint, else interactive. Absent class = 1.0 (base bucket).
    ratelimit_class_fractions: Annotated[str, JsonObject] = '{"bench": 0.5}'
    # Requests one org may have in flight at once, deployment-wide. Each
    # process holds its share (divided by replicas × web workers, at least 1);
    # a call over it gets 429 RATE_LIMITED. 0 = no cap.
    tenant_max_concurrent_requests: int = Field(default=256, ge=0)
