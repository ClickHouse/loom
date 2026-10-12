"""Process shape, deployment, event stream and cost budgets."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RuntimeSettings(BaseModel):
    # Deployed git commit, stamped at image build (ARG LOOM_BUILD_COMMIT) and
    # shown in /v1/health. Empty in a source run.
    build_commit: str = ""

    # Flush cadence of the loom.events queue (budget violations); also the
    # visibility delay of those rows and the loss window on a process crash.
    events_stream_flush_interval_s: float = 1.0
    # Queued telemetry rows that trigger a flush before the interval elapses.
    events_stream_flush_batch: int = 500
    # How long a namespace's registry class is kept in the process cache.
    traffic_class_registry_ttl_seconds: int = 300

    # Daily LLM spend cap per namespace, USD: extraction workers skip a
    # namespace whose rolling 24 h cost reached it. Overridden per namespace by
    # loom.namespaces.daily_cost_cap_usd; 0.0 disables on both tiers.
    default_daily_cost_cap_usd: float = 1.0

    # The in-process rate limiter divides every cap by this count so the
    # replicas' sum matches the target (approximate, not cluster-strict).
    replica_count: int = Field(default=1, ge=1)
    # uvicorn worker processes per container, read by the image's
    # scripts/serve.sh (unset there = one per CPU the container may use, which it
    # exports) and by the rate limiter (N workers count as N replicas).
    web_workers: int = Field(default=1, ge=1)
    # Entity-graph write marks shared by the processes of one server
    # (server/memory/app/graph_write_marks.py): each namespace's last graph write and its
    # in-flight projections, so a graph walk on one worker never serves a walk
    # remembered from before a write another process made. auto = shared when
    # graph_write_marks_dir is set or web_workers > 1; on = always; off =
    # process-local only.
    graph_write_marks_shared: Literal["auto", "on", "off"] = "auto"
    # Directory of the shared marks; "" = <runtime dir>/loom-graph-marks-<parent
    # pid> (the workers of one uvicorn share a parent). Set it to a directory
    # every process that writes the graph mounts, the workers container too.
    graph_write_marks_dir: str = ""
    # Which processes run the WorkerSupervisor: auto = one per host, elected
    # by an advisory flock on supervisor_lock_path; on = always; off = never
    # (a pure web replica).
    background_workers: Literal["auto", "on", "off"] = "auto"
    # File whose advisory flock elects the one supervisor process per host in auto mode.
    supervisor_lock_path: str = "/tmp/loom-supervisor.lock"
    # auto mode: seconds between lease retries by a process that lost it at
    # boot, so the new blue/green colour takes the workers when the old one
    # exits. 0 = one try at boot.
    supervisor_lease_retry_s: float = 5.0
    # The deployment's compose files set LOOM_DEPLOYMENT=1; every other process
    # must set LOOM_INSTANCE_LABEL (e.g. `iso:<worktree>:<port>`,
    # `ci:integration`). A process with neither refuses to start, so every
    # ledger row is attributable.
    deployment: bool = False
    # At most one `budget.violated` audit row per op per this many seconds
    # (the row carries `suppressed_since_last`). 0 = a row per violation.
    budget_violation_audit_interval_s: float = 60.0
    # Violations per window that still write their own row before the rest are
    # folded into the next: a short burst stays visible, a storm costs at most
    # this many inserts a minute per (op, org, namespace).
    budget_violation_audit_burst: int = 5
    # The address the server hands to clients — the Setup page's config
    # block, `claude mcp add`, a bot's claim_url, OAuth metadata. Empty = the
    # request's own Host and scheme, which only a loopback bind may rely on:
    # a non-loopback bind_host refuses to start without it.
    public_base_url: str = ""
    # The address Loom is reachable on: `loom serve` and `make server` bind
    # it; in a container (which listens on all its interfaces) it is the
    # address the port is published on. Anything but loopback turns on the
    # startup checks for the dev secret, auth-off and public_base_url.
    bind_host: str = "127.0.0.1"
    # Comma-separated peer addresses or CIDRs whose X-Forwarded-For/Proto
    # headers uvicorn applies; any other peer's are ignored. "*" trusts all.
    trusted_proxies: str = "127.0.0.1"
    # True lets a non-loopback bind start with the dev signing secret or
    # with auth off. Only for a port that is not actually reachable by others.
    allow_insecure: bool = False
