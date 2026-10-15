"""The task inbox workers and what run.complete does to a run's rows."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class TaskSettings(BaseModel):
    # ---- task inbox ----
    # Lease sweeper: return expired leases to todo, resume deferred tasks at
    # resume_at, escalate stale triage. One read per pass.
    task_lease_sweep_enabled: bool = True
    # Seconds between passes while there is work.
    task_lease_sweep_interval_seconds: int = 60
    # Seconds between passes that found nothing.
    task_lease_sweep_idle_interval_seconds: int = 300
    # Claim arbitration: "auto" = KeeperMap when loom.task_leases
    # exists, else the append-only claim log; "keeper" / "log" pin
    # one.
    task_lease_backend: Literal["auto", "keeper", "log"] = "auto"
    # Post one Standard Webhooks-signed notice per new event batch to each
    # inbox's webhook_url; the secret comes from the env var
    # task_inboxes.webhook_secret_ref names (`LOOM_WEBHOOK_SECRET_<ORG>__<NAME>`,
    # one of the inbox org's own).
    task_webhook_deliver_enabled: bool = True
    # Seconds between passes while notices are going out.
    task_webhook_deliver_interval_seconds: int = 5
    # Seconds between passes that found no new event.
    task_webhook_deliver_idle_interval_seconds: int = 30

    # ---- run completion ----
    # What run.complete does to the rows the run wrote when the call does not
    # say: keep = nothing; demote = scale confidence by the expiry demote
    # factor now; expire = expires_at now + memory_run_complete_expire_days
    # with on_expire=demote. Pinned, verified, promoted, locked, episodic and
    # reflection rows are always kept, and so is everything an MCP session's
    # implicit run wrote when the transport closes it.
    memory_run_complete_retain: Literal["keep", "demote", "expire"] = "keep"
    # Days until expiry for `memory_run_complete_retain = expire`.
    memory_run_complete_expire_days: int = 7
