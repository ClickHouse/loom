"""One list of problems, each with a fix — shared by `/v1/health`, `loom
doctor` and the dashboard banner, so the three surfaces cannot disagree and
every flag says how to clear it.

`collect_problems` is pure over `Settings` plus a `HealthState` of signals the
caller already holds (the probed dim, the provider breaker, the lease, the
pending list) — it does no I/O and never adds a ClickHouse read to a probe
that is polled every few seconds. A `None` signal means "unknown here" (the
doctor runs before boot) and skips that check rather than guessing.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from server.core.ch_errors import error_code
from server.core.provider_keys import saved_key_presence

Severity = Literal["info", "warn", "error"]

# Ordered so a rendered list reads worst-first.
SEVERITY_RANK: dict[str, int] = {"error": 0, "warn": 1, "info": 2}

# ClickHouse's error code for a setting outside a profile's constraints.
_SETTING_CONSTRAINT_VIOLATION = 452


@dataclass(frozen=True)
class Problem:
    """A condition the operator should know about, with the one sentence
    that clears it. `fix` names the env var or command; `source` says which
    surface saw it (`settings`, `runtime`, `schema`, `workers`)."""

    code: str
    severity: Severity
    message: str
    fix: str
    source: str = "settings"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class HealthState:
    """Runtime signals the health handler already has in hand. Every field
    defaults to "unknown" so `loom doctor` (pre-boot, settings only) and the
    server (post-boot) share one collector."""

    probed_dim: int | None = None
    # Coded columns whose width their writes do not carry (`codes_state.width_mismatches`).
    width_mismatches: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    provider_status: Mapping[str, Any] | None = None  # provider_health.status()
    pending_migrations: tuple[str, ...] | None = None
    worker_lease_held: bool | None = None
    # `None` = not a server process (the doctor shell); '' = a server that
    # booted without a label.
    instance_label: str | None = None
    # A read-replica's last word from its main Loom (`main_loom.MAIN_STATE`):
    # True answered, False did not, None not asked yet or not a replica.
    main_reachable: bool | None = None
    main_error: str = ""
    # OTel metrics tables without the `mapValues(Attributes)`
    # skip index the erasure leg needs; `None` = not probed (doctor shell,
    # ClickHouse down, leg off).
    otel_metrics_unindexed: tuple[str, ...] | None = None
    # Seconds since this process read loom.revoked_tokens (inf = never);
    # None = not a server process.
    revocation_age_s: float | None = None
    # Whether repository jobs run inside the sandbox (code/app/index/sandbox.py):
    # None = not probed yet in this process.
    repo_sandbox: bool | None = None
    # Why the host layout refuses sandboxed jobs ('' = it does not).
    repo_sandbox_refusal: str = ""
    # Whether a read as LOOM_CH_READER_USER worked: None = not probed or unset.
    ch_reader_ok: bool | None = None
    # ClickHouse's answer when that read failed.
    ch_reader_error: str = ""
    # Whether run.begin claims run ids in Keeper (loom.agent_run_claims exists):
    # None = no run.begin has asked yet in this process.
    run_claims_atomic: bool | None = None
    # Whether /oauth/token claims each exchange in Keeper (loom.oauth_claims
    # exists): None = no exchange or boot probe has asked yet.
    oauth_claims_atomic: bool | None = None
    # Whether grant changes are claimed in Keeper (loom.grant_claims exists):
    # None = no grant change or boot probe has asked yet.
    grant_claims_atomic: bool | None = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    # Saved keys take precedence over the environment, including explicit empty keys.
    saved_keys: Mapping[str, bool] = field(default_factory=saved_key_presence)


def _embedder_key_envs(provider: str) -> tuple[str, ...]:
    """The key variables the embedder reads for `provider` (its own table)."""
    from server.embedding.embedder import KEY_ENVS

    return KEY_ENVS.get(provider, ())


def _llm_key_env(provider: str) -> str | None:
    """The key variable the LLM client reads for `provider`."""
    from server.core.llm_provider import KEY_ENV_BY_PROVIDER

    return KEY_ENV_BY_PROVIDER.get(provider)


def _key_env_for_health_name(name: str) -> str | None:
    """The key behind a provider_health entry (`<provider>:<kind>`, e.g.
    openai:embeddings, anthropic:messages); None for a name no key backs."""
    provider = name.split(":", 1)[0]
    return _llm_key_env(provider) or next(iter(_embedder_key_envs(provider)), None)


def _embedder_provider(model: str) -> str:
    from server.embedding.embedder import provider_for

    try:
        return provider_for(model)
    except ValueError:
        return "unknown"


def _embeds_via_main(settings: Any) -> bool:
    """A read-replica with the main Loom's URL embeds and completes there
    instead of falling back to the fakes, so a missing key is not a problem on it."""
    return bool(settings.read_replica) and bool(str(settings.read_replica_main_url).strip())


def _fake_severity(settings: Any) -> Severity:
    """A configured fake is a `warn` so the stock dev stack stays `status: ok`;
    `LOOM_REQUIRE_REAL_PROVIDERS=true` (production) makes it an `error`."""
    return "error" if bool(settings.require_real_providers) else "warn"


def _embedder_problems(settings: Any, state: HealthState) -> list[Problem]:
    out: list[Problem] = []
    model = str(settings.embedding_model)
    provider = _embedder_provider(model)
    fix_real = (
        "Choose an embedding model in Settings → Models & keys, which re-embeds what is "
        "stored at the model's width. Or set LOOM_EMBEDDING_MODEL=<a model of the width "
        "the vector tables store> with its key and run `loom reembed`, which keeps that "
        "width."
    )
    if provider == "fake":
        out.append(
            Problem(
                "fake_embedder",
                _fake_severity(settings),
                f"The embedder is the fake n-gram stub (LOOM_EMBEDDING_MODEL={model}): "
                "search ranks by character overlap, not meaning.",
                fix_real,
            )
        )
    else:
        keys = _embedder_key_envs(provider)
        missing = keys and not state.saved_keys.get(provider, any(state.env.get(k) for k in keys))
        if missing and not _embeds_via_main(settings):
            out.append(
                Problem(
                    "embedder_key_missing",
                    _fake_severity(settings),
                    f"LOOM_EMBEDDING_MODEL={model} needs {' or '.join(keys)}, which is not "
                    "set — the process falls back to the fake n-gram embedder.",
                    f"Add the key in Settings → Models & keys, or set {keys[0]}, or "
                    "LOOM_EMBEDDING_MODEL=fake-ngram-3072 to make the fake explicit.",
                )
            )
    if state.probed_dim is not None and state.probed_dim != int(settings.embedding_dim):
        out.append(
            Problem(
                "embedding_dim_mismatch",
                "error",
                f"{model} returns {state.probed_dim}-dim vectors but "
                f"LOOM_EMBEDDING_DIM={settings.embedding_dim}: writes at the configured "
                "width fail or mis-rank.",
                f"Set LOOM_EMBEDDING_DIM={state.probed_dim} and run `loom reembed` if rows "
                "were written at the old width.",
                source="runtime",
            )
        )
    if state.width_mismatches:
        groups: dict[tuple[int, int], list[str]] = {}
        for column, widths in sorted(state.width_mismatches.items()):
            groups.setdefault(widths, []).append(column)
        columns = "; ".join(
            f"{stored}-dim columns written at {written}: {', '.join(cols[:3])}"
            + (f" and {len(cols) - 3} more" if len(cols) > 3 else "")
            for (stored, written), cols in groups.items()
        )
        out.append(
            Problem(
                "coded_width_mismatch",
                "error",
                f"A Quantized codec fixes its column's width when the table is created, so "
                f"these writes fail: {columns}.",
                "Change the model in Settings → Models & keys, which rebuilds the vector tables "
                "at the new width. Setting LOOM_EMBEDDING_DIM (or the label's dim in "
                "LOOM_CODE_VECTOR_TABLES) back to the stored width helps only a model that "
                "can emit it.",
                source="runtime",
            )
        )
    return out


def _llm_problems(settings: Any, state: HealthState) -> list[Problem]:
    provider = str(settings.llm_provider)
    if provider == "fake":
        return [
            Problem(
                "fake_llm",
                _fake_severity(settings),
                "The LLM is the fake echo client (LOOM_LLM_PROVIDER=fake): extraction, "
                "entity tagging and memory.ask synthesis produce nothing.",
                "Choose an LLM in Settings → Models & keys, or set LOOM_LLM_PROVIDER=openai "
                "with OPENAI_API_KEY (or anthropic with ANTHROPIC_API_KEY) and LOOM_LLM_MODEL.",
            )
        ]
    key_env = _llm_key_env(provider)
    if (
        key_env
        and not state.saved_keys.get(provider, bool(state.env.get(key_env)))
        and not _embeds_via_main(settings)
    ):
        # Say how the provider was chosen — an inferred openai
        # with no key is the case worth naming.
        how = (
            f"was inferred from {settings.llm_provider_source()}"
            if settings.llm_provider_inferred()
            else "is set by LOOM_LLM_PROVIDER"
        )
        return [
            Problem(
                "llm_key_missing",
                _fake_severity(settings),
                f"LOOM_LLM_PROVIDER={provider} {how} but {key_env} is not set — every LLM "
                "call runs on the fake echo client and extracts nothing.",
                f"Add the key in Settings → Models & keys, or set {key_env}, or "
                "LOOM_LLM_PROVIDER=fake to make the fake explicit.",
            )
        ]
    return []


def _runtime_problems(state: HealthState) -> list[Problem]:
    out: list[Problem] = []
    providers = (state.provider_status or {}).get("providers") or {}
    for name, entry in sorted(providers.items()):
        if not entry.get("failing"):
            continue
        err = str(entry.get("last_error") or "unknown error")
        age = entry.get("last_error_age_s")
        when = f" {age}s ago" if age is not None else ""
        key_env = _key_env_for_health_name(name)
        behind = f"{key_env}'s quota" if key_env else f"the key and quota behind {name}"
        out.append(
            Problem(
                f"provider_failing:{name}",
                "error",
                f"The last call to {name} failed{when}: {err}",
                f"Check {behind} and the provider's status page; the flag clears on the "
                "next successful call.",
                source="runtime",
            )
        )
    return out


def _revocation_problems(settings: Any, state: HealthState) -> list[Problem]:
    """A snapshot never read refuses every token (503); one that stopped
    refreshing lets a token revoked since then keep authenticating."""
    age = state.revocation_age_s
    if age is None or not settings.auth_actually_required():
        return []
    fix = "Check this process can reach ClickHouse; the snapshot reloads on the next refresh."
    if age == float("inf"):
        return [
            Problem(
                "revocation_not_loaded",
                "error",
                "The token revocation list has not loaded: every bearer is refused with 503.",
                fix,
                source="runtime",
            )
        ]
    max_stale = float(settings.jwt_revocation_max_stale_seconds)
    if age > max_stale:
        return [
            Problem(
                "revocation_too_stale",
                "error",
                f"The token revocation list was last read {int(age)}s ago, past the "
                f"{int(max_stale)}s bound: every bearer is refused with 503.",
                fix,
                source="runtime",
            )
        ]
    stale_after = min(max_stale, max(300.0, 10 * float(settings.jwt_revocation_cache_ttl_seconds)))
    if age > stale_after:
        return [
            Problem(
                "revocation_stale",
                "warn",
                f"The token revocation list was last read {int(age)}s ago: a token revoked "
                "since then still authenticates on this process until the snapshot is "
                f"{int(max_stale)}s old.",
                fix,
                source="runtime",
            )
        ]
    return []


def _auth_problems(settings: Any) -> list[Problem]:
    if not str(settings.jwt_secret):
        return [
            Problem(
                "jwt_secret_empty",
                "error",
                "LOOM_JWT_SECRET is empty: `loom mint-token` and /me/setup cannot sign.",
                "Set LOOM_JWT_SECRET to a random secret (`openssl rand -hex 32`).",
            )
        ]
    # The doctor grades this WARN (a boot still works); the same severity here keeps the two
    # surfaces in agreement.
    if settings.jwt_secret_is_dev_default() and settings.auth_actually_required():
        return [
            Problem(
                "dev_jwt_secret",
                "warn",
                "LOOM_JWT_SECRET is the checked-in dev default with auth on: anyone who "
                "has read this repo can mint tokens this server accepts.",
                "Set LOOM_JWT_SECRET to a random secret (`openssl rand -hex 32`) and "
                "re-mint every token.",
            )
        ]
    if settings.auth_actually_required() and settings.jwt_sealing_secret_is_dev_default():
        return [
            Problem(
                "dev_sealing_secret",
                "warn",
                "LOOM_JWT_SECRET is the checked-in dev default: tokens are signed with "
                "LOOM_JWT_SECRETS_JSON, but stored credentials are sealed with it, so anyone "
                "who reads the tables can open them.",
                "Set LOOM_JWT_SECRET to a random secret (`openssl rand -hex 32`) and enter "
                "the stored credentials again.",
            )
        ]
    return []


def _exposure_problems(settings: Any) -> list[Problem]:
    """A server others can reach (non-loopback LOOM_BIND_HOST) without its
    safeguards, and forwarded-header or CORS settings that trust everyone.
    The bind ones also stop the server at boot unless LOOM_ALLOW_INSECURE=1."""
    from server.auth.public_url import cors_origin_list, is_loopback_host

    out: list[Problem] = []
    reachable = not is_loopback_host(str(settings.bind_host))
    severity: Severity = "warn" if settings.allow_insecure else "error"
    if reachable and not str(settings.public_base_url).strip():
        out.append(
            Problem(
                "public_base_url_missing",
                "error",
                f"LOOM_BIND_HOST={settings.bind_host} is not loopback and LOOM_PUBLIC_BASE_URL "
                "is unset: the server refuses to start.",
                "Set LOOM_PUBLIC_BASE_URL to the address clients use, or bind 127.0.0.1.",
            )
        )
    if reachable and not settings.auth_actually_required():
        out.append(
            Problem(
                "insecure_bind_auth_off",
                severity,
                f"Auth is off on a non-loopback bind (LOOM_BIND_HOST={settings.bind_host}): "
                "anyone who reaches the port has full access.",
                "Turn auth on or bind 127.0.0.1 (LOOM_ALLOW_INSECURE=1 only if the port is "
                "not actually reachable).",
            )
        )
    if reachable and settings.jwt_secret_is_dev_default():
        out.append(
            Problem(
                "insecure_bind_dev_secret",
                severity,
                f"LOOM_JWT_SECRET is the checked-in dev default on a non-loopback bind "
                f"(LOOM_BIND_HOST={settings.bind_host}).",
                "Set LOOM_JWT_SECRET to a random secret (`openssl rand -hex 32`) or bind "
                "127.0.0.1.",
            )
        )
    if "*" in [p.strip() for p in str(settings.trusted_proxies).split(",")]:
        out.append(
            Problem(
                "trusted_proxies_any",
                "warn",
                "LOOM_TRUSTED_PROXIES is '*': any caller can set its own X-Forwarded-For "
                "(rate-limit address) and X-Forwarded-Proto.",
                "List the proxy addresses or CIDRs in front of Loom.",
            )
        )
    try:
        cors_origin_list(str(settings.cors_origins))
    except RuntimeError:
        out.append(
            Problem(
                "cors_wildcard",
                "error",
                "LOOM_CORS_ORIGINS contains '*' while CORS allows credentials: the server "
                "refuses to start.",
                "List the exact origins allowed to call Loom from a browser.",
            )
        )
    return out


def _deployment_problems(settings: Any, state: HealthState) -> list[Problem]:
    out: list[Problem] = []
    pending = state.pending_migrations
    if pending:
        names = ", ".join(pending[:3]) + (", …" if len(pending) > 3 else "")
        out.append(
            Problem(
                "pending_migrations",
                "error",
                f"{len(pending)} migration(s) the schema lacks: {names}. Ops that read "
                "the new columns fail until they run.",
                "Run `loom migrate` (or set LOOM_BOOT_MIGRATE=true and restart).",
                source="schema",
            )
        )
    if (
        state.instance_label is not None
        and not state.instance_label
        and not bool(settings.deployment)
    ):
        out.append(
            Problem(
                "instance_label_missing",
                "warn",
                "LOOM_INSTANCE_LABEL is empty: rows this process writes cannot be "
                "attributed to a launcher.",
                "Set LOOM_INSTANCE_LABEL=<who>:<what>:<port>, or LOOM_DEPLOYMENT=1 for the "
                "compose deployment only.",
            )
        )
    if state.worker_lease_held is False:
        mode = str(settings.background_workers).strip().lower()
        if mode == "off":
            msg = (
                "This process runs no background workers (LOOM_BACKGROUND_WORKERS=off): "
                "extraction and consolidation depend on a workers container holding the lease."
            )
        else:
            msg = (
                "This process does not hold the workers lease (LOOM_BACKGROUND_WORKERS="
                f"{mode}): another process runs the workers, or none does yet."
            )
        out.append(
            Problem(
                "workers_lease_absent",
                "info",
                msg,
                "Run the workers service (`loom-<colour>-workers`), or set "
                "LOOM_BACKGROUND_WORKERS=auto on a single-container stack.",
                source="workers",
            )
        )
    return out


def _otel_problems(settings: Any, state: HealthState) -> list[Problem]:
    """An OTel metrics table without a `mapValues(Attributes)`
    bloom filter turns every erasure's ClickStack pre-count into a full scan
    (GiBs per saga). ClickStack owns the tables, so the fix is
    the operator's DDL, named here and in `loom doctor`."""
    db = str(settings.clickstack_database)
    missing = state.otel_metrics_unindexed
    if not db or not missing:
        return []
    return [
        Problem(
            "otel_erasure_index",
            "warn",
            f"{db}.{', '.join(missing)} carry no mapValues(Attributes) bloom filter: every "
            "erasure full-scans them to find its rows (hundreds of millions of rows / GiBs "
            "per table on a large histogram).",
            "Once per table: ALTER TABLE <db>.<table> ADD INDEX IF NOT EXISTS idx_attr_value "
            "mapValues(Attributes) TYPE bloom_filter(0.01) GRANULARITY 1, then MATERIALIZE "
            "INDEX idx_attr_value (re-issue if system.data_skipping_indices reads 0 bytes) — "
            '"ClickStack erasure index" in docs/operations/maintenance.md.',
            source="erasure",
        )
    ]


def _replica_problems(settings: Any, state: HealthState) -> list[Problem]:
    """A read-replica whose main Loom does not answer serves no blame and, with
    no provider key of its own, no embedding or LLM answer: an error, so
    `status` says `degraded` while the HTTP code stays 200 (the replica still
    serves every read from ClickHouse)."""
    if not settings.read_replica or state.main_reachable is not False:
        return []
    return [
        Problem(
            "main_loom_unreachable",
            "error",
            "This read-replica's main Loom does not answer "
            f"({state.main_error or 'no detail'}): blame, and embeddings and LLM calls "
            "it sends there, fail until it does.",
            "Check that LOOM_READ_REPLICA_MAIN_URL is reachable from this host and the main "
            "Loom is up.",
        )
    ]


def _layout_problems(state: HealthState) -> list[Problem]:
    """A host layout the sandbox refuses: jobs fail only where they would run
    sandboxed. Before the first job probes, bwrap on PATH stands in for the probe
    (no process is spawned from a health check)."""
    if not state.repo_sandbox_refusal:
        return []
    sandboxed = state.repo_sandbox is True or (
        state.repo_sandbox is None and shutil.which("bwrap") is not None
    )
    fix = (
        "Move the data paths (LOOM_BLOB_DIR, LOOM_CODE_REPOS_ROOT, "
        "LOOM_CODE_SCIP_CHECKOUT_DIR, LOOM_ENV_FILE) out of the Python install and "
        "system directories."
    )
    why = (
        "The server's Python or a system path lies in Loom's data, and no sandbox mount "
        "could hide it."
    )
    if sandboxed:
        message = f"Repository jobs fail: {state.repo_sandbox_refusal}. {why}"
        return [Problem("repo_sandbox_layout_refused", "error", message, fix, source="runtime")]
    message = f"Sandboxed repository jobs would be refused: {state.repo_sandbox_refusal}. {why}"
    return [Problem("repo_sandbox_layout_refused", "warn", message, fix, source="runtime")]


_SANDBOX_FIX = (
    "Address the 'sandbox probe failed' warning in the server log (printed at the top of "
    "`loom doctor`). In a container on a host with AppArmor 4.0 or later, load the "
    "loom-repo-sandbox profile and give the container the security_opt and pids_limit "
    "the docs list."
)
_SANDBOX_DOCS = "'Repository-job sandbox' in docs/operations/code-index-operations.md"


def _sandbox_problems(settings: Any, state: HealthState) -> list[Problem]:
    """Repository jobs run without the sandbox: an operator's repositories still
    get their build-based tiers, which execute the repository's code on this
    host; self-registered ones get none, are not fetched over the network, and
    adding one is refused."""
    if not settings.code_ingest_enabled:
        return []
    problems = _layout_problems(state)
    if state.repo_sandbox is not False:
        return problems
    if settings.code_sandbox_required:
        return [
            *problems,
            Problem(
                "repo_jobs_unsandboxed",
                "error",
                "Repository jobs (clone, fetch, dependency installs, compiler tiers) are "
                "refused: LOOM_CODE_SANDBOX_REQUIRED is set and the sandbox cannot run here.",
                f"{_SANDBOX_FIX} Or, for repositories you trust to run unsandboxed, unset "
                "LOOM_CODE_SANDBOX_REQUIRED and list them in LOOM_CODE_REPOS_JSON — "
                f"{_SANDBOX_DOCS}.",
                source="runtime",
            ),
        ]
    return [
        *problems,
        Problem(
            "repo_jobs_unsandboxed",
            "warn",
            "Repository jobs (clone, fetch, dependency installs, compiler tiers) run "
            "unsandboxed: operator repositories' build tiers execute repository code on "
            "this host; self-registered repositories get no build tier, those with a "
            "network remote are not cloned or fetched, and adding one from the page is "
            "refused.",
            f"{_SANDBOX_FIX} Where the sandbox cannot run, list the repositories you trust "
            f"in LOOM_CODE_REPOS_JSON — {_SANDBOX_DOCS}.",
            source="runtime",
        ),
    ]


def _reader_user_problems(settings: Any, state: HealthState) -> list[Problem]:
    """The caller-shaped reads (code.grep, code.search with a pattern or globs,
    memory.aggregate, memory.record.query) run as the reader user, so they all
    fail while ClickHouse refuses it."""
    if state.ch_reader_ok is not False:
        return []
    user = settings.ch_reader_user
    fix = (
        f"Run `loom ch-setup`, which creates {user} or says why it cannot (on the bundled "
        "server it comes from deploy/clickhouse-config/nlp_user_settings.xml), or set "
        "LOOM_CH_READER_USER empty."
    )
    if error_code(state.ch_reader_error) == _SETTING_CONSTRAINT_VIOLATION:
        fix = (
            f"A LOOM_CH_CALLER_SHAPED_* ceiling is outside the {user} profile's bounds "
            f"(ClickHouse: {_constraint_message(state.ch_reader_error)}). Bring it within "
            "them, or run `loom ch-setup` to rebuild the profile from the current values (on "
            "the bundled server, change the bounds in nlp_user_settings.xml)."
        )
    return [
        Problem(
            "ch_reader_user_failing",
            "error",
            f"A read as LOOM_CH_READER_USER={user} failed (the boot probe, or `loom doctor`): "
            "code.grep, code.search with a pattern or globs, memory.aggregate and "
            "memory.record.query fail while it does.",
            fix,
            source="runtime",
        )
    ]


def _run_claim_problems(state: HealthState) -> list[Problem]:
    """No Keeper: two concurrent run.begin calls on one run id can both keep it."""
    if state.run_claims_atomic is not False:
        return []
    return [
        Problem(
            "run_claims_not_atomic",
            "info",
            "loom.agent_run_claims is absent (no ClickHouse Keeper), so run.begin checks a "
            "caller-supplied run_id and then writes it: two users' concurrent begins on one "
            "run_id can both keep it, and only one run survives.",
            "Run ClickHouse with a Keeper (deploy/clickhouse-config/keeper.xml), create the "
            "table from schemas/migrations/025_agent_run_claims.sql (the migration skipped "
            "it) and restart Loom.",
            source="runtime",
        )
    ]


def _oauth_claim_problems(settings: Any, state: HealthState) -> list[Problem]:
    """No Keeper: two concurrent exchanges of one OAuth code or refresh token can both mint."""
    if not settings.oauth_enabled or state.oauth_claims_atomic is not False:
        return []
    return [
        Problem(
            "oauth_claims_not_atomic",
            "warn",
            "loom.oauth_claims is absent (no ClickHouse Keeper), so /oauth/token keeps two "
            "exchanges of one code or refresh token apart only by using it up first: two "
            "concurrent ones can still both mint tokens.",
            "Run ClickHouse with a Keeper (deploy/clickhouse-config/keeper.xml) and create the "
            "table from schemas/migrations/028_oauth_claims.sql (the migration skipped it); "
            "an exchange within a minute finds it.",
            source="runtime",
        )
    ]


def _grant_claim_problems(state: HealthState) -> list[Problem]:
    """No Keeper: two processes can change one grant at once, the later writing over
    the earlier."""
    if state.grant_claims_atomic is not False:
        return []
    return [
        Problem(
            "grant_changes_not_atomic",
            "warn",
            "loom.grant_claims is absent (no ClickHouse Keeper), so two changes of one grant "
            "are kept apart only within each server process: across processes an approval "
            "can still write over a promotion or a revoke that landed after its read.",
            "Run ClickHouse with a Keeper (deploy/clickhouse-config/keeper.xml) and create the "
            "table from schemas/migrations/029_grant_claims.sql (the migration skipped it); "
            "a grant change within a minute finds it.",
            source="runtime",
        )
    ]


def _constraint_message(error: str) -> str:
    """`Setting max_execution_time shouldn't be greater than 30` out of the error text."""
    m = re.search(r"Setting \w+ [^.(]*", error)
    return m.group(0).strip() if m else error[:200]


def collect_problems(settings: Any, state: HealthState | None = None) -> list[Problem]:
    """Every known problem for this process, worst-first. Pure: reads
    `settings`, `state` and `state.env` only."""
    st = state or HealthState()
    problems = (
        _embedder_problems(settings, st)
        + _llm_problems(settings, st)
        + _runtime_problems(st)
        + _auth_problems(settings)
        + _exposure_problems(settings)
        + _revocation_problems(settings, st)
        + _deployment_problems(settings, st)
        + _otel_problems(settings, st)
        + _replica_problems(settings, st)
        + _sandbox_problems(settings, st)
        + _reader_user_problems(settings, st)
        + _run_claim_problems(st)
        + _oauth_claim_problems(settings, st)
        + _grant_claim_problems(st)
    )
    return sorted(problems, key=lambda p: (SEVERITY_RANK.get(p.severity, 9), p.code))


def derive_status(ch_ok: bool, problems: list[Problem]) -> str:
    """`/v1/health` `status`: `degraded` when ClickHouse is down or any
    error-severity problem exists; configured fakes are `warn` unless
    `LOOM_REQUIRE_REAL_PROVIDERS=true`, so the stock dev stack stays `ok`."""
    if not ch_ok or any(p.severity == "error" for p in problems):
        return "degraded"
    return "ok"
