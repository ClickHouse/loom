"""FastAPI application entrypoint.

Run with:
    uvicorn server.main:app --host 0.0.0.0 --port 7777 --loop asyncio

or:
    make server
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from server import __version__, boot
from server.auth.public_url import (
    cors_origin_list,
    host_header_allowed,
    origin_matches_host,
    same_origin,
)
from server.core.ch import get_client, probe_reader_user
from server.core.logging_config import configure_logging
from server.observability import get_emitter
from server.operations.capabilities import operation_rows
from server.settings import settings
from server.tasks.store import runs as runs_store
from server.transport import http as http_transport
from server.transport import mcp as mcp_transport
from server.wiring import wire

# Install formatter before any uvicorn / module-level logging fires.
configure_logging()
wire()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Who launched this process? The deployment says so with
    # LOOM_DEPLOYMENT=1; every other process carries LOOM_INSTANCE_LABEL. A
    # process with neither would write ledger rows and spans nobody can
    # attribute, so it does not get to connect. Raising here makes uvicorn
    # print the message and exit ("Application startup failed").
    from server.observability import instance as _instance_mod

    _instance_mod.require_instance_label(
        _instance_mod.instance_label(), deployment=bool(settings.deployment)
    )
    from server.auth.jwt import require_real_signing_secret

    require_real_signing_secret(
        deployment=bool(settings.deployment),
        instance_label=_instance_mod.instance_label() or "",
        auth_required=settings.auth_actually_required(),
        dev_secret=settings.jwt_secret_is_dev_default(),
        dev_sealing_secret=settings.jwt_sealing_secret_is_dev_default(),
    )
    boot.require_safe_bind(
        bind_host=settings.bind_host,
        public_base_url=settings.public_base_url,
        auth_required=settings.auth_actually_required(),
        dev_secret=settings.jwt_secret_is_dev_default(),
        allow_insecure=settings.allow_insecure,
    )

    ch = get_client()
    await ch.connect()

    # Apply pending migrations on every boot, in the server's own lifespan, so
    # the migrations the server runs match the migrations its code expects.
    # Idempotent — the ledger skips already-applied files.
    #
    # Under LOOM_WEB_WORKERS > 1 every worker process runs this step.
    # Unserialised, all of them see the same pending file and race the ALTER:
    # on SharedMergeTree the losers fail with ClickHouse code 517 ("replica
    # doesn't catch up" / "previous mutation query is not finished yet"),
    # uvicorn stops the parent and the container restarts. The lock below is
    # host-local (the workers share one container), so they apply in turn and
    # the later ones read "no pending migrations" from the ledger. Across
    # containers (web + workers of one colour) the runner itself takes a lease
    # row in loom._migration_lease before applying.
    from server.core.schema import migrate as _migrate_mod

    rc = await boot.run_fenced_boot_migrations(
        ch,
        _migrate_mod.main,
        settings.boot_migrate_lock_path,
        enabled=bool(settings.boot_migrate),
        pending=_migrate_mod.pending_names,
    )
    if rc != 0:
        raise RuntimeError(
            f"schema migrations failed on boot (exit code {rc}); "
            f"refusing to start workers against an inconsistent schema"
        )
    boot.require_replica_schema()
    # A read-replica takes the main Loom's published settings (code
    # repositories, tracker bindings, the embedding model) before anything
    # below reads them.
    replica_config_loop = await boot.start_replica_config(ch)

    from server.core.background import register_buffer_drainer
    from server.insights.app.provider_barrier import register_process

    register_buffer_drainer("boot-writes", boot.drain_writes)
    from server.insights.app.provider_runtime import load_active_configuration, refresh_loop

    await register_process()
    await load_active_configuration()

    emitter = get_emitter()
    await emitter.start()

    # Auth-off deployments serve a single configured namespace: give it a
    # loom.namespaces row now (and warn about data stranded in others) rather
    # than on first dashboard load — the warning matters most to an operator
    # who just changed LOOM_DEFAULT_NAMESPACE and is watching the boot log.
    # auth_actually_required, not auth_required: single-user identity mode
    # with auth_required=true is still an auth-off deployment.
    if not settings.auth_actually_required():
        from server.auth.default_namespace import ensure_default_namespace_cached

        await ensure_default_namespace_cached()

    from server.workers import leader as _leader
    from server.workers.supervisor import get_supervisor

    supervisor = get_supervisor()
    # With `uvicorn --workers N` every process runs this
    # lifespan; exactly one per host runs the background workers.
    lease = _leader.decide_supervisor(settings.background_workers, settings.supervisor_lock_path)
    _leader.set_current(lease)
    # Apply per-table retention overrides from LOOM_RETENTION_*_DAYS, if set —
    # from the leader only, and only where the TTL differs.
    from server.core.model_work_gate import get_model_work_gate
    from server.storage import retention_overrides

    if not get_model_work_gate().closed:
        async with get_model_work_gate().admit():
            await retention_overrides.apply(settings, ch=ch, leader=lease.held)
    # The tracker namespaces are directory rows served live; the
    # leader gives each configured one its row (restricted where listed) at
    # boot, BEFORE any read, so the gate never sees a restricted namespace as
    # an unregistered standard one.
    if lease.held:
        await boot.register_tracker_namespaces(ch)
        boot.publish_deployment_config(ch)
    # This process verifies tokens, so it loads the
    # revoked set now and keeps it fresh itself unless the supervisor's
    # jwt_revocation_refresh worker runs here.
    revocation_loop = await boot.start_revocation(lease.held)
    await probe_reader_user(ch)
    await runs_store.probe_claims(ch)
    tracker_sources_loop = boot.start_tracker_sources_refresher(ch)
    await boot.probe_sandbox()
    lease_retry: asyncio.Task[None] | None = None
    if lease.held:
        supervisor.start_all()
    else:
        logging.getLogger(__name__).info(
            "WorkerSupervisor: not started in pid %s — %s", os.getpid(), lease.reason
        )
        lease_retry = boot.start_lease_retry(lease, supervisor, ch, revocation_loop)

    if not get_model_work_gate().closed:
        await boot.provider_report()
        await boot.probe_vector_capabilities(ch)
        await boot.calibrate_similarity_gates()

    provider_refresh = asyncio.create_task(refresh_loop(), name="provider-configuration-refresh")

    try:
        yield
    finally:
        provider_refresh.cancel()
        await asyncio.gather(provider_refresh, return_exceptions=True)
        if lease_retry is not None and not lease_retry.done():
            lease_retry.cancel()
        if revocation_loop is not None and not revocation_loop.done():
            revocation_loop.cancel()
        tracker_sources_loop.cancel()
        if replica_config_loop is not None:
            replica_config_loop.cancel()
        boot.cancel_boot_tasks()
        if lease.held:
            await supervisor.stop_all(drain_timeout_s=10.0)
        lease.release()
        # The service-side writes still in flight land while what they use is
        # open: a deferred conflict judge calls the LLM over the shared HTTP
        # pool and emits its settle row through the event emitter.
        await boot.drain_writes()
        # The judge emits a cost event and posts scores: it stops before the
        # emitter and the score queue drain, and both before the HTTP clients close.
        from server.observability import langfuse_scores, retrieval_judge

        await retrieval_judge.stop()
        await langfuse_scores.drain(timeout_s=5.0)
        await emitter.stop()
        _otel.shutdown()
        from server.clients import _http as _http_clients

        await _http_clients.aclose()
        from server.insights.app.provider_barrier import retire_process

        await retire_process()
        await ch.disconnect()


app = FastAPI(
    title="Loom Control Plane",
    description=(
        "Shared memory, knowledge, and observability data plane for AI agents "
        "— backed by ClickHouse."
    ),
    version=__version__,
    lifespan=lifespan,
)

# Register the typed-error → JSON serializer: server.core.errors.LoomServerError
# subclasses become structured {"detail": {"code", "message", ...}} responses.
from server.core.errors import register_exception_handler as _register_loom_errors  # noqa: E402

_register_loom_errors(app)

# OTel instrumentation. Must run AFTER app construction (so FastAPIInstrumentor
# can wrap it) but BEFORE routes are added (so every endpoint inherits it).
from server.observability import otel as _otel  # noqa: E402

_otel.init_otel(app)

# Request-body cap — the one bounded input at the front door (413 before any
# parser runs; LOOM_MAX_REQUEST_BYTES, 0 disables). Added before CORS so an
# oversize preflight-free POST is refused at the outermost layer.
from server.transport.body_limit import BodyLimitMiddleware as _BodyLimit  # noqa: E402

app.add_middleware(_BodyLimit, max_bytes=settings.max_request_bytes)

from server.transport.model_maintenance import ModelMaintenanceMiddleware  # noqa: E402

app.add_middleware(ModelMaintenanceMiddleware)

# gzip here (nginx does not gzip proxied bodies): the dashboard bundle and
# JSON bodies above 1 KB. A year-long immutable cache for /me/assets/* — the
# hash in the filename is the version — so a load does not re-validate them.
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/", include_in_schema=False)
async def root_dashboard_redirect() -> Response:
    """Make the memorable server origin open the local dashboard."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/me", status_code=302)


@app.middleware("http")
async def _cache_and_nosniff_headers(request: Request, call_next: Any) -> Response:
    response: Response = await call_next(request)
    if request.url.path.startswith("/me/assets/") and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    # No response is ever sniffed into a type it was not served as.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


# CORS — opt-in via LOOM_CORS_ORIGINS (comma-separated exact origins).
if settings.cors_origins:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origin_list(settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=[
            "X-Request-Id",
            "X-Schema-Version",
            "Server-Timing",
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "X-Loom-Traffic-Class",
            "Retry-After",
            "Link",
        ],
    )


def _refused(status: int, *, code: str, message: str) -> Response:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status,
        content={"detail": {"code": code, "message": message}},
        headers={"X-Content-Type-Options": "nosniff"},
    )


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _own_origin(origin: str, request: Request) -> bool:
    if settings.public_base_url.strip():
        return same_origin(origin, settings.public_base_url.strip().rstrip("/"))
    return origin_matches_host(origin, request.headers.get("host", ""))


def _is_navigation(request: Request) -> bool:
    """A top-level page load, such as following a link to /me from another site."""
    return (
        request.method in ("GET", "HEAD")
        and request.headers.get("sec-fetch-mode") == "navigate"
        and request.headers.get("sec-fetch-dest") == "document"
    )


def web_refusal(request: Request) -> Response | None:
    """What stops a page on another site from driving this server, or None to let the
    request through. A cross-origin POST with a text/plain body needs no preflight, and
    in proxy mode the front proxy adds the bearer itself, so a foreign Origin or a
    cross-site / same-site fetch is refused (403) unless LOOM_CORS_ORIGINS lists it.
    curl and the SDKs send neither header. With auth off every method is checked except
    a top-level navigation, and a foreign Host is DNS rebinding (421)."""
    auth_off = not settings.auth_actually_required()
    if auth_off and not host_header_allowed(
        request.headers.get("host", ""), settings.public_base_url
    ):
        return _refused(
            421,
            code="MISDIRECTED_REQUEST",
            message="this server answers only localhost or LOOM_PUBLIC_BASE_URL",
        )
    if not auth_off and request.method in _SAFE_METHODS:
        return None
    if _is_navigation(request):
        return None
    origin = request.headers.get("origin")
    if origin is not None and origin in cors_origin_list(settings.cors_origins):
        return None
    foreign_origin = origin is not None and not _own_origin(origin, request)
    fetch_site = request.headers.get("sec-fetch-site")
    if foreign_origin or fetch_site in ("cross-site", "same-site"):
        return _refused(
            403, code="CROSS_SITE_REQUEST", message="this server refuses cross-site requests"
        )
    return None


async def _web_gate(request: Request, call_next: Any) -> Response:
    """Runs before any route handler; the auth mode is read per request."""
    refusal = web_refusal(request)
    if refusal is not None:
        return refusal
    response: Response = await call_next(request)
    return response


def attach_web_gate(target: FastAPI) -> None:
    target.middleware("http")(_web_gate)


attach_web_gate(app)

http_transport.attach(app)
mcp_transport.attach(app)

from server.auth import login as _login  # noqa: E402
from server.auth import oauth as _oauth  # noqa: E402
from server.auth import self_signup as _self_signup  # noqa: E402
from server.identity.app import claim_release as _claim_release  # noqa: E402

_self_signup.attach(app)
_login.attach(app)
_claim_release.attach(app)
# OAuth 2.1 AS for MCP one-click connect — mounts only when LOOM_OAUTH_ENABLED.
_oauth.attach(app)

from server.insights.app.admin_routes import admin_router as _admin_router  # noqa: E402
from server.insights.app.admin_routes import org_admin_router as _org_admin_router  # noqa: E402
from server.insights.app.provider_routes import provider_router as _provider_router  # noqa: E402

app.include_router(_admin_router)
app.include_router(_org_admin_router)
app.include_router(_provider_router)

# attach() mounts the StaticFiles assets dir before including the router so the
# /me/{spa_path:path} catch-all doesn't swallow asset requests.
from server.identity.app import me_routes as _me_handlers  # noqa: E402

_me_handlers.attach(app)

# The task inbox plane's SSE wake-up, GET-shaped like
# knowledge.download (a stream, not a JSON envelope).
from server.tasks.app import watch_routes as _task_watch  # noqa: E402

_task_watch.attach(app)

from server import health as _health  # noqa: E402
from server.auth import routes as _auth_routes  # noqa: E402
from server.knowledge.app import download_routes as _knowledge_download  # noqa: E402

_auth_routes.attach(app)
_health.attach(app)
_knowledge_download.attach(app)


@app.get("/v1/api/openapi.json")
async def v1_openapi() -> dict[str, object]:
    """v1-pinned OpenAPI export.

    FastAPI's built-in /openapi.json is the live (unversioned) spec.
    `/v1/api/openapi.json` is the same content but at a path SDK
    codegen can pin to — when v2 lands, /v2/api/openapi.json can
    diverge without breaking v1 clients.
    """
    return app.openapi()


@app.get("/v1/operations")
async def list_operations() -> list[dict[str, object]]:
    """Discovery endpoint: list every operation the server knows about."""
    return operation_rows()
