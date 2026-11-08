"""FastAPI application entrypoint.

Run with `make server`, or `scripts/serve.sh` (the image's launcher). A bare
`uvicorn server.main:app --host 0.0.0.0` leaves LOOM_BIND_HOST at loopback, so the
startup checks for a reachable server do not run: set LOOM_BIND_HOST to the same address.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from server import __version__, boot
from server.auth.public_url import (
    cors_origin_list,
    host_header_allowed,
    origin_matches_host,
    same_origin,
)
from server.core import readiness as _readiness
from server.core.ch import get_client, probe_reader_user
from server.core.errors import classified
from server.core.logging_config import configure_logging
from server.insights.app.provider_barrier import participant_lifespan
from server.memory.app import embedder_calibration
from server.observability import get_emitter
from server.operations.capabilities import operation_rows
from server.settings import settings
from server.transport import http as http_transport
from server.transport import mcp as mcp_transport
from server.wiring import wire

# Install formatter before any uvicorn / module-level logging fires.
configure_logging()
wire()


async def _shut_down(ch: Any, lease: Any, supervisor: Any, emitter: Any) -> None:
    """The bounded part of a shutdown: the instance row's stop write and the drains
    share one budget, and the final cleanup gets what they left unused, so the whole
    runs inside the stop grace period whatever ClickHouse does."""
    from server.core import instances as _instances
    from server.insights.app.provider_barrier import SHUTDOWN_RETIRE_S

    started = time.monotonic()
    await _instances.stop(ch)
    left = max(0.0, boot.SHUTDOWN_DRAIN_BUDGET_S - (time.monotonic() - started))
    try:
        await asyncio.wait_for(_shutdown_drains(lease, supervisor, emitter), timeout=left)
    except TimeoutError:
        logging.getLogger(__name__).warning(
            "shutdown drains exceeded %.0f s; retiring anyway", boot.SHUTDOWN_DRAIN_BUDGET_S
        )
    unused = max(0.0, boot.SHUTDOWN_DRAIN_BUDGET_S - (time.monotonic() - started))
    await boot.leave_deployment(ch, budget_s=SHUTDOWN_RETIRE_S + unused)


async def _shutdown_drains(lease: Any, supervisor: Any, emitter: Any) -> None:
    """The lifespan's shutdown drains, in order; it bounds them all together."""
    from server.workers import leader as _leader

    # An agent claim's last cutoff, which ends what an older replica's sign-in
    # with the code issued while the claim was landing: started first, so it
    # runs beside the drains below.
    claims, started = asyncio.create_task(_agent_claim.drain()), time.monotonic()
    try:
        await _leader.shutdown(lease, supervisor)
        # The service-side writes still in flight land while what they use is
        # open: a write's after-write tasks use the shared HTTP pool and emit
        # through the event emitter.
        # MCP session runs still being begun land first, so the session-run
        # sweep can close them (boot sits below the transport).
        from server.transport import mcp_runs

        with contextlib.suppress(TimeoutError):
            # not a setting: one shutdown step
            await asyncio.wait_for(mcp_runs.drain_ledger_writes(), 5.0)
        await boot.drain_writes()
        # The judge emits a cost event and posts scores: it stops before the
        # emitter and the score queue drain, and both before the HTTP clients close.
        from server.observability import langfuse_scores, retrieval_judge

        await retrieval_judge.stop()
        await langfuse_scores.drain(timeout_s=5.0)
        # Counted from the drains' start, so the emitter and the clients below
        # still close in the budget.
        left = _agent_claim.SHUTDOWN_WAIT_S - (time.monotonic() - started)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(claims, max(0.0, left))
    finally:
        claims.cancel()  # a no-op once it is done
    await emitter.stop()
    _otel.shutdown()
    from server.clients import _http as _http_clients

    await _http_clients.aclose()


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
    )
    from server.auth.keyring import weak_keys
    from server.auth.scim_routes import require_scim_settings

    require_scim_settings(
        settings.scim_token, settings.oauth_login_provider, settings.auth_actually_required()
    )
    boot.require_safe_bind(
        bind_host=settings.bind_host,
        public_base_url=settings.public_base_url,
        auth_required=settings.auth_actually_required(),
        dev_secret=settings.jwt_secret_is_dev_default(),
        allow_insecure=settings.allow_insecure,
        mcp_open=settings.mcp_auth_mode == "open",
        weak_keys=weak_keys(),
    )

    from server.core.object_store import require_client

    require_client()
    await boot.require_clickhouse_ready()
    ch = get_client()
    await ch.connect()
    # What /v1/health/ready checks; each answers not-ready until its state loads.
    boot.register_readiness_checks(ch)

    # Apply pending migrations on every boot, in the server's own lifespan, so
    # the migrations the server runs match the migrations its code expects.
    # Idempotent — the ledger skips already-applied files. Every process of the
    # deployment runs this step; the runner takes the deployment's migration
    # lock (a KeeperMap key) before applying, so they apply in turn and the
    # later ones find nothing pending.
    from server.core.schema import migrate as _migrate_mod

    rc = await boot.run_fenced_boot_migrations(
        ch,
        # The preflight already ran above, before the first connect.
        functools.partial(_migrate_mod.main, check_server=False),
        enabled=bool(settings.boot_migrate),
        pending=_migrate_mod.pending_names,
    )
    if rc != 0:
        raise RuntimeError(
            f"schema migrations failed on boot (exit code {rc}); "
            f"refusing to start workers against an inconsistent schema"
        )
    await boot.require_keeper(ch)
    await boot.require_storage_authority(ch)
    await boot.warn_newer_schema()
    await boot.warn_collection_named_namespaces()
    # Nothing here writes a column a migration just added while another process
    # still carries its version writes by the older column lists.
    from server.core import rmt_carry

    await rmt_carry.await_acks(ch)
    # This process's heartbeat row, and its check against the deployment's
    # published configuration, before it serves.
    from server.core import instances as _instances
    from server.workers import leader as _leader

    instances_loop = await _instances.start(
        ch,
        _leader.leads_deployment,
        label=_instance_mod.instance_label() or "",
        workers_lease=_leader.advertised_lease,
    )

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

    from server.workers import child as _workers_child

    # Every serving process runs this lifespan; one of the deployment's runs
    # the background workers, the holder of the ClickHouse lease, in a child
    # process of its own unless LOOM_WORKERS_PROCESS=inline.
    lease = await _leader.decide_supervisor(settings.workers_mode())
    supervisor = _workers_child.worker_runner(lease)
    _leader.set_current(lease)
    # Apply per-table retention overrides from LOOM_RETENTION_*_DAYS, if set —
    # from the deployment's leader only, and only where the TTL differs.
    from server.core.model_work_gate import get_model_work_gate
    from server.storage import retention_overrides

    if not get_model_work_gate().closed:
        async with get_model_work_gate().admit():
            await retention_overrides.apply(settings, ch=ch, leader=_leader.leads_deployment())
    # The tracker namespaces are directory rows served live; the
    # leader gives each configured one its row (restricted where listed) at
    # boot, BEFORE any read, so the gate never sees a restricted namespace as
    # an unregistered standard one.
    if _leader.leads_deployment():
        await boot.register_tracker_namespaces(ch)
        boot.publish_deployment_config(ch)
    # This process verifies tokens: it loads the revoked set and the signing
    # keyring now and keeps both fresh.
    auth_loops = await boot.start_token_state()
    overrides_loop = boot.start_runtime_overrides()
    await probe_reader_user(ch)
    from server.insights.app import analysis as _analysis

    # Provisioned and probed in the background: the lease can wait for minutes.
    analysis_loop = _analysis.start_probe_loop()
    tracker_sources_loop = boot.start_tracker_sources_refresher(ch)
    from server.code.app.index import repo_registry as _repo_registry

    code_repos_loop = await _repo_registry.start_refresher(ch)
    from server.code.app.index import mirrors as _mirrors

    _mirrors.start_warmup()  # this host's git mirrors, in the background
    boot.publish_frontend_assets(ch)
    boot.mark_process_start(ch)
    await boot.probe_sandbox()
    # The tracker registry, the runtime overrides and this build's bundle
    # load before the first request (bounded; readiness stays false until the
    # first two do).
    await boot.await_warm_up(
        settings.boot_warmup_timeout_s,
        ("tracker_registry", "runtime_overrides", "frontend_assets"),
    )
    if lease.held:
        supervisor.start_all()
    else:
        logging.getLogger(__name__).info(
            "WorkerSupervisor: not started in pid %s — %s", os.getpid(), lease.reason
        )
    lease_loop = boot.start_lease_loop(lease, supervisor, ch)

    if not get_model_work_gate().closed:
        await boot.provider_report()
        await boot.probe_vector_capabilities(ch)
        await boot.calibrate_similarity_gates()

    provider_refresh = asyncio.create_task(refresh_loop(), name="provider-configuration-refresh")
    # SIGTERM: not ready first, the port closes LOOM_SHUTDOWN_READY_DELAY_S later.
    boot.install_drain_on_sigterm()

    try:
        yield
    finally:
        _readiness.begin_drain()
        provider_refresh.cancel()
        await asyncio.gather(provider_refresh, return_exceptions=True)
        if lease_loop is not None:
            lease_loop.cancel()
        for loop in (*auth_loops, overrides_loop):
            loop.cancel()
        tracker_sources_loop.cancel()
        code_repos_loop.cancel()
        analysis_loop.cancel()
        _mirrors.stop_warmup()
        embedder_calibration.stop_refresh_loop()
        instances_loop.cancel()
        boot.cancel_boot_tasks()
        await _shut_down(ch, lease, supervisor, emitter)
        await boot.close_storage_authority()


app = FastAPI(
    title="Loom Control Plane",
    description=(
        "Shared memory, knowledge, and observability data plane for AI agents "
        "— backed by ClickHouse."
    ),
    version=__version__,
    lifespan=participant_lifespan(lifespan),
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
# parser runs; LOOM_MAX_REQUEST_BYTES, 0 disables). Added first, so it is the
# innermost middleware: the 413 still passes out through the header middleware.
from server.transport.body_limit import BodyLimitMiddleware as _BodyLimit  # noqa: E402

# An upload's route bounds its own body (LOOM_KNOWLEDGE_UPLOAD_MAX_BYTES).
app.add_middleware(
    _BodyLimit, max_bytes=settings.max_request_bytes, own_limit=("/v1/knowledge.upload",)
)

from server.transport.model_maintenance import ModelMaintenanceMiddleware  # noqa: E402

app.add_middleware(ModelMaintenanceMiddleware)

# gzip here (nginx does not gzip proxied bodies): the dashboard bundle and
# JSON bodies above 1 KB.
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=1024)


def _refused(status: int, *, code: str, message: str) -> Response:
    detail = classified({"code": code, "message": message}, status)
    return JSONResponse(status_code=status, content={"detail": detail})


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _own_origin(origin: str, request: Request) -> bool:
    if settings.public_base_url.strip():
        return same_origin(origin, settings.public_base_url.strip().rstrip("/"))
    return origin_matches_host(origin, request.headers.get("host", ""))


def _is_navigation(request: Request) -> bool:
    """A top-level page load, such as following a link to the dashboard from another site."""
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


async def _response_headers(request: Request, call_next: Any) -> Response:
    """Headers every answer leaves with."""
    response: Response = await call_next(request)
    path = request.url.path
    # A year-long immutable cache for content-hashed /assets/* names: the hash is the
    # version, so a load does not re-validate them.
    if path.startswith("/assets/") and response.status_code == 200:
        from server.storage.frontend_assets import cache_control_for

        response.headers["Cache-Control"] = cache_control_for(path)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    # same-origin keeps the real Origin on a same-origin non-GET request, which
    # web_refusal needs; no-referrer would send `Origin: null` on a form POST.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # TLS ends at a terminator in front of Loom; browsers ignore HSTS over plain HTTP.
    if settings.hsts_max_age_seconds > 0:
        response.headers.setdefault(
            "Strict-Transport-Security", f"max-age={settings.hsts_max_age_seconds}"
        )
    # Draining: the balancer's kept-alive connections close one response at a
    # time, so none is still pooled here when the port closes.
    if _readiness.draining():
        response.headers["Connection"] = "close"
    return response


def _attach_cors(target: FastAPI) -> None:
    """CORS, opt-in via LOOM_CORS_ORIGINS (comma-separated exact origins)."""
    if not settings.cors_origins:
        return
    from fastapi.middleware.cors import CORSMiddleware

    target.add_middleware(
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


def attach_browser_middleware(target: FastAPI) -> None:
    """CORS, then the web gate, then the header middleware; each added one wraps the last,
    so the gate's refusals and CORS preflights carry the headers."""
    _attach_cors(target)
    target.middleware("http")(_web_gate)
    target.middleware("http")(_response_headers)


attach_browser_middleware(app)

http_transport.attach(app)
mcp_transport.attach(app)

from server.auth import login as _login  # noqa: E402
from server.auth import oauth as _oauth  # noqa: E402
from server.auth import scim_routes as _scim_routes  # noqa: E402
from server.auth import self_signup as _self_signup  # noqa: E402
from server.identity.app import agent_claim as _agent_claim  # noqa: E402
from server.identity.app import claim_release as _claim_release  # noqa: E402
from server.identity.app import password_link as _password_link  # noqa: E402
from server.identity.app import scim_groups as _scim_groups  # noqa: E402

_self_signup.attach(app)
_login.attach(app)
_claim_release.attach(app)
# Sign-in (device grant, SSO) wherever auth is on; MCP connect only with LOOM_OAUTH_ENABLED.
_oauth.attach(app)
_password_link.attach(app)
_agent_claim.attach(app)
# SCIM Groups, pushed per org with an org admin's token; ahead of the Users
# routes, whose catch-all would answer its paths.
_scim_groups.attach(app)
# SCIM provisioning (deactivating people from the IdP) only with LOOM_SCIM_TOKEN.
_scim_routes.attach(app)

from server.insights.app.admin_routes import admin_router as _admin_router  # noqa: E402
from server.insights.app.admin_routes import org_admin_router as _org_admin_router  # noqa: E402
from server.insights.app.provider_routes import provider_router as _provider_router  # noqa: E402

app.include_router(_admin_router)
app.include_router(_org_admin_router)
app.include_router(_provider_router)

from server.identity.app.admin_account_routes import router as _account_admin_router  # noqa: E402

app.include_router(_account_admin_router)

# The caller API (/v1/me/*); the dashboard page itself attaches last, below.
from server.identity.app import me_routes as _me_handlers  # noqa: E402

_me_handlers.attach(app)

# The task inbox plane's SSE wake-up, GET-shaped like
# knowledge.download (a stream, not a JSON envelope).
from server.tasks.app import watch_routes as _task_watch  # noqa: E402

_task_watch.attach(app)

from server import health as _health  # noqa: E402
from server.auth import routes as _auth_routes  # noqa: E402
from server.knowledge.app import download_routes as _knowledge_download  # noqa: E402
from server.knowledge.app import upload_routes as _knowledge_upload  # noqa: E402

_auth_routes.attach(app)
_health.attach(app)
_knowledge_download.attach(app)
_knowledge_upload.attach(app)


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


# The dashboard's pages: only its views' first path segments, none of which an
# API route uses (tests/unit/test_dashboard_routes.py).
from server.identity.app import dashboard_routes as _dashboard  # noqa: E402

_dashboard.attach(app)
