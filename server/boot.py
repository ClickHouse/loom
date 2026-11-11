"""The lifespan's boot and shutdown steps: the fenced boot migration, the
leader's tracker namespace rows, the one-shot boot tasks, the revocation
snapshot and signing keyring, the supervisor lease loop, the provider report, the vector
capability probes, the similarity-gate calibration and the write drain before
the ClickHouse client closes. `server.main.lifespan` runs them in order."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from server.core.background import register_deferred_task
from server.settings import settings

# What a process that skipped the boot step is serving without — the pending migration files
# named at boot, carried by /v1/health.
SKIPPED_PENDING_MIGRATIONS: tuple[str, ...] = ()
# The Postgres files a process with boot migrations off skipped, as `pg:<file>`.
SKIPPED_PG_MIGRATIONS: tuple[str, ...] = ()


def skipped_migrations() -> tuple[str, ...]:
    """Every migration file this process skipped, of either store."""
    return SKIPPED_PENDING_MIGRATIONS + SKIPPED_PG_MIGRATIONS


def run_boot_migrations_if_enabled(
    migrate: Callable[[list[str]], int],
    *,
    enabled: bool = True,
    pending: Callable[[], list[str]] | None = None,
) -> int:
    """The boot-migration step behind `LOOM_BOOT_MIGRATE`.

    Enabled: `migrate([])`, which takes the deployment's migration lock
    (`server.core.schema.migrate_lease`) before applying. Disabled: apply nothing and
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
        return migrate([])
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
    mcp_open: bool,
    open_signup_without_trust_network: bool,
    weak_keys: list[str],
) -> None:
    """Refuse to start on a non-loopback LOOM_BIND_HOST without
    LOOM_PUBLIC_BASE_URL (generated URLs would otherwise echo a request's Host),
    or, unless LOOM_ALLOW_INSECURE=1, with auth off, MCP open (`mcp_open`: every
    tool runs with no principal), self-signup open without
    LOOM_IDENTITY_PROVIDER=trust-network (`open_signup_without_trust_network`:
    anyone who reaches the port gets a writer token), the dev signing secret, or a weak key
    (`weak_keys`: the root secret, gateway or end-user keys below the minimum key size
    of RFC 7518, an HMAC secret under 32 bytes or an RSA key under 2048 bits).
    A loopback bind is not checked."""
    from server.auth import keyring
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
    if mcp_open:
        raise InsecureBindRefused(
            f"refusing to start: {where} and LOOM_MCP_AUTH_MODE=open runs every MCP tool "
            "with no principal, whatever auth the HTTP API requires. Set it to required, "
            "bind 127.0.0.1, or set LOOM_ALLOW_INSECURE=1 if the port is not actually reachable."
        )
    if open_signup_without_trust_network:
        raise InsecureBindRefused(
            f"refusing to start: {where} and LOOM_SELF_SIGNUP=open gives anyone who reaches "
            "it a writer token. Set LOOM_IDENTITY_PROVIDER=trust-network if everyone who can "
            "reach the port may have an account, turn self-signup off, bind 127.0.0.1, or set "
            "LOOM_ALLOW_INSECURE=1 if the port is not actually reachable."
        )
    if dev_secret:
        raise InsecureBindRefused(
            f"refusing to start: {where} and LOOM_JWT_SECRET is the checked-in dev default, "
            "which anyone can mint accepted tokens with. Set a private LOOM_JWT_SECRET, bind "
            "127.0.0.1, or set LOOM_ALLOW_INSECURE=1 if the port is not actually reachable."
        )
    if weak_keys:
        raise InsecureBindRefused(
            f"refusing to start: {where} and {', '.join(weak_keys)} "
            f"{'is' if len(weak_keys) == 1 else 'are'} below the minimum key size (an HMAC "
            f"secret shorter than {keyring.MIN_HMAC_BYTES} bytes or an RSA key under "
            f"{keyring.MIN_RSA_BITS} bits). Use {keyring.MIN_HMAC_BYTES} or more random bytes "
            f"or {keyring.MIN_RSA_BITS}-bit keys, bind 127.0.0.1, or set LOOM_ALLOW_INSECURE=1 "
            "if the port is not actually reachable."
        )


async def warn_collection_named_namespaces() -> list[str]:
    """Log one WARNING naming the first 20 active namespaces that use a
    collection's reserved prefix (`col-`) without being one: a `col-` name not in
    the collection tree admits only a platform admin. Returns the names."""
    from server.core.ch import get_client
    from server.core.principals import COLLECTION_PREFIX

    try:
        rows = await get_client().query_all(
            """
            SELECT namespace FROM loom.namespaces FINAL
            WHERE startsWith(namespace, {prefix:String})
              AND kind NOT IN ('collection', 'personal') AND active
            ORDER BY namespace
            LIMIT 20
            """,
            params={"prefix": COLLECTION_PREFIX},
        )
    except Exception as e:  # noqa: BLE001 — a boot report, never a boot failure
        logging.getLogger(__name__).warning("could not check for col- namespaces: %s", e)
        return []
    names = [str(r["namespace"]) for r in rows]
    if names:
        logging.getLogger(__name__).warning(
            "namespaces named like collections admit only a platform admin (the first 20): %s "
            "(move their data to a new name, or recreate them with collection.create)",
            ", ".join(names),
        )
    return names


async def warn_newer_schema() -> list[str]:
    """Log one WARNING naming the ledger's migrations this build does not
    carry: an older build booting on a newer schema (a rolling upgrade's N-1,
    or a rollback). It serves, under the N-1/N rules of
    docs/architecture/rolling-upgrades.md. Returns the files."""
    from server.core.schema.status import schema_status

    status = await schema_status(ttl_s=0.0)
    newer = list(status.get("ledger_only") or [])
    if newer:
        logging.getLogger(__name__).warning(
            "schema is newer than this build: %d applied migration(s) this build does not "
            "carry (%s); serving as the older build of a rolling upgrade",
            len(newer),
            ", ".join(newer),
        )
    return newer


def publish_deployment_config(ch: Any) -> None:
    """The deployment's workers leader publishes its configuration, which every
    process checks itself against (`server.core.deployment_config`). A branch
    or A/B server on the same service (no LOOM_DEPLOYMENT) never does: its env
    is not the deployment's."""
    if not settings.deployment:
        return
    from server.core import instances

    _spawn_boot_task(instances.publish_config(ch), name="loom-deployment-config")


def publish_frontend_assets(ch: Any) -> None:
    """Every writing replica shares its dashboard bundle, so a page from another
    build finds its `/assets/*` here during a rolling upgrade. Boot waits for
    the first round (`await_warm_up`)."""
    from server.core.paths import FRONTEND_DIST
    from server.observability.instance import instance_label
    from server.storage import frontend_assets

    _spawn_boot_loop(
        frontend_assets.publish_loop(
            FRONTEND_DIST / "assets",
            published_by=instance_label() or "",
            ch=ch,
            first_done=_warm_event("frontend_assets"),
        ),
        name="loom-frontend-assets",
    )


def mark_process_start(ch: Any) -> None:
    """A writing process moves the all-orgs write mark at start: one that died
    lost its buffered marks, and a precedent must not outlive them."""
    from server.core import namespace_write_marks

    _spawn_boot_task(namespace_write_marks.mark_process_start(ch=ch), name="loom-write-marks-start")


async def register_tracker_namespaces(ch: Any) -> None:
    """The leader gives every configured tracker namespace its directory row
    (restricted where listed) — at boot when the lease is held, or when this
    process later acquires the lease. Never fatal: a failed registration logs
    and the gate keeps refusing. A pass that skipped a namespace (its account
    claim busy past its wait) or failed is tried again in the background while
    this process leads, so a narrowing does not wait for the next boot."""
    global _TRACKER_RETRY
    from server.workers import leader

    retry = await _register_tracker_namespaces_once(ch)
    if retry and leader.leads_deployment() and (_TRACKER_RETRY is None or _TRACKER_RETRY.done()):
        _TRACKER_RETRY = _spawn_boot_loop(
            _retry_tracker_namespaces(ch), name="loom-tracker-namespaces-retry"
        )


async def _retry_tracker_namespaces(ch: Any) -> None:
    """Retry every claim TTL: by then a busy holder has finished or its claim can be
    taken over."""
    from server.workers import leader

    while True:
        await asyncio.sleep(_tracker_retry_s())
        if not leader.leads_deployment() or not await _register_tracker_namespaces_once(ch):
            return


def _tracker_retry_s() -> float:
    from server.auth.grant_claims import TTL_S

    return float(TTL_S)


async def _register_tracker_namespaces_once(ch: Any) -> bool:
    """One pass at a time in this process, which would otherwise wait on its own
    claims; True when a namespace was skipped or the pass failed. Its queries carry
    their own tag, reset on return: at boot it runs inside the lifespan, whose later
    queries are not registration."""
    from server.core.ch import query_tag
    from server.tracker.app import registry_sources
    from server.tracker.app.registry import (
        configured_pending,
        ensure_configured_tracker_namespaces,
    )

    async with _TRACKER_PASS:
        with query_tag("boot.tracker_namespaces"):
            try:
                await registry_sources.refresh_db_sources(force=True, ch=ch)
                done = await ensure_configured_tracker_namespaces(ch=ch)
                if done:
                    logging.getLogger(__name__).info("qa namespaces registered: %s", done)
                return configured_pending()
            except Exception as e:  # noqa: BLE001 — registration never fails boot; the gate keeps refusing
                logging.getLogger(__name__).warning("qa namespace registration failed: %s", e)
                return True


async def require_clickhouse_ready() -> None:
    """The ClickHouse preflight, before the first connect (which selects the
    `loom` database): a wrong address, an old server or a missing database
    stops boot with one line that names the fix. Every process needs the DDL
    grants (its workers and provider rebuilds use them);
    one that migrates also creates a missing database where its user may."""
    from server.core.schema import preflight

    try:
        server = await asyncio.to_thread(
            preflight.require_ready,
            need="server",
            migrates=settings.boot_migrate,
        )
    except preflight.PreflightError as e:
        # Logged first, so the reason heads the startup failure's traceback.
        logging.getLogger(__name__).error("ClickHouse preflight: %s", e)
        raise RuntimeError(f"ClickHouse preflight: {e}") from None
    if warning := preflight.warnings(server):
        logging.getLogger(__name__).warning("ClickHouse preflight: %s", warning)


async def require_keeper(ch: Any) -> None:
    """Refuse to serve without a ClickHouse Keeper: every lease and claim is a key of
    the KeeperMap table `loom._migration_lock`, which the migrator creates."""
    from server.core.leases import ENGINE_SQL
    from server.core.schema.migrate_lease import KeeperRequired

    row = await ch.query_one(ENGINE_SQL, {"table": "_migration_lock"})
    if row is None or row.get("engine") != "KeeperMap":
        raise KeeperRequired("loom._migration_lock is not a KeeperMap table")


def require_identity_settings() -> None:
    """Refuse to start with SCIM that could not deactivate anyone, or as a cell without
    what a cell needs. Settings only: run before anything touches a store."""
    from server.auth.scim_routes import require_scim_settings
    from server.core import cell

    require_scim_settings(
        settings.scim_token, settings.oauth_login_provider, settings.auth_actually_required()
    )
    cell.require_settings()


async def require_storage_authority(ch: Any) -> None:
    """The memory authority this replica is configured for, checked against the
    deployment's record (`server.core.pg_profile`); the Postgres profile first opens
    Postgres, applies its migrations and checks its commit durability. Raises to
    refuse the boot."""
    from server.core import cell, pg, pg_profile

    global SKIPPED_PG_MIGRATIONS
    if pg.enabled():
        SKIPPED_PG_MIGRATIONS = tuple(f"pg:{name}" for name in await pg_profile.start())
    await pg_profile.require_authority(ch)
    if pg.enabled():
        # Both stores record it now: the store identity the cell's fingerprint carries,
        # reported before LOOM_CELL is set too, since prepare reads it from then.
        cell.set_deployment_id(await pg_profile.deployment_id())


async def open_storage_authority() -> None:
    """A worker process's Postgres pool; the server process checked the rest."""
    from server.core import pg

    if pg.enabled():
        await pg.open_pool()


async def close_storage_authority() -> None:
    from server.core import pg

    await pg.close_pool()


async def run_fenced_boot_migrations(
    ch: Any,
    migrate: Callable[[list[str]], int],
    *,
    enabled: bool,
    pending: Callable[[], list[str]],
) -> int:
    """`_fenced_boot_migrations`, then forget the schema state this process
    cached before it: a sibling worker may have applied the migrations while
    this one waited, and the no-op paths return without touching the ledger."""
    rc = await _fenced_boot_migrations(ch, migrate, enabled=enabled, pending=pending)
    if rc == 0:
        from server.core.schema.status import reset_cache

        reset_cache()
    return rc


async def _fenced_boot_migrations(
    ch: Any,
    migrate: Callable[[list[str]], int],
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
        return run_boot_migrations_if_enabled(migrate, enabled=False, pending=pending)
    if not pending():
        # A no-op restart performs no DDL and needs no provider Keeper lock.
        # In particular, never turn an advisory/fail-open ledger probe into
        # permission to invoke a migration runner that could apply changes.
        return 0
    exists = await ch.query_one(
        "SELECT count() AS n FROM system.tables "
        "WHERE database=currentDatabase() AND name='provider_configuration'"
    )
    if exists is None or type(exists.get("n")) is not int or exists["n"] not in (0, 1):
        raise RuntimeError("Cannot establish provider configuration schema before boot DDL")
    if exists["n"] == 0:
        return run_boot_migrations_if_enabled(migrate, enabled=True, pending=pending)
    from server.insights.app.provider_barrier import enroll_before_migrations
    from server.insights.domain.provider_setup import pending_configuration
    from server.insights.store.provider_configuration import configurations
    from server.insights.store.provider_lock import configuration_lock

    # Enrolled, a reconfiguration activated after the check waits for this
    # process; the migrations then run under the migrate lease alone. A boot
    # waits for the lock as long as for that lease: one web worker failing to
    # start stops the whole server (uvicorn).
    await enroll_before_migrations(ch)
    async with configuration_lock(wait_s=settings.boot_migrate_lease_wait_s):
        if pending_configuration(await configurations(ch=ch)) is not None:
            raise RuntimeError("Finish the provider rebuild before applying schema migrations")
    return run_boot_migrations_if_enabled(migrate, enabled=True, pending=pending)


# Boot tasks and loops nobody awaits. The loop holds only a weak reference to
# a task, so each is kept here until it finishes, and shutdown cancels what is
# still running before the ClickHouse client closes under it.
_BOOT_TASKS: set[asyncio.Task[Any]] = set()
# The background retry of the tracker namespaces a boot pass skipped.
_TRACKER_RETRY: asyncio.Task[Any] | None = None
_TRACKER_PASS = asyncio.Lock()


def _spawn_boot_task(coro: Coroutine[Any, Any, Any], *, name: str) -> None:
    register_deferred_task(_spawn_boot_loop(coro, name=name))


def _spawn_boot_loop(coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
    """A task that runs for the process's life: cancelled at shutdown, never
    registered as deferred, since a model transition's drain waits for those."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _BOOT_TASKS.add(task)
    task.add_done_callback(_BOOT_TASKS.discard)
    return task


def cancel_boot_tasks() -> None:
    for task in list(_BOOT_TASKS):
        task.cancel()


# The first attempts of the boot loops, which boot awaits before serving.
_WARM: dict[str, asyncio.Event] = {}


def _warm_event(name: str) -> asyncio.Event:
    return _WARM.setdefault(name, asyncio.Event())


async def await_warm_up(timeout_s: float, names: tuple[str, ...]) -> list[str]:
    """Wait (at most `timeout_s` in all) for the first load attempt of each
    loop in `names`; returns the ones still pending. A load that failed or
    timed out does not fail boot: readiness stays false until it lands."""
    events = [_warm_event(n) for n in names]
    try:
        await asyncio.wait_for(asyncio.gather(*(e.wait() for e in events)), timeout=timeout_s)
    except TimeoutError:
        pass
    late = [n for n, e in zip(names, events, strict=True) if not e.is_set()]
    if late:
        logging.getLogger(__name__).warning(
            "boot warm-up: %s not loaded within %.0f s; not ready until they are",
            ", ".join(late),
            timeout_s,
        )
    return late


async def probe_sandbox() -> None:
    """Probe the repository-job sandbox before serving. `/v1/health` reports only what
    this process has probed, and a stack with no repositories never runs a repository
    job that would probe it, so the first answer would say nothing."""
    if settings.code_ingest_enabled or settings.code_sandbox_required:
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
        try:
            await registry_sources.refresh_db_sources(force=True, ch=ch)
        finally:
            _warm_event("tracker_registry").set()
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
    from server.llm.client import log_conflict_resolver_resolution, log_llm_provider_resolution
    from server.settings import log_unknown_env_keys

    log_unknown_env_keys()
    log_llm_provider_resolution()
    log_conflict_resolver_resolution()
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
    a value). The codec state is read again:
    `connect()` probed it before the boot migrations created a fresh install's
    tables, and the write guards need their widths from the first request."""
    from server.core.vectors import codes_state, patch_parts

    await codes_state.ensure_probed(ch, reread=True)
    await patch_parts.probe_patch_parts(ch)


# The embedder the scalar similarity gates were tuned for.
_SCALAR_GATES_MODEL = "text-embedding-3-small"


async def calibrate_similarity_gates() -> None:
    """Derive the similarity gate thresholds from the embedder
    (`LOOM_ADAPTIVE_THRESHOLDS`, on by default). Off: warn when a real
    embedder other than the one the scalar gates were tuned for is in use,
    since those gates are then miscalibrated and recall can silently regress.
    Never fails boot; the scalar gates apply. On: adopt the shared
    calibration (probing only when none exists) and keep it refreshed."""
    from server.memory.app import embedder_calibration

    log = logging.getLogger("loom.boot")
    try:
        if settings.adaptive_thresholds:
            await embedder_calibration.adopt_or_probe()
            embedder_calibration.start_refresh_loop()
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


async def start_cell_state() -> list[asyncio.Task[None]]:
    """A cell's process reads its cell's rows before it serves, then on a timer."""
    from server.core import cell, pg_profile

    if not cell.deployment_id():
        cell.set_deployment_id(await pg_profile.deployment_id())
    await cell.refresh()
    return [cell.start_refresh_loop()]


async def start_token_state() -> list[asyncio.Task[None]]:
    """Every process that verifies tokens loads the revocation snapshot, the
    signing keyring, the end-user issuers' JWKS and the grant watermark at boot
    and re-reads them on their cadence."""
    from server.auth import end_user_keys, keyring, revocation
    from server.core import grant_cache
    from server.core.ch import query_tag

    with query_tag("revocation.refresh"):
        await revocation.load_at_boot()
    with query_tag("auth.keyring_refresh"):
        await keyring.refresh()
    with query_tag("grant_cache.poll"):
        await grant_cache.poll()
    await end_user_keys.refresh()
    loops = await start_cell_state() if settings.cell else []
    return loops + [
        revocation.start_refresh_loop(settings.jwt_revocation_cache_ttl_seconds),
        keyring.start_refresh_loop(),
        end_user_keys.start_refresh_loop(),
        grant_cache.start_poll_loop(settings.acl_watermark_poll_seconds),
    ]


# Set once the runtime overrides were adopted in this process (readiness).
_OVERRIDES_ADOPTED = False


async def adopt_runtime_overrides() -> None:
    """Read loom.runtime_overrides once and apply it: the rate-limit, latency-budget
    and setting overrides."""
    from server.core import runtime_overrides, settings_overrides
    from server.core.ratelimit import get_limiter
    from server.observability.budgets import get_overrides

    snapshot = await runtime_overrides.load()
    get_limiter().adopt(snapshot)
    get_overrides().adopt(snapshot)
    settings_overrides.adopt(snapshot)


def start_runtime_overrides() -> asyncio.Task[None]:
    """Every process adopts the deployment's runtime overrides
    (loom.runtime_overrides) now and on a timer."""

    async def _loop() -> None:
        global _OVERRIDES_ADOPTED
        from server.core.ch import set_query_tag

        set_query_tag("runtime_overrides.refresh")
        while True:
            try:
                await adopt_runtime_overrides()
                _OVERRIDES_ADOPTED = True
            except Exception as e:  # noqa: BLE001 — keep the overrides already adopted; retry
                logging.getLogger(__name__).warning("runtime overrides not adopted: %s", e)
            finally:
                _warm_event("runtime_overrides").set()
            await asyncio.sleep(max(1.0, float(settings.runtime_overrides_refresh_seconds)))

    return asyncio.create_task(_loop(), name="loom-runtime-overrides")


def _overrides_ready() -> Any:
    from server.core import readiness

    return readiness.result(_OVERRIDES_ADOPTED, "runtime overrides not loaded")


async def _apply_retention_overrides(ch: Any) -> None:
    """LOOM_RETENTION_*_DAYS, applied by a process that became the leader
    after boot (the boot leader applies them in the lifespan)."""
    from server.core.model_work_gate import get_model_work_gate
    from server.storage import retention_overrides

    if get_model_work_gate().closed:
        return
    async with get_model_work_gate().admit():
        await retention_overrides.apply(settings, ch=ch, leader=bool(settings.deployment))


def start_lease_loop(lease: Any, supervisor: Any, ch: Any) -> asyncio.Task[None] | None:
    """In `auto` mode keep the supervisor lease: the workers start where it is
    acquired (another process's holder died or exited) and stop where it is
    lost. A cell's `on` process runs the loop too, with no lease: its cell's gate
    starts and stops the workers. Returns the loop task, or None otherwise."""
    if lease.mode != "auto" and not (lease.mode == "on" and settings.cell):
        return None
    from server.workers import leader as _leader

    def _on_acquired() -> None:
        # The leader's boot duties follow the lease.
        supervisor.start_all()
        # As at boot: only the deployment's leader registers them (a cell's `on`
        # process outside LOOM_DEPLOYMENT is not one).
        if _leader.leads_deployment():
            _spawn_boot_task(register_tracker_namespaces(ch), name="loom-tracker-namespaces")
        _spawn_boot_task(_apply_retention_overrides(ch), name="loom-retention-overrides")
        publish_deployment_config(ch)

    async def _on_lost() -> None:
        await supervisor.stop_all(drain_timeout_s=_leader.drain_timeout_s())

    return asyncio.create_task(
        _leader.keep_lease(lease, _on_acquired, _on_lost), name="loom-supervisor-lease"
    )


async def drain_writes() -> None:
    """Every write still in flight lands before the ClickHouse client goes:
    the service-side tasks, then the drop ledger (last of the services: the
    side effects before it record their drops too), the events stream and
    the buffered write marks."""
    # A request cancelled by the shutdown left its Keeper release, its credential
    # put-back or a 'high' row's fence running: they land first, the fence's drop too.
    from server.core import shielded

    if left := await shielded.drain(timeout_s=5.0):
        logging.getLogger(__name__).warning("shutdown: writes left running: %s", ", ".join(left))
    # Restricted-access audit rows and policy decision rows (an enforced
    # denial among them) still being written land first.
    from server.identity.app import policy_gate as _policy_gate
    from server.identity.app import restricted_access as _restricted_access
    from server.memory.app import drops

    await _restricted_access.drain(timeout_s=5.0)
    await _policy_gate.drain(timeout_s=5.0)
    # A write's projected side effects and the memory reads' telemetry
    # (exposure ledger, recall credit, trace log, verify mirror, ask credit).
    from server.memory.app import after_write as _after_write

    await _after_write.drain(timeout_s=10.0)
    # Send the queued publications; what misses its lease is the delivery worker's.
    from server.memory.store import pg_batch

    await pg_batch.drain(timeout_s=5.0)
    # The point-write intents those follows finished, resolved in one flush.
    from server.memory.app import intent_marks as _intent_marks

    await _intent_marks.drain()
    # The ends of the graph projections just drained: one left pending holds every
    # walk of its namespace on every process for its full wait.
    from server.memory.app import graph_write_marks as _graph_marks

    await _graph_marks.drain(timeout_s=5.0)
    # Buffered usage labels (used / harm / acted), some recorded by the side
    # effects just drained.
    from server.memory.store import usage as _usage

    await _usage.drain()
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


# --- readiness, drain and the workers process ---------------------------------

# The lifespan's shutdown drains (supervisor, write drains, judge, scores,
# emitter, HTTP pool) together get at most this; participant retirement and
# the client close run after it whatever happened. With the ready delay and
# uvicorn's graceful timeout it sizes `stop_grace_period` (docs/operations/deployment.md).
SHUTDOWN_DRAIN_BUDGET_S = 40.0


# Of the final cleanup's budget, what retirement leaves for the withdrawal and the
# client close.
_CLOSE_S = 2.0


async def leave_deployment(ch: Any, *, budget_s: float) -> None:
    """The last of a shutdown, under one deadline: retire the provider participant
    (settling cut-off writes while it retries), withdraw the carry acknowledgement,
    close the client. Past the deadline the acknowledgement and the participant row
    are left to expire, never waited on."""
    from server.core import rmt_carry
    from server.insights.app.provider_barrier import retire_at_shutdown

    deadline = time.monotonic() + budget_s
    await retire_at_shutdown(ch, budget_s=max(0.0, budget_s - _CLOSE_S))
    for step in (rmt_carry.withdraw, _disconnect):
        try:
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                await step(ch)
        except Exception as e:  # noqa: BLE001 — the process exits either way
            logging.getLogger(__name__).warning("shutdown: %s cut off (%s)", step.__name__, e)


async def _disconnect(ch: Any) -> None:
    await ch.disconnect()


def register_readiness_checks(ch: Any) -> None:
    """The checks `/v1/health/ready` runs (server/core/readiness.py). Each
    module that keeps a piece of warm-up state owns its check; ClickHouse and
    the ledger are cached."""
    from server.assistant.app import service as assistant_service
    from server.auth import keyring, revocation
    from server.code.app.index import mirrors, repo_registry, sandbox
    from server.core import deployment_config, grant_cache, model_work_gate, pg_profile, readiness
    from server.memory.app import embedder_calibration
    from server.tracker.app import registry_sources

    async def _clickhouse() -> readiness.CheckResult:
        row = await ch.query_one("SELECT 1 AS one")
        return readiness.result(bool(row and row.get("one") == 1), "SELECT 1 did not answer 1")

    async def _migrations() -> readiness.CheckResult:
        from server.core.schema.status import schema_status

        global SKIPPED_PG_MIGRATIONS
        if f"pg:{pg_profile.LEDGER_UNREADABLE}" in SKIPPED_PG_MIGRATIONS:
            # The boot could not read the ledger: read it again on each probe, as the
            # ClickHouse ledger is.
            SKIPPED_PG_MIGRATIONS = tuple(f"pg:{n}" for n in await pg_profile.pending_files())
        pending = SKIPPED_PENDING_MIGRATIONS or tuple((await schema_status()).get("pending") or ())
        pending += SKIPPED_PG_MIGRATIONS
        return readiness.result(not pending, "pending: " + ", ".join(map(str, pending)))

    readiness.register("clickhouse", _clickhouse, ttl_s=2.0)
    readiness.register("migrations", _migrations, ttl_s=5.0)
    readiness.register("revocation", revocation.readiness_check)
    readiness.register("keyring", keyring.readiness_check)
    readiness.register("grant_watermark", grant_cache.readiness_check, required=False)
    readiness.register("tracker_registry", registry_sources.readiness_check)
    readiness.register("code_registry", repo_registry.readiness_check, required=False)
    readiness.register("runtime_overrides", _overrides_ready)
    readiness.register("calibration", embedder_calibration.readiness_check)
    readiness.register("config_drift", deployment_config.readiness_check)
    if settings.cell:
        from server.core import cell

        readiness.register("cell", cell.readiness_check)
    readiness.register("sandbox", sandbox.readiness_check)
    # Reported, not required: a provider rebuild closes the gate on every
    # replica at once, and an LB with no ready backend would cut off the
    # /v1/admin/models routes that finish it (the middleware keeps them open).
    readiness.register("model_work_gate", model_work_gate.readiness_check, required=False)
    # Only where an assistant is configured: a missing check means none is.
    if settings.assistant_librechat_url:
        readiness.register("assistant", assistant_service.readiness_check, required=False)
    if settings.ready_requires_mirrors:
        readiness.register("mirrors", mirrors.readiness_check, ttl_s=5.0)


def install_drain_on_sigterm() -> bool:
    """Put a not-ready phase in front of uvicorn's stop. The first SIGTERM
    marks the process draining (`/v1/health/ready` answers 503, responses
    close their connection, and the lease loop stops the workers and gives
    the lease up: `server/workers/leader.py`); uvicorn gets the signal
    `LOOM_SHUTDOWN_READY_DELAY_S` later and only then closes the port. A
    second SIGTERM or a SIGINT stops at once. Installed only over the handler
    of a `uvicorn.Server` (not in the workers child, not under a test client)."""
    import signal
    import threading

    import uvicorn

    from server.core import readiness

    if threading.current_thread() is not threading.main_thread():
        return False
    prev = signal.getsignal(signal.SIGTERM)
    if not callable(prev) or not isinstance(getattr(prev, "__self__", None), uvicorn.Server):
        return False
    loop = asyncio.get_running_loop()
    delay = float(settings.shutdown_ready_delay_s)
    log = logging.getLogger("loom.boot")

    def _handler(sig: int, frame: Any) -> None:
        already = readiness.draining()
        readiness.begin_drain()
        if already or delay <= 0:
            prev(sig, frame)
            return
        log.info("SIGTERM: not ready; serving %.0f s more before stopping", delay)
        loop.call_soon_threadsafe(loop.call_later, delay, prev, sig, frame)

    signal.signal(signal.SIGTERM, _handler)
    return True
