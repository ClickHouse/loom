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
    # Why this process could not ask for the lease ('' when it could).
    worker_lease_error: str = ""
    # Why this process stopped contending for the workers lease ('' while it does).
    worker_lease_given_up: str = ""
    # `None` = not a server process (the doctor shell); '' = a server that
    # booted without a label.
    instance_label: str | None = None
    # How this process differs from the deployment's published configuration
    # (`deployment_config.drift()`); None = not a server process.
    config_drift: tuple[str, ...] | None = None
    # OTel metrics tables without the `mapValues(Attributes)`
    # skip index the erasure leg needs; `None` = not probed (doctor shell,
    # ClickHouse down, leg off).
    otel_metrics_unindexed: tuple[str, ...] | None = None
    # Seconds since this process read loom.revoked_tokens (inf = never);
    # None = not a server process.
    revocation_age_s: float | None = None
    # `server.auth.keyring.status()`; None = not a server process.
    keyring: Mapping[str, Any] | None = None
    # Whether repository jobs run inside the sandbox (code/app/index/sandbox.py):
    # None = not probed yet in this process.
    repo_sandbox: bool | None = None
    # Why the host layout refuses sandboxed jobs ('' = it does not).
    repo_sandbox_refusal: str = ""
    # Whether a read as LOOM_CH_READER_USER worked: None = not probed or unset.
    ch_reader_ok: bool | None = None
    # ClickHouse's answer when that read failed.
    ch_reader_error: str = ""
    # insights.query's analyst user: True = the last probe passed, False = a
    # finding, None = no probe answer (`analysis_error` set once that lasts).
    analysis_ok: bool | None = None
    analysis_error: str = ""
    # The finding's own fix, when it has one.
    analysis_fix: str = ""
    # The assistant's LibreChat service: None = not set up yet or no LibreChat
    # configured, False = its setup failed (`assistant_error` says why).
    assistant_ok: bool | None = None
    assistant_error: str = ""
    # Whether a sign-in lately found loom.oauth_signin_state at its key limit.
    oauth_signin_state_full: bool = False
    # Provider participants this process's last sweep could not retire
    # (`provider_barrier.stale_participants()`): None = not swept yet.
    stale_participants: tuple[Mapping[str, str], ...] | None = None
    # Cut-off carried writes held past their release or uncountable
    # (`rmt_carry.pinned_overdue()`): None = not a server process.
    carry_pinned: tuple[Mapping[str, Any], ...] | None = None
    # Why the dashboard's stored model configuration is not applied
    # (`provider_runtime.refused_configuration()`); '' when it is, or not a server.
    refused_configuration: str = ""
    # The code-index tiers failing past the warn threshold, one row per entry and
    # tier (`server.code.app.index.tier_health.problem_rows`): org, namespace, tier,
    # kind, since, failed_passes, lag_commits, fix, severity. None = not read here.
    code_tier_failures: tuple[Mapping[str, Any], ...] | None = None
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    # Saved keys take precedence over the environment, including explicit empty keys.
    saved_keys: Mapping[str, bool] = field(default_factory=saved_key_presence)


def _keyless() -> bool:
    """Model calls take a workload token instead of a provider key (server/llm/gateway.py)."""
    from server.llm import gateway

    return gateway.keyless()


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
        missing = missing and not (provider in ("openai", "compatible") and _keyless())
        if missing:
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
    if key_env and not (state.saved_keys.get(provider, bool(state.env.get(key_env))) or _keyless()):
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
    if state.refused_configuration:
        out.append(
            Problem(
                "model_configuration_refused",
                "error",
                "The model configuration saved in the dashboard is not applied: "
                f"{state.refused_configuration}. Every data-plane request on this server "
                "is refused (503 GATEWAY_REQUIRED) and it is not ready until a "
                "replacement is saved or the configuration is reset; one that needs a "
                "rebuild is then held as a model change (503 MODEL_CHANGE_IN_PROGRESS) "
                "until it is applied.",
                "Save one whose endpoints are in LOOM_INFERENCE_GATEWAY_HOSTS (Settings, "
                "Models & keys), or reset it to the environment's.",
                source="runtime",
            )
        )
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


def _keyring_problems(state: HealthState) -> list[Problem]:
    """A keyring this process never read, keys it cannot open (another root
    secret sealed them) and a keyring that leaves it nothing to sign with."""
    k = state.keyring
    if k is None:
        return []
    if not k.get("loaded"):
        return [
            Problem(
                "keyring_not_loaded",
                "error",
                "This process has not read loom.signing_keys since it started: every token "
                "it is shown answers 503 and nothing can be signed.",
                "Check that this process's ClickHouse user can SELECT loom.signing_keys "
                "and that migration 033 is applied; the server log names the read error.",
                source="runtime",
            )
        ]
    out: list[Problem] = []
    if k.get("root_row") == "other":
        out.append(
            Problem(
                "keyring_root_mismatch",
                "error",
                "LOOM_JWT_SECRET is not the root secret loom.signing_keys was recorded under: "
                "tokens of the keys the other processes sign with are refused here.",
                "Give this process the deployment's LOOM_JWT_SECRET; if the root was "
                "replaced on purpose, run `loom signing-key record-root` with the new one.",
                source="runtime",
            )
        )
    if k.get("unsealable"):
        out.append(
            Problem(
                "signing_keys_unsealable",
                "warn",
                f"Signing key(s) {', '.join(k['unsealable'])} do not unseal with this "
                "LOOM_JWT_SECRET: their tokens are refused here.",
                "Run this process with the LOOM_JWT_SECRET the keys were sealed under, or "
                "retire a key nothing signs with (`loom signing-key retire <kid>`).",
                source="runtime",
            )
        )
    if not k.get("signing_kid"):
        out.append(
            Problem(
                "no_signing_key",
                "error",
                "No signing key is active here, and kid 'default' is retired or "
                "LOOM_JWT_SECRET signs neither HS256 nor RS256: every sign-in and "
                "token mint answers 503 NO_SIGNING_KEY.",
                "Run `loom signing-key rotate` with the deployment's LOOM_JWT_SECRET.",
                source="runtime",
            )
        )
    return out


def _auth_problems(settings: Any) -> list[Problem]:
    if not str(settings.jwt_secret):
        return [
            Problem(
                "jwt_secret_empty",
                "error",
                "LOOM_JWT_SECRET is empty: `loom mint-token` and the dashboard cannot sign tokens.",
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
    return []


def _exposure_problems(settings: Any) -> list[Problem]:
    """A server others can reach (non-loopback LOOM_BIND_HOST) without its
    safeguards, and forwarded-header or CORS settings that trust everyone.
    The bind ones also stop the server at boot unless LOOM_ALLOW_INSECURE=1."""
    from server.auth import keyring
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
    if reachable and settings.mcp_auth_mode == "open":
        out.append(
            Problem(
                "insecure_bind_mcp_open",
                severity,
                f"LOOM_MCP_AUTH_MODE=open on a non-loopback bind "
                f"(LOOM_BIND_HOST={settings.bind_host}): every MCP tool runs with no principal.",
                "Set LOOM_MCP_AUTH_MODE=required or bind 127.0.0.1.",
            )
        )
    weak = keyring.weak_keys(settings) if reachable else []
    if weak:
        out.append(
            Problem(
                "insecure_bind_weak_key",
                severity,
                f"{', '.join(weak)} {'is' if len(weak) == 1 else 'are'} below the minimum key "
                "size on a non-loopback bind "
                f"(LOOM_BIND_HOST={settings.bind_host}).",
                f"Use a random secret of {keyring.MIN_HMAC_BYTES} or more bytes "
                f"(`openssl rand -hex 32`) and RSA keys of {keyring.MIN_RSA_BITS} bits or "
                "more, or bind 127.0.0.1.",
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
    if state.carry_pinned:
        ids = ", ".join(q for p in state.carry_pinned for q in p["query_ids"])
        out.append(
            Problem(
                "carry_write_pinned",
                "warn",
                f"A cut-off write (queries {ids}) is still held: "
                f"{state.carry_pinned[0]['error']}. Until it is released this process "
                "acknowledges the older column lists, so a boot after a migration waits for it.",
                "Grant the Loom user SELECT on system.processes (clusterAllReplicas on "
                "LOOM_CH_CLUSTER), or check LOOM_CH_CLUSTER names this cluster.",
                source="schema",
            )
        )
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
    if state.worker_lease_given_up:
        out.append(
            Problem(
                "workers_lease_given_up",
                "warn",
                f"This process gave the workers lease up: {state.worker_lease_given_up}. "
                "It runs no background workers for a few minutes, then asks again; "
                "another process takes them meanwhile.",
                "Read the workers child's log lines (`loom.workers.child`) for why it exits.",
                source="workers",
            )
        )
    elif state.worker_lease_held is False and state.worker_lease_error:
        out.append(
            Problem(
                "workers_lease_unreachable",
                "warn",
                f"This process could not ask for the workers lease: {state.worker_lease_error}. "
                "If every process sees this, no process runs the background workers.",
                "Check that this host reaches ClickHouse and its Keeper (LOOM_CH_HOST) and "
                "that its user may write loom._migration_lock.",
                source="workers",
            )
        )
    elif state.worker_lease_held is False:
        mode = settings.workers_mode()
        if mode == "off" and settings.background_workers == "auto":
            msg = (
                "This process runs no background workers: LOOM_BACKGROUND_WORKERS=auto runs "
                "none outside the deployment (LOOM_DEPLOYMENT unset). Set it to on to run them."
            )
        elif mode == "off":
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
                "Keep LOOM_BACKGROUND_WORKERS=auto on every replica; one of them holds the "
                "lease and runs the workers.",
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


def _drift_problems(state: HealthState) -> list[Problem]:
    """A process whose token keys or MUST_MATCH settings differ from the
    deployment's: it is not ready, so the load balancer sends it nothing."""
    if not state.config_drift:
        return []
    return [
        Problem(
            "config_drift",
            "error",
            "This process's configuration differs from the deployment's published one: "
            + "; ".join(state.config_drift),
            "Give every replica the same env (LOOM_JWT_SECRET, LOOM_EMBEDDING_MODEL, "
            'LOOM_EMBEDDING_DIM) and restart this one — "Running N replicas" in '
            "docs/operations/replicas.md.",
            source="runtime",
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
        "Move the data paths (LOOM_CODE_REPOS_ROOT, "
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


def _assistant_problems(settings: Any, state: HealthState) -> list[Problem]:
    """The assistant needs its LibreChat service user and agent; until a boot sets them
    up, the assistant is unavailable."""
    del settings
    if state.assistant_ok is not False:
        return []
    return [
        Problem(
            "assistant_service_failing",
            "warn",
            f"The assistant is unavailable: {state.assistant_error}",
            "Check that LibreChat answers at LOOM_ASSISTANT_LIBRECHAT_URL; the next try "
            "of the setup clears this. A sign-in LibreChat refuses (a 4xx other than its "
            "429 limit) means its database came from elsewhere: empty it and run "
            "`TRUNCATE TABLE loom.assistant_service`. A 5xx is LibreChat or its database "
            "being unavailable: nothing to delete.",
            source="runtime",
        )
    ]


def _analysis_problems(settings: Any, state: HealthState) -> list[Problem]:
    """insights.query runs as the analyst user. A finding (the user missing or
    wider than the allowlist) answers ANALYSIS_UNAVAILABLE; a probe that has had
    no answer from ClickHouse for a while answers OVERLOADED, and its fix is
    ClickHouse's reachability and load, not access."""
    del settings
    if state.analysis_ok is True or not state.analysis_error:
        return []
    if state.analysis_ok is None:
        return [
            Problem(
                "analysis_user_failing",
                "warn",
                f"insights.query is unavailable: {state.analysis_error}",
                "ClickHouse has not answered the analysis probe: check that it is reachable "
                "and not overloaded (the analyst user's concurrency cap included); the next "
                "probe that gets an answer clears it.",
                source="runtime",
            )
        ]
    return [
        Problem(
            "analysis_user_failing",
            "warn",
            f"insights.query is unavailable: {state.analysis_error}",
            state.analysis_fix
            or "Act on the cause above; the server log holds the detail. Where "
            "it names a write the analyst user needs, give LOOM_CH_USER access management (the "
            "bundled ClickHouse's loom user has it), which the next probe uses, or run "
            "`loom ch-setup` as a user that has it, or have an admin run the SQL "
            "`loom ch-setup --print-analyst-sql` prints.",
            source="runtime",
        )
    ]


def _oauth_claim_problems(settings: Any, state: HealthState) -> list[Problem]:
    """A full claims table turns new sign-ins away until the sweep frees keys.
    Nothing to say where sign-in is not mounted (auth off, no MCP connect)."""
    if not (settings.auth_actually_required() or settings.oauth_enabled):
        return []
    if not state.oauth_signin_state_full:
        return []
    return [
        Problem(
            "oauth_signin_state_full",
            "warn",
            "loom.oauth_signin_state reached its key limit (seen by this process), so new "
            "codes, device sign-ins and sessions answer 503 until keys expire; they are "
            "swept every five minutes.",
            "Rate-limit /oauth/device_authorization, /oauth/device and /oauth/callback by "
            "network at the proxy; the server log names an address it refused each minute.",
            source="runtime",
        )
    ]


def _participant_problems(state: HealthState) -> list[Problem]:
    """A provider reconfiguration waits on every participant it cannot count as stopped."""
    if not state.stale_participants:
        return []
    ids = ", ".join(
        f"{p.get('participant_id')} ({p.get('reason')})" for p in state.stale_participants
    )
    return [
        Problem(
            "provider_participants_stale",
            "warn",
            f"A provider reconfiguration would wait on these participants: {ids}. "
            "`heartbeat_expired` ones stopped heartbeating (they may be paused), "
            "and `queries_running` ones did so with a query still running.",
            "Once a participant's process is confirmed stopped, retire it with "
            "`loom participant retire --participant <id> --yes`.",
            source="runtime",
        )
    ]


def _constraint_message(error: str) -> str:
    """`Setting max_execution_time shouldn't be greater than 30` out of the error text."""
    m = re.search(r"Setting \w+ [^.(]*", error)
    return m.group(0).strip() if m else "a setting constraint"


# How many failing tiers the `code_tier_failing` message names before it counts the rest.
_TIER_FAILURES_NAMED = 5


def _code_tier_problems(st: HealthState) -> list[Problem]:
    """One `code_tier_failing` problem for all the code-index tiers that keep failing:
    each tier with its error kind, failed passes, since when and how far behind, and
    the fix of each kind. It names no entry or repository, because `/v1/health` answers
    anyone and an entry may be restricted; `code.repos` and the repository page name
    them to whoever may read them. Its severity is the worst row's."""
    rows = st.code_tier_failures or ()
    if not rows:
        return []

    def named(r: Mapping[str, Any]) -> str:
        lag, passes = int(r["lag_commits"]), int(r["failed_passes"])
        # the count the entry's last published status made: a lower bound
        behind = f", at least {lag} commit{'' if lag == 1 else 's'} behind" if lag >= 0 else ""
        return (
            f"{r['tier']} ({r['kind']}, {passes} failed pass{'' if passes == 1 else 'es'} "
            f"since {r['since']}{behind})"
        )

    shown = [named(r) for r in rows[:_TIER_FAILURES_NAMED]]
    more = len(rows) - len(shown)
    tail = f" and {more} more" if more > 0 else ""
    noun, it = ("tier is", "it answers") if len(rows) == 1 else ("tiers are", "they answer")
    fixes = list(dict.fromkeys(str(r["fix"]) for r in rows))
    return [
        Problem(
            "code_tier_failing",
            "error" if any(r["severity"] == "error" for r in rows) else "warn",
            f"{len(rows)} code-index {noun} failing, so what {it} trails the "
            f"branch: {'; '.join(shown)}{tail}. code.repos and the repository page name "
            "each entry.",
            " ".join(fixes),
            source="workers",
        )
    ]


# Problems about the data a deployment holds, not the deployment: they are listed, and
# never make `status` degraded (a repository's failing code-index tier would otherwise
# fail `loom up`, `helm test` and every health gate over a missing token).
NOT_DEPLOYMENT_FAULTS = frozenset({"code_tier_failing"})


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
        + _keyring_problems(st)
        + _deployment_problems(settings, st)
        + _otel_problems(settings, st)
        + _drift_problems(st)
        + _sandbox_problems(settings, st)
        + _reader_user_problems(settings, st)
        + _analysis_problems(settings, st)
        + _assistant_problems(settings, st)
        + _oauth_claim_problems(settings, st)
        + _participant_problems(st)
        + _code_tier_problems(st)
    )
    # within a severity, the deployment's own problems first
    return sorted(
        problems,
        key=lambda p: (SEVERITY_RANK.get(p.severity, 9), p.code in NOT_DEPLOYMENT_FAULTS, p.code),
    )


def derive_status(ch_ok: bool, problems: list[Problem]) -> str:
    """`/v1/health` `status`: `degraded` when ClickHouse is down or any
    error-severity problem exists but those in `NOT_DEPLOYMENT_FAULTS`; configured
    fakes are `warn` unless `LOOM_REQUIRE_REAL_PROVIDERS=true`, so the stock dev stack
    stays `ok`."""
    faults = [p for p in problems if p.code not in NOT_DEPLOYMENT_FAULTS]
    if not ch_ok or any(p.severity == "error" for p in faults):
        return "degraded"
    return "ok"
