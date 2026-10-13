"""`GET /v1/health`: the multi-tier health answer.

A composition module beside `server.main`: the answer reads ClickHouse, the
providers, the boot snapshot, calibration, the rate limiter and, on request,
the worker supervisor and the observability subsystems.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter
from fastapi.responses import Response

from server import boot
from server.operations.capabilities import SCHEMA_VERSION
from server.settings import settings

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
    list `loom doctor` prints and the /me banner renders, each entry with a
    fix; `status` is `degraded` when ClickHouse is down or any entry is an
    error — a configured fake is a warn unless LOOM_REQUIRE_REAL_PROVIDERS.
    """
    from server.auth import revocation
    from server.code.app.index.sandbox import layout_refusal, sandbox_state
    from server.core.ch import reader_user_error, reader_user_state
    from server.core.vectors import codes_state
    from server.embedding.embedder import effective_embedding_dim
    from server.llm.client import provider_key_missing
    from server.observability import instance as _instance
    from server.observability import provider_health
    from server.observability.problems import HealthState, collect_problems, derive_status
    from server.tasks.store.runs import claims_atomic
    from server.workers import leader as _leader

    embedding_model = settings.embedding_model
    llm_provider = settings.llm_provider
    fake_embedding = embedding_model.startswith("fake")
    fake_llm = llm_provider == "fake" or provider_key_missing(llm_provider)
    provider_status = provider_health.status()
    probed_dim = effective_embedding_dim()
    width_mismatches = codes_state.width_mismatches(probed_dim)
    lease = _leader.current()
    main = _main_state()
    reachable = main.get("reachable")
    problems = collect_problems(
        settings,
        HealthState(
            probed_dim=probed_dim,
            width_mismatches=width_mismatches,
            provider_status=provider_status,
            pending_migrations=boot.SKIPPED_PENDING_MIGRATIONS,
            worker_lease_held=bool(lease.held) if lease else None,
            instance_label=_instance.instance_label(),
            otel_metrics_unindexed=otel_metrics_unindexed,
            revocation_age_s=revocation.snapshot_age_seconds(),
            main_reachable=reachable if isinstance(reachable, bool) else None,
            main_error=str(main.get("error") or ""),
            repo_sandbox=sandbox_state(),
            repo_sandbox_refusal=layout_refusal(),
            ch_reader_ok=reader_user_state(),
            ch_reader_error=reader_user_error(),
            run_claims_atomic=claims_atomic(),
        ),
    )
    return {
        "status": derive_status(ch_ok, problems),
        "embedding_model": embedding_model,
        "embedding_dim_configured": settings.embedding_dim,
        "embedding_dim_probed": probed_dim,
        "embedding_dim_mismatch": probed_dim != settings.embedding_dim or bool(width_mismatches),
        "llm_provider": llm_provider,
        "llm_model": settings.llm_model,
        "providers_degraded": fake_embedding or fake_llm or provider_status["degraded"],
        "fake_embedding": fake_embedding,
        "fake_llm": fake_llm,
        "providers": provider_status["providers"],
        "problems": [p.as_dict() for p in problems],
    }


async def _ping_clickhouse() -> tuple[bool, str | None]:
    """(answered, error): `SELECT 1`, the only liveness-load-bearing check."""
    from server.core.ch import get_client

    try:
        row = await get_client().query_one("SELECT 1 AS one")
        return bool(row and row["one"] == 1), None
    except Exception as e:  # noqa: BLE001 — health reports any CH failure
        return False, f"{type(e).__name__}: {e}"


def _mode() -> str:
    """This deployment's tier: CH always; other integrations only when
    configured. Rendered by operators and /me."""
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
    from server.memory.app.embedder_calibration import get_calibration

    calibration = get_calibration()
    if calibration is None:
        return {
            "status": "calibration unavailable; using scalar settings",
            "likely_cause": (
                "fake embedder" if fake_embedding else "probe error — check boot logs"
            ),
        }
    return {
        "enabled": True,
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
        return {"error": f"{type(e).__name__}: {e}"}


def _workers_blocks() -> dict[str, object]:
    from server.workers import leader as _leader
    from server.workers.supervisor import get_supervisor

    out: dict[str, object] = {}
    try:
        out["workers"] = get_supervisor().status()
    except Exception as e:  # noqa: BLE001 — health must not fail on workers
        out["workers"] = {"error": f"{type(e).__name__}: {e}"}
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


@health_router.get("/v1/health")
async def health(response: Response, include: str = "") -> dict[str, object]:
    """Multi-tier health.

    Default response (lightweight; safe for high-frequency liveness
    probes): pings CH (SELECT 1) and reports overall status. The JSON
    body always carries the detail; the status code says whether the
    data plane can serve: 200 when ClickHouse answers, **503 when it
    does not**, so a probe that reads only the code (Docker's healthcheck,
    a load balancer, an uptime monitor) sees the outage.

    `?include=workers,langfuse,events,embedding_cache,llm_cache,otel` opts
    in to per-subsystem checks, each reported independently: only
    `clickhouse` makes the whole response degraded.
    """
    from server.observability import instance as _instance

    requested = {s.strip() for s in include.split(",") if s.strip()}
    ch_ok, ch_error = await _ping_clickhouse()
    prov = _health_provider_fields(ch_ok, await _otel_unindexed_for_health(ch_ok))
    if not ch_ok:
        response.status_code = 503
    out: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "build_commit": settings.build_commit or "unknown",
        # The process (hostname:pid) and the launcher's label, the same
        # stamps every ledger row and OTel resource from it carry.
        "instance": _instance.instance_id(),
        "instance_label": _instance.instance_label(),
        "boot_migrate": bool(settings.boot_migrate),
        "pending_migrations": list(boot.SKIPPED_PENDING_MIGRATIONS),
        "ch_ok": ch_ok,
        "mode": _mode(),
        "read_replica": bool(settings.read_replica),
        **prov,
    }
    if settings.read_replica:
        out["replica"] = _replica_block()
    if ch_error:
        out["ch_error"] = ch_error
    if ch_ok:
        # Repo migrations the ledger lacks and ledger entries no file carries;
        # one ledger read per minute per process, fail-quiet.
        from server.core.schema.status import schema_status

        out["schema"] = await schema_status()
    out["adaptive_thresholds"] = _calibration_block(bool(prov["fake_embedding"]))
    out["ratelimit"] = _ratelimit_block()
    out.update(_included_blocks(requested))
    return out


def _main_state() -> dict[str, object]:
    """A read-replica's last word from its main Loom; empty elsewhere."""
    if not settings.read_replica:
        return {}
    from server.clients.main_loom import MAIN_STATE

    return dict(MAIN_STATE)


def _replica_block() -> dict[str, object]:
    """Which of the main Loom's published settings this read-replica applied."""
    from server.core import deployment_config
    from server.core.provider_process import process_identity
    from server.embedding.embedder import embeds_via_main
    from server.llm.client import llm_via_main

    state = deployment_config.STATE
    return {
        # What `loom read-replica retire --participant` takes if this process dies
        # without shutting down.
        "participant_id": str(process_identity()),
        # Where the ops that read git history go: the main Loom, or nowhere
        # (they answer that this Loom has no git history).
        "git_history": "forwarded"
        if str(settings.read_replica_main_url).strip()
        else "unavailable",
        # Whose embedder serves this replica: the main Loom's (no provider key
        # here) or this process's own.
        "embeddings": "main" if embeds_via_main() else "own",
        "llm": "main" if llm_via_main(settings.llm_provider) else "own",
        # Whether the main Loom answered the last call or probe, when, and why not.
        "main": _main_state(),
        "config_published_by": state["published_by"],
        "config_published_at": state["published_at"],
        "config_applied": len(state["applied"]),
        "token_keys_match_main": bool(state["keys_checked"]) and not state["error"],
        "config_error": state["error"],
    }


def attach(app: Any) -> None:
    app.include_router(health_router)
