"""`loom doctor` and `loom report`: the pre-boot and live checks, the
problem list /v1/health carries, and the report body."""

from __future__ import annotations

import argparse
import functools
import json
import sys
from typing import Any

from server import __version__
from server.core.ch_names import database, qualify, quote


def _version() -> str:
    return __version__


# Doctor grades. PASS and WARN exit 0; only FAIL exits 1 —
# a warning is a configuration the operator should look at, not a broken boot.
PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


def _grade(ok: bool) -> str:
    return PASS if ok else FAIL


def dev_jwt_secret_warning() -> str | None:
    """The one sentence `loom mint-token` prints to stderr and `loom doctor`
    grades WARN when the signing secret is the checked-in dev default while
    auth is on. ``None`` when there is nothing to say: a real
    secret, or single-user mode (no bearer is checked, so the secret is moot)."""
    from server.settings import DEV_JWT_SECRET, settings

    if not settings.auth_actually_required():
        return None
    if not settings.jwt_secret_is_dev_default():
        return None
    return (
        f"LOOM_JWT_SECRET is the dev default ({DEV_JWT_SECRET!r}) with auth on — "
        "anyone who has read this repo can mint tokens this server accepts. "
        "Set LOOM_JWT_SECRET before exposing it."
    )


def _doctor_llm_provider_check() -> tuple[str, str, str]:
    """The `llm_provider` doctor line.

    PASS names the resolved provider/model and the evidence behind it (an
    explicit LOOM_LLM_PROVIDER, the model name, or the key present). WARN when
    the provider is keyed but its key is not in the environment: an inferred
    `openai` from `LOOM_LLM_MODEL=gpt-6-luna` with no OPENAI_API_KEY runs
    extraction on the fake client and yields zero facts. An explicit `fake` is
    a PASS: the operator chose it.
    """
    from server.core.llm_provider import KEY_ENV_BY_PROVIDER
    from server.llm.client import provider_key_missing
    from server.settings import settings

    provider, model = settings.llm_provider, settings.llm_model
    how = (
        f"inferred from {settings.llm_provider_source()}"
        if settings.llm_provider_inferred()
        else "set by LOOM_LLM_PROVIDER"
    )
    if provider_key_missing(provider):
        key_env = KEY_ENV_BY_PROVIDER.get(provider, "its API key")
        return (
            "llm_provider",
            WARN,
            f"{provider} ({how}) but {key_env} is not set — extraction and "
            "memory.ask run on the fake LLM and extract nothing. Set "
            f"{key_env}, or LOOM_LLM_PROVIDER=fake to make the fake explicit.",
        )
    return ("llm_provider", PASS, f"{provider} / {model} ({how})")


# The erasure saga's ClickStack leg matches OTel rows by a Map
# value (`Attributes['loom.namespace'] = …`); a `mapValues(...)` bloom filter
# turns that from a full scan into a granule read. The collector's default
# logs/traces tables carry one, the metrics tables do not, so an erasure can
# read GiBs of metrics to find its own footprint.


def _ch_http_param(value: Any) -> str:
    """A query parameter as the HTTP interface reads it: a scalar is its text,
    an array is ClickHouse's own literal — `['a','b']`, single quotes,
    backslash-escaped — not JSON (a JSON-encoded Array(String) answers 400)."""
    if isinstance(value, (list, tuple)):
        items = ("'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'" for v in value)
        return "[" + ",".join(items) + "]"
    return str(value)


def _doctor_ch_endpoint(settings: Any) -> tuple[str, tuple[str, str] | None]:
    """The ClickHouse HTTP base URL and basic-auth pair the doctor probes with
    (no pair without a password), resolved as the server's client resolves
    them: an unset `ch_secure` means HTTPS on 8443 only."""
    from server.core.ch_target import ch_secure

    secure = ch_secure(settings)
    base = f"{'https' if secure else 'http'}://{settings.ch_host}:{settings.ch_port}"
    auth = (str(settings.ch_user), str(settings.ch_password)) if settings.ch_password else None
    return base, auth


def _doctor_ch_query(settings: Any, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """One read-only statement over the HTTP interface, JSONEachRow rows; the
    doctor runs before boot and holds no client. It carries the same
    `log_comment` a CHClient query would (`cli.doctor`), as a URL setting."""
    import json as _json

    import httpx

    from server.core.ch import query_settings

    base, auth = _doctor_ch_endpoint(settings)
    query = {k: str(v) for k, v in query_settings().items()}
    query.update({f"param_{k}": _ch_http_param(v) for k, v in params.items()})
    with httpx.Client(timeout=10.0) as c:
        r = c.post(
            f"{base}/", params=query, content=qualify(sql), auth=auth or httpx.USE_CLIENT_DEFAULT
        )
    r.raise_for_status()
    return [_json.loads(line) for line in r.text.splitlines() if line.strip()]


def _doctor_otel_erasure_index_check(settings: Any) -> tuple[str, str, str]:
    """The `otel_erasure_index` doctor line: WARN naming every
    OTel metrics table in the ClickStack database without a
    `mapValues(Attributes)` skip index — each erasure full-scans it. PASS
    when the leg is off or every table present carries the index."""
    from server.erasure.store.health import OTEL_INDEX_SQL, OTEL_METRICS_TABLES

    db = str(settings.clickstack_database)
    if not db:
        return ("otel_erasure_index", PASS, "skipped (LOOM_CLICKSTACK_DATABASE unset)")
    try:
        rows = _doctor_ch_query(
            settings,
            OTEL_INDEX_SQL + " FORMAT JSONEachRow",
            {"db": db, "tables": list(OTEL_METRICS_TABLES)},
        )
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return ("otel_erasure_index", WARN, f"could not read system.data_skipping_indices: {e}")
    missing = sorted(str(r["name"]) for r in rows if not int(r.get("n") or 0))
    if not missing:
        return (
            "otel_erasure_index",
            PASS,
            f"{len(rows)} metrics table(s) carry mapValues(Attributes)",
        )
    return (
        "otel_erasure_index",
        WARN,
        f"{db}.{', '.join(missing)} have no mapValues(Attributes) bloom filter — every "
        "erasure full-scans them (GiBs per saga). Fix: see ClickStack erasure index in "
        "docs/operations/maintenance.md "
        "(ALTER TABLE … ADD INDEX idx_attr_value mapValues(Attributes) TYPE bloom_filter, "
        "then MATERIALIZE INDEX).",
    )


def _doctor_ch_reachable(settings: Any, ch_host: str) -> tuple[bool, str]:
    """Whether ClickHouse answers /ping at LOOM_CH_HOST, and what to print."""
    from server.core.schema.preflight import host_problem

    if problem := host_problem(ch_host):
        return False, problem
    base, auth = _doctor_ch_endpoint(settings)
    try:
        import httpx

        # 10s covers serverless cold-start without dragging out a real
        # outage's failure feedback.
        with httpx.Client(timeout=10.0) as c:
            r = c.get(f"{base}/ping", auth=auth)
    except Exception as e:  # noqa: BLE001 — doctor reports any probe failure
        # The probed host is the first thing an operator needs on a
        # timeout: it tells a wrong `.env` from a blocked path.
        return False, f"{base} → {type(e).__name__}: {e}"
    ok = r.status_code == 200
    return ok, f"{base} → HTTP {r.status_code}" + ("" if ok else " (expected 200)")


def _doctor_preflight_check() -> tuple[str, str, str]:
    """The `clickhouse_preflight` line: FAIL with the fix where boot would
    refuse this ClickHouse; WARN, with every reason, when boot will create the
    database (the doctor never does), a telemetry table is unreadable or the
    next migration would refuse; PASS otherwise. A missing Keeper has its own line."""
    from server.core.schema import preflight
    from server.settings import settings

    try:
        server = preflight.require_ready(
            need="server",
            create_database=False,
            migrates=settings.boot_migrate,
        )
    except preflight.PreflightError as e:
        return ("clickhouse_preflight", FAIL, str(e))
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return ("clickhouse_preflight", FAIL, f"{type(e).__name__}: {e}")
    warnings = []
    if not server.database:
        warnings.append(f"database {quote(database())} does not exist yet; boot will create it")
    if line := preflight.warnings(server):
        warnings.append(line)
    if not warnings:
        return (
            "clickhouse_preflight",
            PASS,
            f"ClickHouse {server.version}, database {quote(database())} and its grants in place",
        )
    return ("clickhouse_preflight", WARN, f"ClickHouse {server.version}: " + "; ".join(warnings))


def _doctor_keyring_check(settings: Any) -> tuple[str, str, str]:
    """The `signing_keyring` doctor line, read with this host's root secret:
    the kid that signs, kids that do not unseal here, and whether the
    keyring was recorded under another root (`server.auth.keyring`)."""
    from datetime import datetime

    from server.auth import keyring

    try:
        rows = _doctor_ch_query(settings, keyring.READ_SQL + " FORMAT JSONEachRow", {})
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return ("signing_keyring", WARN, f"could not read loom.signing_keys: {e}")
    for r in rows:
        r["activate_at"] = datetime.fromisoformat(r["activate_at"])
        r["server_now"] = datetime.fromisoformat(r["server_now"])
        r["retired"] = bool(int(r["retired"]))
    st = keyring.status(rows)
    problems = []
    if st["root_row"] == "other":
        problems.append("LOOM_JWT_SECRET here is not the root the keyring was recorded under")
    if st["unsealable"]:
        problems.append(f"{', '.join(st['unsealable'])} do not unseal with this LOOM_JWT_SECRET")
    if not st["signing_kid"]:
        why = (
            "'default' is retired"
            if any(r["kid"] == keyring.ROOT_KID and r["retired"] for r in rows)
            else "LOOM_JWT_SECRET is neither an HMAC secret nor an RSA private key,"
        )
        problems.append(f"no key signs: {why} and no other key is active")
    if problems:
        return ("signing_keyring", WARN, "; ".join(problems))
    return ("signing_keyring", PASS, f"signs as {st['signing_kid']}; {len(rows)} row(s)")


# The attempt kinds keyed by the caller's address (`server.auth.attempts.client_address`).
_ADDRESS_KINDS = ("login.address", "signup.human", "signup.agent")
# Fewer address-keyed attempts than this in the window is too little to judge by.
_PROXY_MIN_ATTEMPTS = 20
_LOOPBACK = ("127.0.0.1", "::1")


def _doctor_scim_token_check(settings: Any) -> tuple[str, str, str]:
    """The `scim_token` line: FAIL when the server would refuse to start with it."""
    from server.auth.scim import tokens
    from server.auth.scim_routes import scim_settings_problem

    problem = scim_settings_problem(
        settings.scim_token, settings.oauth_login_provider, settings.auth_actually_required()
    )
    if problem:
        return ("scim_token", FAIL, problem)
    count = len(tokens(settings.scim_token))
    return ("scim_token", PASS, f"{count} token(s) set" if count else "SCIM off")


def _doctor_trusted_proxies_check(settings: Any) -> tuple[str, str, str]:
    """The `trusted_proxies` doctor line: WARN when LOOM_TRUSTED_PROXIES is the
    default and the last day's address-keyed attempts all came from one
    non-loopback peer, which is a load balancer whose X-Forwarded-For is ignored:
    every client then shares one sign-in and sign-up limit."""
    from server.auth.attempts import _digest

    trusted = str(settings.trusted_proxies).strip()
    if trusted != "127.0.0.1":
        return ("trusted_proxies", PASS, f"LOOM_TRUSTED_PROXIES={trusted}")
    sql = (
        "SELECT sum(c) AS n, max(c) AS top_n, argMax(k, c) AS top FROM ("
        "SELECT key_sha256 AS k, count() AS c FROM loom.auth_attempts "
        "WHERE kind IN {kinds:Array(String)} AND at > now64(3) - toIntervalHour(24) "
        "GROUP BY k) FORMAT JSONEachRow"
    )
    try:
        rows = _doctor_ch_query(settings, sql, {"kinds": list(_ADDRESS_KINDS)})
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return ("trusted_proxies", WARN, f"could not read loom.auth_attempts: {e}")
    row = rows[0] if rows else {}
    n, top_n = int(row.get("n") or 0), int(row.get("top_n") or 0)
    top = str(row.get("top") or "")
    if n < _PROXY_MIN_ATTEMPTS or top_n < n or top in {_digest(a) for a in _LOOPBACK}:
        return ("trusted_proxies", PASS, f"default; {n} address-keyed attempt(s) in 24 h")
    return (
        "trusted_proxies",
        WARN,
        f"all {n} sign-in/sign-up attempts in 24 h came from one non-loopback address while "
        "LOOM_TRUSTED_PROXIES is the default: behind a load balancer every client shares its "
        "limits. Set LOOM_TRUSTED_PROXIES to the balancer's addresses or CIDRs.",
    )


def _doctor_keeper_check(settings: Any) -> tuple[str, str, str]:
    """The `keeper` doctor line: FAIL without a ClickHouse Keeper (Loom's leases and
    claims are KeeperMap keys), WARN before `loom migrate` made the lock table or
    when `graph_projections` is not the KeeperMap table its migration makes."""
    from server.core.schema.migrate_lease import KeeperRequired

    sql = (
        "SELECT database, name, engine FROM system.tables WHERE "
        "(database = {db:String} AND name IN ('graph_projections', '_migration_lock')) "
        "OR (database = 'system' AND name = 'zookeeper_connection') FORMAT JSONEachRow"
    )
    try:
        rows = _doctor_ch_query(settings, sql, {"db": database()})
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return ("keeper", WARN, f"could not read system.tables: {e}")
    tables = {(str(r.get("database")), str(r.get("name"))): str(r.get("engine")) for r in rows}
    if ("system", "zookeeper_connection") not in tables:
        return ("keeper", FAIL, str(KeeperRequired("the server has no Keeper configured")))
    if (database(), "_migration_lock") not in tables:
        return (
            "keeper",
            WARN,
            "Keeper present; loom._migration_lock is missing: run `loom migrate`",
        )
    engine = tables.get((database(), "graph_projections"))
    if engine is not None and engine != "KeeperMap":
        return (
            "keeper",
            WARN,
            f"loom.graph_projections is a {engine} (a part per memory.set): drop it and run "
            "the KeeperMap statement of 034_graph_projections.sql",
        )
    return ("keeper", PASS, "Keeper present; leases and claims are KeeperMap keys")


# Tables whose guarantees rest on a sequentially consistent read seeing every
# acknowledged insert: the sign-in/sign-up limits, the single signing-key retire,
# task.create's idempotency key, the single coverage ready event, the bucket
# walk and user erasure of document originals, and a knowledge delete's read after a
# restore (under the Postgres authority also the read that drops a head ClickHouse no
# longer holds, an erasure's read of the document ids it refuses afterwards and the
# originals sweep's ClickHouse read after its read of the unpublished commits).
_SEQUENTIAL_TABLES = (
    "auth_attempts",
    "signing_keys",
    "tasks",
    "task_events",
    "document_blobs",
    "blob_uploads",
    "documents",
)


def _doctor_replica_consistency_check(settings: Any) -> tuple[str, str, str]:
    """The `replica_consistency` doctor line: WARN on a self-managed
    ReplicatedMergeTree cluster with more than one replica of those tables. A
    sequential read there waits only for inserts written with `insert_quorum`,
    which Loom's async inserts cannot use, so a recount, a retire, a task.create
    retry, a ready-event check, the document bucket's walk or user erasure, a
    knowledge delete's read after a restore, a knowledge head's check, an erasure's
    evidence read, or the originals sweep's read on one replica can miss another's
    write.
    SharedMergeTree and one server are exact."""
    sql = (
        "SELECT table, total_replicas FROM system.replicas WHERE database = {db:String} "
        "AND table IN {tables:Array(String)} AND total_replicas > 1 AND table IN ("
        "SELECT name FROM system.tables WHERE database = {db:String} "
        "AND engine LIKE 'Replicated%') FORMAT JSONEachRow"
    )
    try:
        rows = _doctor_ch_query(
            settings, sql, {"db": database(), "tables": list(_SEQUENTIAL_TABLES)}
        )
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return ("replica_consistency", WARN, f"could not read system.replicas: {e}")
    if not rows:
        return ("replica_consistency", PASS, "sequential reads see every acknowledged insert")
    tables = ", ".join(f"loom.{r['table']} ({r['total_replicas']} replicas)" for r in rows)
    return (
        "replica_consistency",
        WARN,
        f"{tables}: a sign-in limit, a signing-key retire, a task.create retry, a "
        "coverage ready event, the document bucket's walk or user erasure, a knowledge "
        "delete's read after a restore, a knowledge head's check, an erasure's evidence "
        "read, or the originals sweep's read on one replica can miss another's write. "
        "Send every Loom process to one replica.",
    )


def _doctor_ch_read_checks(settings: Any | None) -> list[tuple[str, str, str]]:
    """The doctor lines that read ClickHouse; skipped (`settings` None) when it
    did not answer."""
    if settings is None:
        return [
            (name, PASS, "skipped (ClickHouse not reachable)")
            for name in (
                "otel_erasure_index",
                "signing_keyring",
                "trusted_proxies",
                "keeper",
                "replica_consistency",
            )
        ]
    return [
        _doctor_otel_erasure_index_check(settings),
        _doctor_keyring_check(settings),
        _doctor_trusted_proxies_check(settings),
        _doctor_keeper_check(settings),
        _doctor_replica_consistency_check(settings),
    ]


_GRADE_BY_SEVERITY = {"error": FAIL, "warn": WARN, "info": PASS}


def _doctor_reader_probe(settings: Any) -> tuple[str, str]:
    """Whether ClickHouse accepts the boot probe's read as LOOM_CH_READER_USER,
    under the caller-shaped caps: `("ok", "")`, `("failing", <error>)`, or
    `("not_probed", <reason>)`. The reason is empty when the user is unset or
    ClickHouse does not answer (`clickhouse_reachable` says so), and names the
    missing table before migrations created loom.memory."""
    import httpx

    from server.core.ch import (
        BEFORE_MIGRATIONS_CODES,
        READER_PROBE_SQL,
        caller_shaped,
        statement_settings,
    )
    from server.core.ch_errors import error_code

    if not settings.ch_reader_user or not settings.ch_host:
        return "not_probed", ""
    base, _ = _doctor_ch_endpoint(settings)
    auth = (str(settings.ch_reader_user), str(settings.ch_password))
    with caller_shaped():
        query = {k: str(v) for k, v in statement_settings(READER_PROBE_SQL).items()}
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(f"{base}/", params=query, content=qualify(READER_PROBE_SQL), auth=auth)
    except httpx.HTTPError:
        return "not_probed", ""
    if r.status_code != 200:
        if error_code(r.text) in BEFORE_MIGRATIONS_CODES:
            return "not_probed", "reader user not probed: loom.memory missing"
        return "failing", r.text.strip()
    if r.text.split("\t")[0] != settings.ch_reader_user:
        return "failing", ""
    return "ok", ""


@functools.cache  # `--json` builds the problem list twice; read once
def _doctor_tier_failures() -> tuple[Any, ...] | None:
    """The failing code-index tiers as `/v1/health` reports them, read once from
    ClickHouse; None when they cannot be read (ClickHouse down or slow past 5 s, or the
    registry unread): the doctor then says nothing of them."""
    import asyncio

    from server.code.app.index import repo_registry, tier_health
    from server.core import settings_overrides
    from server.core.ch import get_client
    from server.settings import settings

    if not settings.code_intel_enabled or not settings.ch_host:
        return None  # nothing runs a tier, or no ClickHouse to read

    async def read() -> tuple[Any, ...] | None:
        client = get_client()
        try:
            await client.connect()
            if not repo_registry.registry_read():
                return None  # which entries are configured is unknown: say nothing
            await settings_overrides.refresh()  # the thresholds `/v1/health` grades with
            await tier_health.refresh_failing()
            return tier_health.problem_rows()
        finally:
            await client.disconnect()

    _read_code_registry()  # the self-service entries too, once per doctor run
    try:
        # bounded: with ClickHouse down, `clickhouse_reachable` already says so
        return asyncio.run(asyncio.wait_for(read(), timeout=5))
    except Exception:  # noqa: BLE001 — a doctor line must never crash the doctor
        return None


def _read_code_registry() -> None:
    """Read the self-service registry once, bounded; a failure leaves it unread."""
    import asyncio

    from server.code.app.index import repo_registry
    from server.core.ch import get_client
    from server.settings import settings

    if repo_registry.registry_read() or not settings.ch_host:
        return

    async def read() -> None:
        client = get_client()
        try:
            await client.connect()
            await repo_registry.refresh_db_repos(force=True)
        finally:
            await client.disconnect()

    try:
        asyncio.run(asyncio.wait_for(read(), timeout=5))
    except Exception:  # noqa: BLE001 — a doctor line must never crash the doctor
        return


def _doctor_problems() -> list[Any]:
    """The shared problem list as the doctor sees it: settings, and the failing
    code-index tiers read once from ClickHouse — no probed dim, breaker, lease or
    ledger before boot. When that read runs (code intelligence on, ClickHouse and the
    registry read) it first adopts the deployment's runtime setting overrides, so every
    problem is graded as the server grades it; otherwise the environment's values.
    Empty when settings do not load (the `settings_load` check already says so)."""
    try:
        from server.code.app.index.sandbox import layout_refusal, sandbox_available
        from server.observability.problems import HealthState, Problem, collect_problems
        from server.settings import settings

        tiers = _doctor_tier_failures()  # first: it adopts the runtime overrides
        status, detail = _doctor_reader_probe(settings)
        problems = collect_problems(
            settings,
            HealthState(
                repo_sandbox=sandbox_available(),
                repo_sandbox_refusal=layout_refusal(),
                ch_reader_ok={"ok": True, "failing": False}.get(status),
                ch_reader_error=detail if status == "failing" else "",
                code_tier_failures=tiers,
            ),
        )
        if status == "not_probed" and detail:
            problems.append(
                Problem("ch_reader_user_not_probed", "info", detail, "run `loom migrate`.")
            )
        return problems
    except Exception:  # noqa: BLE001 — a doctor line must never crash the doctor
        return []


def _problem_checks(problems: list[Any]) -> list[tuple[str, str, str]]:
    """Render problems as doctor lines: `problem:<code>`, graded error→FAIL,
    warn→WARN, info→PASS, detail `message Fix: …` — the same list `/v1/health`
    carries, so the two surfaces cannot disagree. A problem about the data a deployment
    holds (`NOT_DEPLOYMENT_FAULTS`) is at most WARN, as it never degrades `status`: the
    doctor's exit code is a deployment gate too."""
    from server.observability.problems import NOT_DEPLOYMENT_FAULTS

    def grade(p: Any) -> str:
        g = _GRADE_BY_SEVERITY.get(p.severity, WARN)
        return WARN if g == FAIL and p.code in NOT_DEPLOYMENT_FAULTS else g

    return [(f"problem:{p.code}", grade(p), f"{p.message} Fix: {p.fix}") for p in problems]


def _run_doctor_checks() -> list[tuple[str, str, str]]:
    """The live checks (Python, settings, JWT, ClickHouse, LLM) followed
    by the shared problem list, as (name, grade, detail)."""
    return _run_live_checks() + _code_mirror_checks() + _problem_checks(_doctor_problems())


def _code_mirror_checks() -> list[tuple[str, str, str]]:
    try:
        from server.code.app.index.mirrors import doctor_checks

        _read_code_registry()  # the entries the checks list refuse until it is read
        return doctor_checks()
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return [("code_mirrors", WARN, f"not checked: {type(e).__name__}: {e}")]


def _run_live_checks() -> list[tuple[str, str, str]]:
    """Run the doctor's live checks and return them as (name, grade, detail)
    with grade one of PASS / WARN / FAIL.

    Shared by `_cmd_doctor` and `_cmd_report`. Each check captures its own
    exception to stay independent — one failure doesn't short-circuit the rest.
    """
    import sys

    checks: list[tuple[str, str, str]] = []  # (name, grade, detail)

    # 1. Python version (matches pyproject's requires-python>=3.12)
    py = sys.version_info
    py_ok = (py.major, py.minor) >= (3, 12)
    checks.append(
        (
            "python_version",
            _grade(py_ok),
            f"{py.major}.{py.minor}.{py.micro}"
            + ("" if py_ok else " — Loom requires Python 3.12 or newer"),
        )
    )

    # 2. Settings load (Pydantic surfaces missing env vars cleanly).
    try:
        from server.settings import settings

        ch_host = settings.ch_host
        jwt_secret_present = bool(settings.jwt_secret)
        settings_ok = bool(ch_host)
    except Exception as e:  # noqa: BLE001 — doctor reports any settings load failure
        checks.append(("settings_load", FAIL, f"{type(e).__name__}: {e}"))
        settings_ok = False
        ch_host = ""
        jwt_secret_present = False
    else:
        checks.append(
            (
                "settings_load",
                _grade(settings_ok),
                "loaded" if settings_ok else "LOOM_CH_HOST is empty",
            )
        )

    # 3. JWT secret. FAIL when empty even if auth_required is off — the secret
    # is needed for `loom mint-token` and the dashboard's token minting. WARN when it is
    # the checked-in dev default with auth on: the server
    # boots and works, and anyone with the repo can mint tokens it accepts.
    if not jwt_secret_present:
        jwt_grade, jwt_detail = (
            FAIL,
            "LOOM_JWT_SECRET is empty — `loom mint-token` and minting in the dashboard will fail",
        )
    elif settings_ok and (dev_warning := dev_jwt_secret_warning()):
        jwt_grade, jwt_detail = WARN, dev_warning
    else:
        jwt_grade, jwt_detail = PASS, "set"
    checks.append(("jwt_secret", jwt_grade, jwt_detail))
    if settings_ok:
        checks.append(_doctor_scim_token_check(settings))

    # 4. ClickHouse reachability. HTTP probe avoids spinning up asyncio.
    if not settings_ok or not ch_host:
        ch_ok, ch_detail = False, "skipped (no LOOM_CH_HOST configured)"
    else:
        ch_ok, ch_detail = _doctor_ch_reachable(settings, ch_host)
    checks.append(("clickhouse_reachable", _grade(ch_ok), ch_detail))

    # 4a. The checks boot runs before it migrates: version, database, grants.
    checks.append(
        _doctor_preflight_check()
        if settings_ok and ch_ok
        else ("clickhouse_preflight", WARN, "skipped (ClickHouse not reachable)")
    )

    # 4b. The OTel erasure index and the signing keyring: only once ClickHouse answered.
    checks += _doctor_ch_read_checks(settings if settings_ok and ch_ok else None)

    # 5. LLM provider resolution: what extraction runs on and
    # what decided it; WARN when the provider was inferred (or named) but its
    # key is absent — the runtime then falls back to the echo client.
    if settings_ok:
        checks.append(_doctor_llm_provider_check())
    else:
        checks.append(("llm_provider", PASS, "skipped (settings did not load)"))

    return checks


def _client_checks(args: argparse.Namespace) -> list[tuple[str, str, str]]:
    """`loom doctor --client`: the checks from the machine a
    coding agent runs on, with the CLI's own HTTP helpers. A crash in one of
    them is a FAIL line, never a crash of the doctor."""
    try:
        from server.cli._client import get_json, post_op
        from server.cli.doctor_client import client_checks

        return client_checks(args, get_json=get_json, post_json=post_op)
    except Exception as e:  # noqa: BLE001 — a doctor line must never crash the doctor
        return [("client", FAIL, f"client checks crashed: {type(e).__name__}: {e}")]


def _client_machine_grades(
    checks: list[tuple[str, str, str]], client: list[tuple[str, str, str]]
) -> list[tuple[str, str, str]]:
    """Under `--client`, a machine whose server answered
    `/v1/health` clean is a client. Its own direct ClickHouse probe (run only
    because a `.env` loaded here) is then not a failure — the hooks and the
    MCP server go through the server, which reaches ClickHouse itself — so a
    FAIL on `clickhouse_reachable` becomes a WARN that says so. Anything else,
    and a server that did not answer, is left as graded."""
    server = next((c for c in client if c[0] == "client:server"), None)
    if server is None or server[1] != PASS:
        return checks
    note = (
        " — not required on a client machine: the server answers and reaches "
        "ClickHouse itself; the hooks and the MCP server go through it. Ignore, or "
        "run `loom doctor` from outside a checkout to skip the server checks."
    )
    return [
        (name, WARN, detail + note)
        if name == "clickhouse_reachable" and grade == FAIL
        else (name, grade, detail)
        for name, grade, detail in checks
    ]


def _cmd_doctor(_args: argparse.Namespace) -> int:
    """Diagnose common pre-boot misconfigurations.

    Reports every check before exiting (not just the first failure), so the
    operator sees the full picture. Exit 0 = nothing failed (warnings are
    listed but do not fail); 1 = at least one FAIL.
    """
    checks = _run_doctor_checks()
    # The client's side of the same picture, when asked.
    client = _client_checks(_args) if getattr(_args, "client", False) else []
    checks = _client_machine_grades(checks, client) + client
    failed = [name for name, grade, _ in checks if grade == FAIL]
    warned = [name for name, grade, _ in checks if grade == WARN]
    if getattr(_args, "json", False):
        # The same problem list `/v1/health` carries, for scripts.
        print(
            json.dumps(
                {
                    "ok": not failed,
                    "checks": [{"name": n, "grade": g, "detail": d} for n, g, d in checks],
                    "problems": [p.as_dict() for p in _doctor_problems()],
                    "client_checks": [{"name": n, "grade": g, "detail": d} for n, g, d in client],
                },
                indent=2,
            )
        )
        return 1 if failed else 0

    width = max(len(name) for name, _, _ in checks)
    for name, grade, detail in checks:
        print(f"  [{grade}]  {name:<{width}}  {detail}")

    if failed:
        print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
        return 1
    suffix = f" ({len(warned)} warning(s): {', '.join(warned)})" if warned else ""
    print(f"\nAll {len(checks)} checks passed.{suffix}")
    return 0


# The issue form `loom report --open` fills (.github/ISSUE_TEMPLATE/).
BUG_FORM = "30_bug-report.yaml"


def _build_report_body(*, header_only: bool = False) -> str:
    """The bug form's Environment field, plain text: versions, platform and the
    `loom doctor` checks (`header_only`: the versions, short enough for a URL).

    No secrets — doctor reports presence booleans + the CH host only.
    """
    import platform as _platform

    loom_version = _version()
    py_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    platform_str = _platform.platform()

    header = f"Loom version: {loom_version}\nPython: {py_version}\nPlatform: {platform_str}\n"
    if header_only:
        return header

    checks = _run_doctor_checks()
    width = max(len(name) for name, _, _ in checks)
    doctor_block = "\n".join(
        f"  [{grade}]  {name:<{width}}  {detail}" for name, grade, detail in checks
    )
    return f"{header}\nloom doctor:\n{doctor_block}\n"


def _cmd_report(args: argparse.Namespace) -> int:
    """Print the bug form's Environment field with the doctor diagnostic.

    `--open` also opens the bug form with the versions pre-filled; the full
    diagnostic stays on stdout, since browsers truncate long URLs.
    """
    import urllib.parse
    import webbrowser

    body = _build_report_body()
    print(body)

    if args.open:
        header = _build_report_body(header_only=True)
        url = "https://github.com/ClickHouse/loom/issues/new?" + urllib.parse.urlencode(
            {"template": BUG_FORM, "environment": header}
        )
        opened = webbrowser.open(url)
        if opened:
            print(
                "\nOpened the bug form in your browser. "
                "Replace its Environment field with the output above.",
            )
        else:
            print(
                "\nCould not open a browser. Visit:\n  "
                f"https://github.com/ClickHouse/loom/issues/new?template={BUG_FORM}\n"
                "and put the output above in its Environment field.",
            )
    return 0


_DOCTOR_HELP = (
    "Diagnose pre-boot misconfig (Python version, settings, JWT, CH reachability) "
    "and print the problem list /v1/health carries, each with its fix. With --client, "
    "also examine this machine as a coding-agent client."
)


def _add_doctor_flags(p_doc: argparse.ArgumentParser) -> None:
    """`loom doctor`'s flags (kept out of `main`, which is at its length
    baseline)."""
    p_doc.add_argument(
        "--json",
        action="store_true",
        help="Emit the checks and the problem list as JSON.",
    )
    p_doc.add_argument(
        "--client",
        action="store_true",
        help=(
            "Also examine this machine as a coding-agent client: the "
            "server the hooks would call, the token they would present, whether it is "
            "accepted and can write the intended namespace, the Claude Code / Codex "
            "wiring, and capture jobs that never reached the server."
        ),
    )
    p_doc.add_argument("--server-url", default=None, help="Override the client's server URL.")
    p_doc.add_argument("--token", default=None, help="Override the client's token.")
