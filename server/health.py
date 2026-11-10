"""`GET /v1/health`: the multi-tier health answer; `/v1/health/live` and
`/v1/health/ready`: the probes a load balancer or orchestrator reads.

A composition module beside `server.main`: the answer reads ClickHouse, the
providers, the boot snapshot, calibration, the rate limiter and, on request,
the worker supervisor and the observability subsystems.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable
from typing import Any, cast

from fastapi import APIRouter
from fastapi.responses import Response

from server import __version__, boot
from server.core.errors import shown_unauthenticated
from server.operations.capabilities import SCHEMA_VERSION
from server.settings import settings

log = logging.getLogger(__name__)


def _shown_error(e: BaseException, where: str) -> str:
    """An error as `/v1/health` shows it: `/v1/health` answers without a token."""
    return shown_unauthenticated(log, f"health: {where}", e)


health_router = APIRouter()


async def _otel_unindexed_for_health(ch_ok: bool) -> tuple[str, ...] | None:
    """The metrics tables the erasure leg full-scans, for the
    `problems` list — cached an hour in the erasure store; `None` when the leg is
    off, ClickHouse is down or the read fails (the banner then says nothing)."""
    if not ch_ok or not settings.clickstack_database:
        return None
    try:
        from server.erasure.store import health as erasure_health

        return await erasure_health.otel_metrics_unindexed(settings.clickstack_database)
    except Exception:  # noqa: BLE001 — health never fails on a probe
        return None


def _health_provider_fields(
    ch_ok: bool, otel_metrics_unindexed: tuple[str, ...] | None = None
) -> dict[str, object]:
    """The provider block of `/v1/health` plus the shared problem list and the
    `status` derived from it.

    `fake_embedding` / `fake_llm` say a fake is configured (or a keyed
    provider's key is missing); `providers_degraded` also folds in the
    observed outcome of real calls (the `provider_health` breaker), because a
    total embedding outage leaves both fakes false. `problems` is the same
    list `loom doctor` prints and the dashboard banner renders, each entry with a
    fix; `status` is `degraded` when ClickHouse is down or any entry outside
    `NOT_DEPLOYMENT_FAULTS` is an error — a configured fake is a warn unless
    LOOM_REQUIRE_REAL_PROVIDERS.
    """
    from server.assistant.app.service import assistant_state
    from server.auth import claims as signin_claims
    from server.auth import keyring, revocation
    from server.code.app.index import tier_health
    from server.code.app.index.sandbox import LAYOUT_REFUSED, layout_refusal, sandbox_state
    from server.core.ch import reader_user_error, reader_user_state
    from server.core.vectors import codes_state
    from server.embedding.embedder import effective_embedding_dim, embedder_is_fake
    from server.insights.app import provider_runtime
    from server.insights.app.analysis import analysis_state
    from server.llm.client import llm_is_fake
    from server.observability import instance as _instance
    from server.observability import provider_health
    from server.observability.problems import HealthState, collect_problems, derive_status
    from server.workers import leader as _leader

    fake_embedding = embedder_is_fake()
    fake_llm = llm_is_fake()
    provider_status = provider_health.status()
    probed_dim = effective_embedding_dim()
    width_mismatches = codes_state.width_mismatches(probed_dim)
    lease = _leader.current()
    analysis_ok, analysis_error, analysis_fix = analysis_state()
    assistant_ok, assistant_error = assistant_state()
    from server.core import deployment_config, rmt_carry
    from server.insights.app import provider_barrier

    problems = collect_problems(
        settings,
        HealthState(
            probed_dim=probed_dim,
            width_mismatches=width_mismatches,
            provider_status=provider_status,
            pending_migrations=boot.skipped_migrations(),
            worker_lease_held=bool(lease.held) if lease else None,
            worker_lease_error=lease.error if lease else "",
            worker_lease_given_up=lease.given_up if lease else "",
            instance_label=_instance.instance_label(),
            otel_metrics_unindexed=otel_metrics_unindexed,
            revocation_age_s=revocation.snapshot_age_seconds(),
            keyring=keyring.status(),
            config_drift=tuple(deployment_config.drift()),
            repo_sandbox=sandbox_state(),
            # The refusal names a host path; `/v1/health` answers without a token, so it
            # says what is wrong and `loom doctor` names the path.
            repo_sandbox_refusal=LAYOUT_REFUSED if layout_refusal() else "",
            ch_reader_ok=reader_user_state(),
            ch_reader_error=reader_user_error(),
            analysis_ok=analysis_ok,
            analysis_error=analysis_error,
            analysis_fix=analysis_fix,
            assistant_ok=assistant_ok,
            assistant_error=assistant_error,
            oauth_signin_state_full=signin_claims.keeper_full(),
            stale_participants=provider_barrier.stale_participants(),
            carry_pinned=tuple(rmt_carry.pinned_overdue()),
            refused_configuration=provider_runtime.refused_configuration(),
            code_tier_failures=tier_health.problem_rows(),
        ),
    )
    return {
        "status": derive_status(ch_ok, problems),
        "embedding_model": settings.embedding_model,
        "embedding_dim_configured": settings.embedding_dim,
        "embedding_dim_probed": probed_dim,
        "embedding_dim_mismatch": probed_dim != settings.embedding_dim,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "providers_degraded": fake_embedding or fake_llm or provider_status["degraded"],
        "fake_embedding": fake_embedding,
        "fake_llm": fake_llm,
        "providers": provider_status["providers"],
        "problems": [p.as_dict() for p in problems],
    }


def health_problems() -> list[dict[str, object]]:
    """This process's `/v1/health` problems without a ClickHouse round trip,
    for the `loom.health.problems` gauge."""
    return cast("list[dict[str, object]]", _health_provider_fields(ch_ok=True)["problems"])


async def _ping_clickhouse() -> tuple[bool, str | None]:
    """(answered, error): `SELECT 1`, the only liveness-load-bearing check."""
    from server.core.ch import get_client

    try:
        row = await get_client().query_one("SELECT 1 AS one")
        return bool(row and row["one"] == 1), None
    except Exception as e:  # noqa: BLE001 — health reports any CH failure
        return False, _shown_error(e, "the ClickHouse ping")


def _mode() -> str:
    """This deployment's tier: CH always; other integrations only when
    configured. Rendered by operators and the dashboard."""
    parts = ["ch"]
    if settings.langfuse_url and settings.langfuse_admin_token:
        parts.append("langfuse")
    if settings.clickstack_database or settings.otel_exporter:
        parts.append("clickstack")
    return "+".join(parts)


def _calibration_block(fake_embedding: bool) -> dict[str, object]:
    """The boot calibration's percentiles and derived thresholds, or why the
    gates fell back to the scalar settings. Calibration probes the embedder,
    so only the embedder can be the cause."""
    from server.memory.app.embedder_calibration import (
        calibration_ready,
        calibration_source,
        current_calibration_hash,
        get_calibration,
    )

    calibration = get_calibration()
    if calibration is None:
        return {
            "calibration_hash": None,
            "ready": calibration_ready(),
            "status": "calibration unavailable; using scalar settings",
            "likely_cause": (
                "fake embedder" if fake_embedding else "probe error — check boot logs"
            ),
        }
    return {
        "enabled": True,
        # Equal on every replica serving the same gates; "source" is "shared"
        # (loom.embedder_calibration) or "probe" (this replica's own).
        "calibration_hash": current_calibration_hash(),
        "source": calibration_source(),
        "ready": True,
        "embedder_model": calibration.embedder_model,
        "similar_p25": round(calibration.similar_p25, 4),
        "similar_p50": round(calibration.similar_p50, 4),
        "similar_p75": round(calibration.similar_p75, 4),
        "distractor_p90": round(calibration.distractor_p90, 4),
        "noise_floor": round(calibration.noise_floor, 4),
        "rerank_skip_above": round(calibration.rerank_skip_above, 4),
        "hyde_fallback_threshold": round(calibration.hyde_fallback_threshold, 4),
        "query_rewrite_threshold": round(calibration.query_rewrite_threshold, 4),
        "abstain_threshold": round(calibration.abstain_threshold, 4),
        "conflict_threshold": round(calibration.conflict_threshold, 4),
        "duplicate_threshold": round(calibration.duplicate_threshold, 4),
    }


def _ratelimit_block() -> dict[str, Any]:
    """Which backend holds the rate buckets: "shared" means the web workers of
    this server draw from one table; "per-process" means the cap is divided by
    replicas × workers and a keep-alive client sees 1/N."""
    try:
        from server.core.ratelimit import get_limiter

        return dict(get_limiter().backend_status())
    except Exception as e:  # noqa: BLE001 — health must not fail on the limiter
        return {"error": _shown_error(e, "the rate limiter's status")}


def _workers_blocks() -> dict[str, object]:
    from server.workers import leader as _leader

    out: dict[str, object] = {}
    try:
        from server.workers import child as _workers_child
        from server.workers.status import shown_worker_row

        # Without a token: a pass's text and outcome strings can name another org.
        out["workers"] = [shown_worker_row(r) for r in _workers_child.current_runner().status()]
    except Exception as e:  # noqa: BLE001 — health must not fail on workers
        out["workers"] = {"error": _shown_error(e, "the workers' status")}
    lease = _leader.current()
    out["workers_leader"] = {
        "held": bool(lease.held) if lease else True,
        "reason": lease.reason if lease else "lifespan not run",
        "pid": os.getpid(),
    }
    return out


def _langfuse_block() -> dict[str, object]:
    from server.observability import langfuse_scores, retrieval_judge
    from server.observability.otel import langfuse_processor_status

    return {
        "configured": bool(settings.langfuse_url and settings.langfuse_admin_token),
        "circuit": langfuse_scores.circuit_status(),
        # `span_processor.errors > 0` means telemetry is degraded while requests
        # are fine: every processor method swallows its exceptions so
        # observability never fails a request, so a silently broken processor
        # is the failure mode to watch for.
        "span_processor": langfuse_processor_status(),
        # What was posted, what failed, and what was dropped (queue full, breaker open).
        "scores": langfuse_scores.status(),
        "retrieval_judge": retrieval_judge.status(),
    }


def _events_block() -> dict[str, object]:
    from server.observability import events_stream, get_emitter

    return {"agent_events": get_emitter().stats(), "stream": events_stream.stats()}


def _embedding_cache_block() -> object:
    from server.embedding._cache import stats

    return stats()


def _llm_cache_block() -> object:
    from server.llm.cache import stats

    return stats()


def _otel_block() -> dict[str, object]:
    return {
        "mode": settings.otel_exporter
        or ("ch" if not settings.otel_exporter_otlp_endpoint else "otlp"),
        "endpoint": settings.otel_exporter_otlp_endpoint or "(direct-CH)",
    }


def _included_blocks(requested: set[str]) -> dict[str, object]:
    """The opt-in subsystems `?include=` names, each reported on its own."""
    out: dict[str, object] = {}
    if "workers" in requested:
        out.update(_workers_blocks())
    if "langfuse" in requested:
        out["langfuse"] = _langfuse_block()
    if "events" in requested:
        out["events"] = _events_block()
    if "embedding_cache" in requested:
        out["embedding_cache"] = _embedding_cache_block()
    if "llm_cache" in requested:
        out["llm_cache"] = _llm_cache_block()
    if "otel" in requested:
        out["otel"] = _otel_block()
    return out


async def _mirrors_block() -> dict[str, object]:
    """This host's git mirrors (ClickHouse's bootstrap_stage is deployment-wide).
    Walks every mirror's directory, so it is opt-in."""
    from server.code.app.index.mirrors import mirror_status
    from server.code.domain.index.repos import RegistryUnread

    try:
        return await asyncio.to_thread(mirror_status)
    except RegistryUnread:  # the state an operator reads this block to diagnose
        return {"registry": "unread"}


@health_router.get("/v1/health/live")
async def health_live() -> dict[str, object]:
    """Liveness: the process is up and its event loop answers. No ClickHouse
    call, so a ClickHouse outage never gets a healthy replica restarted."""
    return {"status": "ok"}


@health_router.get("/v1/health/ready")
async def health_ready(response: Response) -> dict[str, object]:
    """Readiness: 200 only when every required check passes (ClickHouse,
    migrations, token state, warm-up loads, calibration, config drift, the
    sandbox, not draining); 503 with the failing checks otherwise. Each check
    reads in-memory state or a cached answer, so a probe a second is cheap."""
    from server.core import readiness

    state = await readiness.evaluate()
    if not state.ready:
        response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    return state.as_dict()


@health_router.get("/v1/health")
async def health(response: Response, include: str = "") -> dict[str, object]:
    """Multi-tier health.

    Default response (lightweight; safe for high-frequency liveness
    probes): pings CH (SELECT 1) and reports overall status. The JSON
    body always carries the detail; the status code says whether the
    data plane can serve: 200 when ClickHouse answers, **503 when it
    does not**, so a probe that reads only the code (Docker's healthcheck,
    a load balancer, an uptime monitor) sees the outage.

    `?include=workers,langfuse,events,embedding_cache,llm_cache,otel,mirrors,postgres` opts
    in to per-subsystem checks, each reported independently: only
    `clickhouse` makes the whole response degraded.
    """
    from server.core.provider_process import process_identity
    from server.observability import instance as _instance

    requested = {s.strip() for s in include.split(",") if s.strip()}
    ch_ok, ch_error = await _ping_clickhouse()
    prov = _health_provider_fields(ch_ok, await _otel_unindexed_for_health(ch_ok))
    if not ch_ok:
        response.status_code = 503
    out: dict[str, object] = {
        # The server's release, which a coding-agent plugin compares with the
        # minimum its manifest declares.
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "build_commit": settings.build_commit or "unknown",
        # The process (hostname:pid) and the launcher's label, the same
        # stamps every ledger row and OTel resource from it carry.
        "instance": _instance.instance_id(),
        "instance_label": _instance.instance_label(),
        # What `loom participant retire --participant` takes if this process
        # dies without shutting down.
        "participant_id": str(process_identity()),
        "boot_migrate": bool(settings.boot_migrate),
        "pending_migrations": list(boot.skipped_migrations()),
        "ch_ok": ch_ok,
        "mode": _mode(),
        # Which store holds live memory; under postgres, whether it answers.
        "storage_authority": settings.storage_authority,
        **prov,
    }
    from server.core import pg

    if pg.enabled():
        out["pg_ok"] = await _pg_ok()
        # The detail is opt-in: the default answer stays one ping for a liveness probe.
        if out["pg_ok"] and "postgres" in requested:
            out["postgres"] = await _postgres_block()
    out["deployment_config"] = _deployment_config_block()
    if ch_error:
        out["ch_error"] = ch_error
    if ch_ok:
        # Repo migrations the ledger lacks and ledger entries no file carries;
        # one ledger read per minute per process, fail-quiet.
        from server.core.schema.status import schema_status

        out["schema"] = await schema_status()
    from server.core import readiness

    # The same answer /v1/health/ready gives, with the reason for each check.
    out["readiness"] = (await readiness.evaluate()).as_dict()
    out["adaptive_thresholds"] = _calibration_block(bool(prov["fake_embedding"]))
    out["ratelimit"] = _ratelimit_block()
    if "mirrors" in requested and settings.code_intel_enabled:
        out["mirrors"] = await _mirrors_block()
    out.update(_included_blocks(requested))
    return out


async def _pg_ok() -> bool:
    from server.core import pg

    return bool(await _bounded(pg.fetchval("SELECT 1"), 0) == 1)


async def _postgres_block() -> dict[str, object]:
    """How commits are acknowledged (`pg_profile.standby_now`), and the deliveries, audit
    rows and knowledge document deliveries ClickHouse kept refusing past the attempt cap;
    each None when unread."""
    from server.core import pg_profile
    from server.knowledge.store import pg_documents
    from server.memory.store import pg_memory

    standby, deliveries, audits, documents, backlog = await asyncio.gather(
        _bounded(pg_profile.standby_now(), None),
        _bounded(pg_memory.parked_count(), None),
        _bounded(pg_memory.parked_audits_count(), None),
        _bounded(pg_documents.parked_count(), None),
        _bounded(_backlog(), None),
    )
    return {
        "standby": standby,
        "parked_deliveries": deliveries,
        "parked_audits": audits,
        "parked_knowledge_deliveries": documents,
        "backlog": backlog,
    }


_backlog_cache: dict[str, Any] = {"at": None, "value": None}


async def _backlog() -> dict[str, dict[str, Any]]:
    """Each source's delivery backlog (`pg_pipeline`): the lease holder's last reading
    when fresh, else a reading of this process's own, kept
    LOOM_PG_BACKLOG_HEALTH_CACHE_SECONDS."""
    import dataclasses

    from server.knowledge.store import pg_documents
    from server.memory.store import pg_memory
    from server.observability import pg_pipeline

    readings = pg_pipeline.fresh_backlog()
    if len(readings) < len(pg_pipeline.SOURCES):
        now = time.monotonic()
        at = _backlog_cache["at"]
        if at is None or now - at >= settings.pg_backlog_health_cache_seconds:
            cap = settings.pg_backlog_count_cap
            _backlog_cache["value"] = {
                "memory": await pg_memory.backlog(cap),
                "audit_outbox": await pg_memory.outbox_backlog(cap),
                "knowledge": await pg_documents.backlog(cap),
            }
            _backlog_cache["at"] = now
        readings = _backlog_cache["value"]
    return {source: dataclasses.asdict(r) for source, r in readings.items()}


async def _bounded(read: Awaitable[Any], default: Any) -> Any:
    """`read`'s answer within two seconds, or `default`: each read is bounded on its own."""
    try:
        return await asyncio.wait_for(read, timeout=2.0)  # not a setting: health's bound
    except Exception:  # noqa: BLE001 — the health answer reports it, never raises
        return default


def _deployment_config_block() -> dict[str, object]:
    """This process against the deployment's published configuration, and the
    live instances the rate limits divide by."""
    from server.core import deployment_config, instances

    state = deployment_config.STATE
    return {
        "instance_id": instances.instance_id(),
        "live_hosts": instances.live_hosts(),
        "checked": bool(state["checked"]),
        "drift": deployment_config.drift(),
        "published_by": state["published_by"],
        "published_at": state["published_at"],
        "error": state["error"],
    }


def attach(app: Any) -> None:
    app.include_router(health_router)
