"""The lifespan's boot and shutdown steps: the serialised boot migration, the
leader's tracker namespace rows, the one-shot boot tasks, the revocation
snapshot, the supervisor lease retry, the provider report, the vector
capability probes, the similarity-gate calibration and the write drain before
the ClickHouse client closes. `server.main.lifespan` runs them in order."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from server.core.background import register_deferred_task
from server.settings import settings


def run_boot_migrations_serialised(migrate: Callable[[list[str]], int], lock_path: str) -> int:
    """Run `migrate([])` while holding an exclusive fcntl lock on `lock_path`.

    Sibling worker processes block until the holder releases, then run the
    same idempotent step and find nothing pending. The lock is advisory and
    host-local: it serialises processes that share a filesystem (the web
    workers of one container), which is exactly the population that races.
    If the lock file cannot be opened the step runs unserialised.
    """
    import fcntl

    try:
        fh = open(lock_path, "a+")
    except OSError as e:
        logging.getLogger("loom.boot").warning(
            "boot migrate lock unavailable (%s): running unserialised", e
        )
        return migrate([])
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            return migrate([])
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# What a process that skipped the boot step is serving without — the pending migration files
# named at boot, carried by /v1/health.
SKIPPED_PENDING_MIGRATIONS: tuple[str, ...] = ()


def run_boot_migrations_if_enabled(
    migrate: Callable[[list[str]], int],
    lock_path: str,
    *,
    enabled: bool = True,
    pending: Callable[[], list[str]] | None = None,
) -> int:
    """The boot-migration step behind `LOOM_BOOT_MIGRATE`.

    Enabled: `run_boot_migrations_serialised`. Disabled: apply nothing and
    return 0 — the process serves whatever schema the service has. The
    switch exists for processes that share a ClickHouse service with the
    deployment but run a branch's code (isolated A/B servers), so a branch's
    not-yet-merged migration never reaches the shared service at boot. The
    deployment never sets it; a process that skips the step logs so at
    WARNING.

    The skip names what it skips. `pending` (read-only,
    `server.core.schema.migrate.pending_names`) lists the repo's migration files the
    service's ledger lacks; they go into the WARNING and into
    `SKIPPED_PENDING_MIGRATIONS` for `/v1/health`, so a server missing a
    column its code expects says so at boot instead of failing silently."""
    global SKIPPED_PENDING_MIGRATIONS
    if enabled:
        SKIPPED_PENDING_MIGRATIONS = ()
        return run_boot_migrations_serialised(migrate, lock_path)
    names: list[str] = []
    if pending is not None:
        try:
            names = [str(n) for n in pending()]
        except Exception as e:  # noqa: BLE001 — a diagnostic, never a boot failure
            logging.getLogger(__name__).warning("pending-migration check failed: %s", e)
    SKIPPED_PENDING_MIGRATIONS = tuple(names)
    log = logging.getLogger(__name__)
    if names:
        log.warning(
            "boot migrations skipped (LOOM_BOOT_MIGRATE=false): %d pending on this service — %s "
            "— every path that reads what they add fails on this process until the deployment "
            "applies them",
            len(names),
            ", ".join(names),
        )
    else:
        log.warning(
            "boot migrations skipped (LOOM_BOOT_MIGRATE=false): this process applies no "
            "schema change; %s",
            "nothing is pending"
            if pending is not None
            else "pending migrations, if any, stay pending",
        )
    return 0


class InsecureBindRefused(RuntimeError):
    """A server others can reach would start without its safeguards."""


def require_safe_bind(
    *,
    bind_host: str,
    public_base_url: str,
    auth_required: bool,
    dev_secret: bool,
    allow_insecure: bool,
) -> None:
    """Refuse to start on a non-loopback LOOM_BIND_HOST without
    LOOM_PUBLIC_BASE_URL (generated URLs would otherwise echo a request's Host),
    or with auth off or the dev signing secret unless LOOM_ALLOW_INSECURE=1.
    A loopback bind is not checked."""
    from server.auth.public_url import is_loopback_host

    if is_loopback_host(bind_host):
        return
    where = f"LOOM_BIND_HOST={bind_host} is reachable by others"
    if not public_base_url.strip():
        raise InsecureBindRefused(
            f"refusing to start: {where} and LOOM_PUBLIC_BASE_URL is unset. Set it to the "
            "address clients use (e.g. https://loom.example), or bind 127.0.0.1."
        )
    if allow_insecure:
        return
    if not auth_required:
        raise InsecureBindRefused(
            f"refusing to start: {where} and auth is off, so anyone who reaches it has full "
            "access. Turn auth on, bind 127.0.0.1, or set LOOM_ALLOW_INSECURE=1 if the port "
            "is not actually reachable."
        )
    if dev_secret:
        raise InsecureBindRefused(
            f"refusing to start: {where} and LOOM_JWT_SECRET is the checked-in dev default, "
            "which anyone can mint accepted tokens with. Set a private LOOM_JWT_SECRET, bind "
            "127.0.0.1, or set LOOM_ALLOW_INSECURE=1 if the port is not actually reachable."
        )


def require_replica_schema() -> None:
    """A read-replica runs against the schema the main Loom applies. A build
    that expects a migration the service lacks would fail on every path that
    reads what it adds, so it does not start: upgrade the main Loom first."""
    if settings.read_replica and SKIPPED_PENDING_MIGRATIONS:
        raise RuntimeError(
            "this read-replica's build expects migrations the shared ClickHouse has not "
            f"applied ({', '.join(SKIPPED_PENDING_MIGRATIONS)}); upgrade the main Loom first"
        )


async def start_replica_config(ch: Any) -> asyncio.Task[None] | None:
    """A read-replica takes the main Loom's published settings before it loads
    providers or serves, then re-reads them on a timer, and checks on a timer
    that the main Loom answers. None on the main Loom."""
    if not settings.read_replica:
        return None
    from server.core import deployment_config

    await deployment_config.load(ch, boot=True)
    loops = [deployment_config.refresh_loop(ch)]
    if str(settings.read_replica_main_url).strip():
        from server.clients import main_loom

        # Whether the main Loom answers, for /v1/health (`replica.main`).
        loops.append(main_loom.probe_loop())
    return asyncio.create_task(_run_together(*loops), name="loom-replica-deployment-config")


async def _run_together(*loops: Coroutine[Any, Any, Any]) -> None:
    """The replica's loops under one task, so shutdown's one cancel stops them all."""
    await asyncio.gather(*loops)


def publish_deployment_config(ch: Any) -> None:
    """The deployment's workers leader publishes its settings for read-replicas.
    A branch or A/B server on the same service (no LOOM_DEPLOYMENT) never does:
    its env is not the deployment's."""
    if settings.read_replica or not settings.deployment:
        return
    from server.core import deployment_config
    from server.observability.instance import instance_label

    _spawn_boot_task(
        deployment_config.publish(ch, published_by=instance_label() or ""),
        name="loom-deployment-config",
    )


async def register_tracker_namespaces(ch: Any) -> None:
    """The leader gives every configured tracker namespace its directory row
    (restricted where listed) — at boot when the lease is held, or when the
    lease is acquired on retry. Never
    fatal: a failed registration logs and the gate keeps refusing. Its
    queries carry their own tag, reset on return: at boot it runs inside the
    lifespan, whose later queries are not registration."""
    from server.core.ch import query_tag_var

    tag = query_tag_var.set("boot.tracker_namespaces")
    try:
        from server.tracker.app import registry_sources
        from server.tracker.app.registry import ensure_configured_tracker_namespaces

        await registry_sources.refresh_db_sources(force=True, ch=ch)
        done = await ensure_configured_tracker_namespaces(ch=ch)
        if done:
            logging.getLogger(__name__).info("qa namespaces registered: %s", done)
    except Exception as e:  # noqa: BLE001 — registration never fails boot; the gate keeps refusing
        logging.getLogger(__name__).warning("qa namespace registration failed: %s", e)
    finally:
        query_tag_var.reset(tag)


async def run_fenced_boot_migrations(
    ch: Any,
    migrate: Callable[[list[str]], int],
    lock_path: str,
    *,
    enabled: bool,
    pending: Callable[[], list[str]],
) -> int:
    """`_fenced_boot_migrations`, then forget the schema state this process
    cached before it: a sibling worker may have applied the migrations while
    this one waited, and the no-op paths return without touching the ledger."""
    rc = await _fenced_boot_migrations(ch, migrate, lock_path, enabled=enabled, pending=pending)
    if rc == 0:
        from server.core.schema.status import reset_cache

        reset_cache()
    return rc


async def _fenced_boot_migrations(
    ch: Any,
    migrate: Callable[[list[str]], int],
    lock_path: str,
    *,
    enabled: bool,
    pending: Callable[[], list[str]],
) -> int:
    """Boot DDL must not race provider snapshot/publication on another process.

    The first install predates provider configuration and needs no provider
    fence. An unfinished transition permits same-schema restarts, not applying
    an upgrade's migrations to tables being replaced underneath it.
    """
    if not enabled:
        return run_boot_migrations_if_enabled(migrate, lock_path, enabled=False, pending=pending)
    if not pending():
        # A no-op restart performs no DDL and needs no provider Keeper lock.
        # In particular, never turn an advisory/fail-open ledger probe into
        # permission to invoke a migration runner that could apply changes.
        return 0
    exists = await ch.query_one(
        "SELECT count() AS n FROM system.tables "
        "WHERE database='loom' AND name='provider_configuration'"
    )
    if exists is None or type(exists.get("n")) is not int or exists["n"] not in (0, 1):
        raise RuntimeError("Cannot establish provider configuration schema before boot DDL")
    if exists["n"] == 0:
        return run_boot_migrations_if_enabled(migrate, lock_path, enabled=True, pending=pending)
    from server.insights.domain.provider_setup import pending_configuration
    from server.insights.store.provider_configuration import configurations
    from server.insights.store.provider_lock import configuration_lock

    # A sibling holds the lock while its migrations run, so wait as long as for
    # the migration lease: failing to start would stop the whole server
    # (uvicorn exits when one worker fails to start).
    log = logging.getLogger(__name__)
    async with configuration_lock(
        wait_s=settings.boot_migrate_lease_wait_s,
        out=lambda m: log.info(m.strip(" ·")),
    ):
        if pending_configuration(await configurations(ch=ch)) is not None:
            raise RuntimeError("Finish the provider rebuild before applying schema migrations")
        return run_boot_migrations_if_enabled(migrate, lock_path, enabled=True, pending=pending)


# One-shot boot tasks nobody awaits. The loop holds only a weak reference to
# a task, so each is kept here until it finishes, and shutdown cancels what is
# still running before the ClickHouse client closes under it.
_BOOT_TASKS: set[asyncio.Task[Any]] = set()


def _spawn_boot_task(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
    task = asyncio.get_running_loop().create_task(coro, name=name)
    register_deferred_task(task)
    _BOOT_TASKS.add(task)
    task.add_done_callback(_BOOT_TASKS.discard)


def cancel_boot_tasks() -> None:
    for task in list(_BOOT_TASKS):
        task.cancel()


async def probe_sandbox() -> None:
    """Probe the repository-job sandbox before serving. `/v1/health` reports only what
    this process has probed, and a stack with no repositories never runs a repository
    job that would probe it, so the first answer would say nothing."""
    if settings.code_ingest_enabled:
        from server.code.app.index.sandbox import sandbox_available

        await asyncio.to_thread(sandbox_available)


def start_tracker_sources_refresher(ch: Any) -> asyncio.Task[None]:
    """Every process — not only the leader — loads the
    self-service tracker registry at boot and keeps it within
    `tracker_sources_refresh_seconds`. The read statements resolve their
    namespace from this snapshot before any read (`TrackerSource.repos`,
    `_privacy`), so a process that never refreshed would answer "not a
    configured tracker namespace" for every tracker added from the page. One
    small read a minute."""

    async def _loop() -> None:
        from server.core.ch import set_query_tag
        from server.tracker.app import registry_sources

        set_query_tag("tracker.sources_refresh")  # its own task: the tag stays in it
        await registry_sources.refresh_db_sources(force=True, ch=ch)
        from server.tracker.app import contract

        # Its own task, so a slow tracker never delays a refresh
        _spawn_boot_task(contract.warm_at_boot(), name="loom-tracker-warm")
        while True:
            await asyncio.sleep(max(5.0, float(settings.tracker_sources_refresh_seconds)))
            await registry_sources.refresh_db_sources()  # never raises; keeps the last snapshot

    return asyncio.create_task(_loop(), name="loom-tracker-sources-refresh")


async def provider_report() -> None:
    """What the two providers resolved to, said once at boot.

    One INFO line naming the LLM provider/model an unset
    LOOM_LLM_PROVIDER was inferred to and from what (silent when explicit).
    Then the embedder dim probe, which catches a LOOM_EMBEDDING_MODEL vs
    LOOM_EMBEDDING_DIM mismatch; it falls back gracefully for a fake/keyless
    embedder.
    """
    from server.embedding import embedder as _embedder_mod
    from server.llm.client import log_llm_provider_resolution
    from server.settings import log_unknown_env_keys

    log_unknown_env_keys()
    log_llm_provider_resolution()
    try:
        await _embedder_mod.probe_embedding_dim()
    except Exception as e:  # noqa: BLE001 — an unreachable embedder never fails boot
        logging.getLogger(__name__).warning(
            "embedding dim probe failed (%s); using settings.embedding_dim",
            e,
        )


async def probe_vector_capabilities(ch: Any) -> None:
    """What this ClickHouse supports for vector reads and inserts, probed once
    before the first search: the two-stage codec setting (whether reads carry
    `vector_search_use_quantized_codes`, which columns are coded and at which
    width, and which tables carry patch parts), the fact leg's patch-part
    reading (seeded so the first search does not wait on it and the gauge has
    a value), and the vector-read version floor. The codec state is read again:
    `connect()` probed it before the boot migrations created a fresh install's
    tables, and the write guards need their widths from the first request."""
    from server.core.vectors import codes_state, patch_parts, vector_floor

    await codes_state.ensure_probed(ch, reread=True)
    await patch_parts.probe_patch_parts(ch)
    await vector_floor.warn_below_vector_floor(ch)


# The embedder the scalar similarity gates were tuned for.
_SCALAR_GATES_MODEL = "text-embedding-3-small"


async def calibrate_similarity_gates() -> None:
    """Derive the similarity gate thresholds from the embedder
    (`LOOM_ADAPTIVE_THRESHOLDS`, on by default). Off: warn when a real
    embedder other than the one the scalar gates were tuned for is in use,
    since those gates are then miscalibrated and recall can silently regress.
    Never fails boot; the scalar gates apply."""
    from server.memory.app import embedder_calibration

    log = logging.getLogger("loom.boot")
    try:
        if settings.adaptive_thresholds:
            calibration = await embedder_calibration.calibrate_embedder()
            embedder_calibration.set_calibration(calibration)
            return
        model = settings.embedding_model
        if "fake" not in model.lower() and model != _SCALAR_GATES_MODEL:
            log.warning(
                "LOOM_ADAPTIVE_THRESHOLDS is off and LOOM_EMBEDDING_MODEL=%s "
                "is not the model the scalar similarity gates were tuned for "
                "(%s). The hardcoded thresholds "
                "(memory_search_min_similarity, llm_rerank_skip_above, "
                "hyde/query-rewrite/abstain) are likely miscalibrated for "
                "this embedder — enable adaptive thresholds or re-bench and "
                "retune before serving production traffic.",
                model,
                _SCALAR_GATES_MODEL,
            )
    except Exception as e:  # noqa: BLE001 — calibration never fails boot; scalar gates apply
        log.warning("calibration failed (%s); falling back to scalar settings", e)


async def start_revocation(lease_held: bool) -> asyncio.Task[None] | None:
    """Every process that verifies tokens loads the
    revocation snapshot at boot; one that does not hold the supervisor lease
    (the web tier under LOOM_BACKGROUND_WORKERS=off, the non-leader uvicorn
    workers) also refreshes it on the worker's cadence, because the
    supervisor's jwt_revocation_refresh runs only where the lease is held;
    without it a non-leader process would accept a revoked token."""
    from server.auth import revocation

    await revocation.load_at_boot()
    if lease_held:
        return None
    return revocation.start_refresh_loop(settings.jwt_revocation_cache_ttl_seconds)


def start_lease_retry(
    lease: Any, supervisor: Any, ch: Any, revocation_loop: asyncio.Task[None] | None
) -> asyncio.Task[None] | None:
    """In `auto` mode keep trying for the lease so the workers
    follow the lock when its holder exits (the old colour of a blue/green
    deploy, a sibling process that died without a respawn). Returns the
    retry task, or None outside `auto`."""
    if settings.background_workers.strip().lower() != "auto":
        return None
    from server.workers import leader as _leader

    def _on_lease_acquired() -> None:
        # The leader's boot duties follow the lease — under blue/green the new
        # colour acquires it here, after the old colour exits, not at boot;
        # the QA directory rows must be written by whoever holds it.
        supervisor.start_all()
        # The supervisor's jwt_revocation_refresh worker takes over.
        if revocation_loop is not None and not revocation_loop.done():
            revocation_loop.cancel()
        _spawn_boot_task(register_tracker_namespaces(ch), name="loom-tracker-namespaces")
        publish_deployment_config(ch)

    return asyncio.create_task(
        _leader.retry_until_held(lease, settings.supervisor_lease_retry_s, _on_lease_acquired),
        name="loom-supervisor-lease-retry",
    )


async def drain_writes() -> None:
    """Every write still in flight lands before the ClickHouse client goes:
    the service-side tasks, then the drop ledger (last of the services: the
    side effects before it record their drops too), the events stream and
    the buffered write marks."""
    # A request cancelled by the shutdown left its Keeper release or its
    # credential put-back running: they land first.
    from server.core import keeper_claims

    await keeper_claims.drain(timeout_s=5.0)
    # In-flight deferred conflict judges settle first.
    from server.memory.app import conflict_deferred, drops

    await conflict_deferred.drain(timeout_s=15.0)
    # Restricted-access audit rows and policy decision rows (an enforced
    # denial among them) still being written land first.
    from server.identity.app import policy_gate as _policy_gate
    from server.identity.app import restricted_access as _restricted_access

    await _restricted_access.drain(timeout_s=5.0)
    await _policy_gate.drain(timeout_s=5.0)
    # A write's projected side effects and the memory reads' telemetry
    # (exposure ledger, recall credit, trace log, verify mirror, ask credit).
    from server.memory.app import after_write as _after_write

    await _after_write.drain(timeout_s=10.0)
    # Coalesced blacklist hit counts.
    from server.memory.app import blacklist_guard as _blacklist

    await _blacklist.flush_hits()
    # Last of the services: the side effects above record their drops too.
    await drops.flush_drops()
    # In-flight events-stream writes finish before the client goes.
    from server.observability import events_stream as _events_stream

    await _events_stream.drain()
    # The buffered write marks land before the client goes.
    from server.core import namespace_write_marks as _marks

    await _marks.flush_now()
