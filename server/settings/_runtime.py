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

    # The rate limiter divides every cap by the deployment's replica count so
    # the replicas' sum matches the target (approximate, not cluster-strict).
    # The count is the hosts with a live serving loom.instances row; this is
    # its floor, and the count while those rows cannot be read.
    replica_count: int = Field(default=1, ge=1)
    # False for a process no load balancer routes to (a workers-only
    # container): its instance row does not count as a replica.
    serves_traffic: bool = True
    # Seconds between this process's loom.instances heartbeats, each of which
    # also compares it with the deployment's published configuration. A row
    # not renewed for three intervals is not live.
    instance_heartbeat_seconds: float = Field(default=10.0, ge=1.0)
    # uvicorn processes per container (the image's default: server.core.web_workers);
    # the rate limiter counts N workers as N replicas.
    web_workers: int = Field(default=1, ge=1)
    # Directory of the files one server's web processes share (the rate-limit
    # table); "" = chosen per process by server/core/runtime_files.py.
    # scripts/serve.sh sets it once, before the workers start, so all of them
    # name the same files.
    runtime_dir: str = ""
    # Which processes run the WorkerSupervisor: auto = the one process of the
    # deployment holding the `workers_supervisor` lease in ClickHouse
    # (server/workers/leader.py), and none outside the deployment (no
    # LOOM_DEPLOYMENT); on = always; off = never (a pure web replica).
    background_workers: Literal["auto", "on", "off"] = "auto"
    # auto mode: seconds a lease holder that stops renewing keeps the workers
    # before another process takes them over (failover time). At least 24: below
    # that a renewal blocked for its 2 s timeout leaves no retry before the
    # holder must give up (server/workers/leader.py has the timings).
    supervisor_lease_ttl_s: int = Field(default=30, ge=24)
    # Seconds between passes of the always-on sweep that deletes the KeeperMap
    # keys (leases, claims, graph slots) a stopped process left behind.
    keeper_sweep_interval_seconds: float = Field(default=3600.0, gt=0)
    # auto mode: where the lease holder runs the workers. child = a separate
    # process it starts and stops with the lease, so the passes do not share
    # the event loop that serves requests; inline = in the serving process.
    workers_process: Literal["child", "inline"] = "child"
    # Seconds a process keeps serving after SIGTERM while /v1/health/ready
    # answers 503, so the load balancer stops routing to it before its port
    # closes. Size it to the LB's probe interval × failure threshold. 0 = stop at once.
    shutdown_ready_delay_s: float = Field(default=15.0, ge=0)
    # Seconds boot waits for the first load of the tracker registry and the
    # runtime overrides, and the first publish of the dashboard bundle, before
    # it serves; readiness stays false until the first two load.
    boot_warmup_timeout_s: float = Field(default=30.0, ge=0)
    # True: /v1/health/ready also waits until the mirror warm-up has queued its
    # clones and while a clone of this host's git mirrors is running or queued (a
    # failed clone does not hold it). Needs LOOM_CODE_MIRROR_WARMUP: without the
    # warm-up nothing is cloned at boot and readiness does not wait for mirrors.
    # Off by default: history ops answer `no_git_mirror` meanwhile.
    ready_requires_mirrors: bool = False
    # Seconds between re-reads of loom.runtime_overrides (the admin rate-limit,
    # latency-budget and live-setting overrides) in every process.
    runtime_overrides_refresh_seconds: float = 10.0
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
    # Required behind a load balancer: at the default every client counts as the
    # balancer's address for the sign-in and sign-up limits.
    trusted_proxies: str = "127.0.0.1"
    # True lets a non-loopback bind start with the dev signing secret or
    # with auth off. Only for a port that is not actually reachable by others.
    allow_insecure: bool = False

    def workers_mode(self) -> str:
        """`background_workers` as this process runs it: `auto` outside the
        deployment is `off`, so a branch server never runs the deployment's
        workers. `on` is the explicit opt-in there."""
        if self.background_workers == "auto" and not self.deployment:
            return "off"
        return self.background_workers
