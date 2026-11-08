"""Every Loom setting, in one place.

Each setting is one field below, in the section for its area: its name is the
`LOOM_<NAME>` environment variable in lowercase, its default is the value it ships with,
and the comment above it says what it controls; `docs/reference/configuration.md` and
`schemas/loom-settings.schema.json` are generated from this file. `Annotated[T, LIVE]`
marks a field a deployment can change while Loom runs (`loom settings set`). The
sections compose into one flat `Settings` (`server/settings/__init__.py`), which also
holds where a value comes from: a live override, the environment, the dotenv file, the
settings file, the default.
"""

from __future__ import annotations

import functools
import ipaddress
import itertools
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
import idna
from pydantic import AfterValidator, BaseModel, Field, PrivateAttr, field_validator, model_validator

from server.core.document_formats import route_key
from server.core.embedding_space import inferred_provider
from server.core.numbers import parse_budget_value
from server.settings._env import (
    DEV_JWT_SECRET,
    CaseFolded,
    JsonArray,
    JsonObject,
    RoutePrior,
    decode_json_or_b64,
    json_map_items,
)
from server.settings._live import LIVE

# ===== Assistant ==============================================================


class AssistantSettings(BaseModel):
    """The assistant: LibreChat run headless beside Loom, and the agent Loom seeds in it."""

    # LibreChat's internal address (http://librechat:3080), reachable only from Loom. ""
    # = no assistant: no service setup at boot.
    assistant_librechat_url: str = ""
    # Email of the LibreChat service user that owns the agent; LibreChat sends it no mail.
    assistant_service_email: str = "loom-assistant@loom.invalid"
    # Seconds before the service setup is tried again after its first failure
    # (LibreChat still starting, its database restored from elsewhere); each next
    # failure doubles the wait, up to 16 times this. A try signs in at most once, and
    # LibreChat allows 7 sign-ins per address in 5 minutes, hence the floor of 60.
    assistant_setup_retry_s: float = Field(default=60.0, ge=60, le=3600)


# ===== Auth ===================================================================


@functools.lru_cache(maxsize=4)
def _end_user_issuers(raw: str) -> dict[str, EndUserIssuer]:
    """Decoded once per raw value: every token check reads it."""
    if not raw:
        return {}
    parsed = decode_json_or_b64(raw, "jwt_end_user_issuers_json")
    if not isinstance(parsed, dict):
        raise ValueError("jwt_end_user_issuers_json must map issuer to its orgs and keys")
    return {str(issuer): EndUserIssuer.parse(str(issuer), spec) for issuer, spec in parsed.items()}


def _public_numbers(pem: str) -> object:
    """A PEM public key's numbers, to tell one key under two kids; None when it
    does not load (it then verifies nothing)."""
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    try:
        return load_pem_public_key(pem.encode()).public_numbers()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — a key that does not load matches none
        return None


def _overlap(a: str, b: str) -> bool:
    """Whether two org patterns (a name, or `prefix*`) can match one org."""
    if a.endswith("*") and b.endswith("*"):
        return a[:-1].startswith(b[:-1]) or b[:-1].startswith(a[:-1])
    if a.endswith("*"):
        return b.startswith(a[:-1])
    if b.endswith("*"):
        return a.startswith(b[:-1])
    return a == b


@dataclass(frozen=True)
class EndUserIssuer:
    """One end-user issuer: the orgs its tokens may name and where its keys are."""

    orgs: tuple[str, ...]
    jwks_url: str = ""
    keys: dict[str, str] = dataclass_field(default_factory=dict)

    @classmethod
    def parse(cls, issuer: str, spec: object) -> EndUserIssuer:
        if not isinstance(spec, dict):
            raise ValueError(f"end-user issuer {issuer!r}: expected {{orgs, jwks_url, keys}}")
        orgs = spec.get("orgs")
        if (
            not isinstance(orgs, list)
            or not orgs
            or not all(isinstance(o, str) and o for o in orgs)
        ):
            raise ValueError(f"end-user issuer {issuer!r}: `orgs` lists the orgs it may sign for")
        if any(o.strip() in ("*", "") for o in orgs):
            raise ValueError(f"end-user issuer {issuer!r}: name its orgs, not every org (`*`)")
        jwks_url = str(spec.get("jwks_url") or "")
        if jwks_url and not jwks_url.startswith("https://"):
            raise ValueError(f"end-user issuer {issuer!r}: `jwks_url` is https")
        keys = spec.get("keys") or {}
        if not isinstance(keys, dict):
            raise ValueError(f"end-user issuer {issuer!r}: `keys` maps kid to a public PEM")
        if not jwks_url and not keys:
            raise ValueError(f"end-user issuer {issuer!r}: give `jwks_url` or `keys`")
        unknown = sorted(set(spec) - {"orgs", "jwks_url", "keys"})
        if unknown:
            raise ValueError(f"end-user issuer {issuer!r}: unknown fields {unknown}")
        return cls(
            orgs=tuple(o.strip() for o in orgs),
            jwks_url=jwks_url,
            keys={str(k): str(v) for k, v in keys.items()},
        )

    def signs_for(self, org: str) -> bool:
        """Whether this issuer's tokens may name `org`: listed, or under a `prefix*`."""
        return any(org.startswith(o[:-1]) if o.endswith("*") else org == o for o in self.orgs)


class AuthSettings(BaseModel):
    """Authentication, tokens and identity providers."""

    # Comma-separated login handles and emails promoted to platform_admin
    # (org-wide visibility). A handle matches a password account's subject; an
    # email matches only an IdP-verified (OIDC) email, never the unverified
    # self-signup address. Empty = no platform admins.
    platform_admins: str = ""

    @property
    def platform_admins_set(self) -> set[str]:
        return {s.strip().lower() for s in (self.platform_admins or "").split(",") if s.strip()}

    # True: every operation requires a valid bearer JWT. False (laptop demos
    # only): missing tokens are not rejected and the SPA synthesizes a local
    # identity. identity_provider=single-user forces auth off regardless.
    auth_required: bool = True
    # The one namespace auth-off mode serves (SPA identity, dashboard bootstrap,
    # MCP/SDK caller defaults). Ignored when auth is required; `personal-*` is
    # reserved and rejected.
    default_namespace: str = "local"

    @field_validator("default_namespace")
    @classmethod
    def _normalise_default_namespace(cls, v: str) -> str:
        """Empty normalises to `local` so an explicitly-blank env var behaves
        like an unset one. Shape validation happens in the model validator
        below, where the auth mode is known."""
        return (v or "").strip() or "local"

    @model_validator(mode="after")
    def _check_default_namespace(self) -> AuthSettings:
        """Reject unusable names before any data is written under one — but
        only in the mode that reads the setting.

        The value is inserted into `loom.namespaces` and baked into the SPA
        identity, so it must satisfy the shared namespace-name shape
        (`server.core.naming.validate_namespace_name` — the same rule set
        `namespace.register` and the create dialog accept). `personal-*` is
        additionally reserved for per-subject auto-provisioning: reusing it
        collides with that scheme and skews the `NOT LIKE 'personal-%'`
        analytics split in the dashboard.

        Auth-ON deployments never read the setting, so a stale value left in
        a shared .env must not crash-loop the server/CLI/migrations for a
        mode that ignores it — warn and carry on instead.
        """
        from server.core.naming import validate_namespace_name

        v = self.default_namespace
        error: str | None = None
        if v.startswith("personal-"):
            error = (
                f"LOOM_DEFAULT_NAMESPACE={v!r} uses the reserved 'personal-' "
                "prefix, which belongs to per-subject auto-provisioning. "
                "Pick a name that doesn't start with 'personal-'."
            )
        else:
            try:
                self.default_namespace = validate_namespace_name(v)
            except ValueError as e:
                error = f"LOOM_DEFAULT_NAMESPACE: {e}"
        if error is None:
            return self
        if not self.auth_actually_required():
            raise ValueError(error)
        logging.getLogger(__name__).warning(
            "%s — ignored because auth is enabled (the setting only steers "
            "auth-disabled deployments); fix or remove it from the env.",
            error,
        )
        return self

    # MCP one-click connect: dynamic client registration and /oauth/authorize
    # on top of the sign-in routes (device grant, token, revoke) every
    # auth-on deployment serves. Opt-in: anyone can register a client.
    oauth_enabled: bool = False
    # Public base URL used in OAuth discovery and redirects; empty derives it from each request.
    # Required when `oauth_login_provider` is "oidc" (the IdP callback URL is built from it).
    oauth_issuer: str = ""
    # How people sign in (dashboard, `loom login`, MCP connect): "password"
    # (Loom's own form) or "oidc" (the external IdP; loom maps the verified
    # email to an account and still mints its own token). "oidc" closes
    # password sign-in and self-signup, and requires `oauth_issuer` and the
    # three oidc_* values.
    oauth_login_provider: Literal["password", "oidc"] = "password"
    # Lifetime of an OAuth access token, in seconds; at most a day, which bounds access
    # after the IdP deactivates someone (only a refresh asks it).
    oauth_access_token_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    # Seconds a password set-up link stays valid, at most a week (it sets a password once).
    password_link_ttl_seconds: int = Field(default=86400, ge=300, le=7 * 86400)
    # Seconds between passes of the always-on sweep that drops sign-in state
    # (loom.oauth_signin_state keys) past its credential's expiry.
    oauth_signin_state_sweep_interval_seconds: int = Field(default=300, ge=1)
    # External IdP issuer URL; its `/.well-known/openid-configuration` must name the same issuer.
    oidc_issuer: str = ""
    # Client id registered at the IdP; also the audience each id_token must carry.
    oidc_client_id: str = ""
    # Client secret sent to the IdP token endpoint when exchanging the sign-in code.
    oidc_client_secret: str = ""
    # Origin used for server-side sign-in calls to the issuer, preserving its Host header.
    # Empty uses the issuer directly; browser redirects and bot-trust key fetches are unchanged.
    oidc_backchannel_base_url: str = ""
    # JIT provisioning: a first OIDC sign-in with no account creates a personal
    # namespace in oidc_jit_org instead of being rejected. Off = fail closed.
    # oidc_jit_email_domains (comma-separated) restricts it; empty admits any
    # verified email the IdP authenticates.
    oidc_jit_provision: bool = False
    # Org that JIT-provisioned accounts are created in; required when `oidc_jit_provision` is on.
    oidc_jit_org: str = ""
    # Comma-separated email domains JIT provisioning admits; empty admits any verified email.
    oidc_jit_email_domains: str = ""
    # Seconds one call to an identity provider may take: OIDC discovery, the token and
    # revocation endpoints, its signing keys, a machine issuer's discovery, and an end-user
    # issuer's JWKS fetch.
    identity_provider_timeout_s: Annotated[float, LIVE] = Field(default=10.0, gt=0)
    # Bearer tokens (comma-separated, any one accepted) an identity provider presents to
    # deactivate and reactivate people over SCIM 2.0 (/scim/v2). Empty = off; needs auth on, oidc.
    scim_token: str = ""

    @property
    def oidc_jit_email_domains_set(self) -> set[str]:
        return {
            s.strip().lower() for s in (self.oidc_jit_email_domains or "").split(",") if s.strip()
        }

    @model_validator(mode="after")
    def _check_oidc_backchannel(self) -> AuthSettings:
        """A mistyped back-channel base fails at load, not at the first sign-in. The
        error never echoes the value: a refused one may carry a credential."""
        value = self.oidc_backchannel_base_url
        if not value:
            return self
        parts = urlsplit(value)
        try:
            numeric_port = parts.port is None or parts.port >= 0
        except ValueError:  # a port that is not a number, or out of range
            numeric_port = False
        if (
            not numeric_port
            or parts.scheme not in ("http", "https")
            or not parts.hostname
            or "@" in parts.netloc
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "LOOM_OIDC_BACKCHANNEL_BASE_URL must be an http(s) URL of a host and an "
                "optional port, with no user, path, query or fragment"
            )
        return self

    # The root secret, and the one secret a deployment carries: it signs tokens
    # as kid `default` and seals every stored credential and every signing key
    # in loom.signing_keys (`loom signing-key rotate` adds those). Required when
    # auth is on. The default is the checked-in DEV secret anyone can mint
    # with: a deployment with auth on refuses to start with it, except the
    # `make dev` stack (`loom doctor` warns everywhere else). A non-loopback bind
    # refuses an HMAC secret under 32 bytes or an RSA key under 2048 bits.
    jwt_secret: str = DEV_JWT_SECRET
    # The root secret LOOM_JWT_SECRET replaced, while a rotation is under way:
    # credentials and signing keys sealed under it still open (tokens it signed
    # as kid `default` do not verify: a leaked root mints nothing). `loom
    # secrets reseal` rewrites the stored ones under the new root; then remove
    # this. "" = none.
    jwt_secret_previous: str = ""
    # Seconds between re-reads of loom.signing_keys in every process (at least
    # 1); `loom signing-key rotate` waits out two of the slowest process's.
    jwt_keyring_refresh_seconds: float = Field(default=30.0, ge=1.0)

    def jwt_secret_is_dev_default(self) -> bool:
        """Whether the root secret is the checked-in dev default — the value
        anyone who can read this repo can mint tokens with (`loom mint-token`
        warns, `loom doctor` grades WARN)."""
        return self.jwt_secret == DEV_JWT_SECRET

    # The `iss` Loom stamps on every token it mints; a verified token carries it or one
    # of `jwt_trusted_issuers`.
    jwt_issuer: str = "loom"
    # The `aud` claim stamped on minted tokens and required on every verified token.
    jwt_audience: str = "loom-platform"
    # Extra accepted `iss` values, for a gateway minting Loom-shaped JWTs with a
    # key in `jwt_public_keys_json`. Loom's own tokens (iss = jwt_issuer) pass.
    jwt_trusted_issuers: str = ""

    def trusted_issuers(self) -> set[str]:
        """The `iss` values a token Loom verifies may carry: Loom's own issuer
        plus the federated issuers in `jwt_trusted_issuers`."""
        extra = (i.strip() for i in self.jwt_trusted_issuers.split(","))
        return {self.jwt_issuer, *(i for i in extra if i)}

    # JSON (or base64 JSON) map kid → PEM public key of a gateway whose RS256
    # tokens this deployment accepts (a non-loopback bind refuses an RSA key under 2048
    # bits). Loom's own keys live in loom.signing_keys.
    jwt_public_keys_json: str = ""
    # Clock-skew tolerance on iat/exp, seconds; small enough to give replay no
    # useful post-exp window.
    jwt_leeway_seconds: int = 30

    # Seconds the `loom.revoked_tokens` snapshot is cached; verify checks both
    # jti and sub against it, so a revocation takes effect on other processes
    # within this TTL (at once on the one that wrote it).
    jwt_revocation_cache_ttl_seconds: float = 30.0
    # Oldest revocation snapshot a process still trusts, seconds. Past it
    # (refreshes keep failing) every bearer is refused with a retryable 503.
    # A subject's cached grants are trusted as long when a re-read fails.
    jwt_revocation_max_stale_seconds: float = 600.0
    # Seconds a subject's grant rows are cached for the namespace gate, which
    # caps a token's claim at the grant it holds there. The backstop: a grant
    # change reaches other processes through the per-org grant watermark
    # (acl_watermark_poll_seconds), and this bounds one whose watermark row was
    # never written. It also bounds how long a SCIM deactivation of an email, or a
    # bot disable, takes to stop a trusted caller acting for it on another process.
    grant_cache_ttl_seconds: float = 10.0
    # Seconds between each process's reads of the per-org grant watermark
    # (loom.acl_versions): a revoke on one replica is enforced on every other
    # within about this long.
    acl_watermark_poll_seconds: float = Field(default=1.0, ge=0.1)
    # Oldest grant watermark a process still trusts, seconds. Past it (polls keep
    # failing) cached grants are not served and every request reads its own.
    acl_watermark_max_stale_seconds: float = Field(default=10.0, gt=0)

    # Sign-ins, sign-ups and workload-token exchanges one process counts at once
    # (each costs ClickHouse statements, a sign-in a password hash); past it a
    # caller waits up to a second, then gets 429 (an exchange 503), so a burst
    # cannot take the shared connection pool.
    auth_attempts_concurrency: int = Field(default=8, ge=1)
    # Workload-token exchanges (RFC 8693) one trust may make in 10 minutes,
    # deployment-wide; past it /oauth/token answers 429 until older ones age out.
    workload_exchanges_per_trust: int = Field(default=50000, ge=1)
    # Workload-token exchanges one agent of a trust (a token with an agent claim) may
    # make in 10 minutes, so one agent's loop cannot spend the trust's. A trust without
    # agents has only `workload_exchanges_per_trust`.
    workload_exchanges_per_agent: int = Field(default=300, ge=1)

    # Browser self-signup: "off" (endpoint 404s) or "open"
    # (/v1/auth/self-signup mints JWTs scoped to the requested namespace).
    self_signup: Literal["off", "open"] = "off"
    # Default org for self-signed tokens.
    self_signup_org: str = "dev"

    # How callers authenticate: "single-user" (no auth, one anonymous identity),
    # "trust-network" (self-signup, first claim wins) or "gateway" (an upstream proxy
    # mints short-lived Loom JWTs with the shared HS256 secret; Loom only validates).
    # Empty = inferred from auth_required and self_signup at boot.
    identity_provider: Literal["", "single-user", "trust-network", "gateway"] = ""

    # When set, a dashboard page load redirects (302) here instead of serving the SPA (Loom
    # behind an aggregator). Deployment-specific; never hardcode.
    dashboard_redirect_url: str = ""

    # Comma-separated subjects allowed to send `X-Loom-On-Behalf-Of` (a trusted
    # aggregator passing each end-user's identity under one service token).
    # Empty disables the header.
    trusted_callers: str = ""

    @property
    def trusted_callers_set(self) -> set[str]:
        return {s.strip() for s in self.trusted_callers.split(",") if s.strip()}

    # The services that sign a short-lived token per end user (a chat app in
    # front of Loom): JSON (or base64 JSON) {issuer: {"orgs": [...], "jwks_url":
    # "https://…", "keys": {kid: RS256 public PEM}}}. `orgs` lists the orgs its
    # tokens may name (exact names, or `prefix*`); its keys come from `jwks_url`
    # (fetched every `jwt_end_user_jwks_refresh_seconds`) and/or `keys`. A key
    # here verifies only its own issuer's tokens, and only as an end user
    # (`sub: user:<id>`, one listed `org`, an optional `agent: agent:<id>`),
    # never any other token. Empty: no end-user tokens. A non-loopback bind refuses
    # an inline RSA key under 2048 bits (a key fetched from `jwks_url` is not checked).
    jwt_end_user_issuers_json: str = ""
    # The longest an end-user token may live, from its `iat` to its `exp`.
    jwt_end_user_max_ttl_seconds: int = Field(default=300, ge=60, le=3600)
    # Seconds between fetches of each end-user issuer's `jwks_url`; a key it
    # rotates in is honoured from the next fetch.
    jwt_end_user_jwks_refresh_seconds: float = Field(default=300.0, ge=10.0)

    def end_user_issuers(self) -> dict[str, EndUserIssuer]:
        """issuer → what it may sign. Empty when none set."""
        return _end_user_issuers(self.jwt_end_user_issuers_json.strip())

    @model_validator(mode="after")
    def _end_user_issuers_stand_apart(self) -> AuthSettings:
        issuers = self.end_user_issuers()
        # A token's `iss` picks the end-user path, so an issuer is one or the other.
        both = sorted(set(issuers) & self.trusted_issuers())
        if both:
            raise ValueError(
                f"{both} are both end-user and trusted issuers (LOOM_JWT_ISSUER, "
                "LOOM_JWT_TRUSTED_ISSUERS)"
            )
        # A federated key verifies any token of a trusted `iss`, Loom-shaped ones
        # included, so an end-user service's key is never one.
        federated = self.federated_key_numbers()
        for issuer, spec in issuers.items():
            same = sorted(k for k, pem in spec.keys.items() if _public_numbers(pem) in federated)
            if same:
                raise ValueError(f"end-user issuer {issuer!r}: keys {same} are federated keys")
        # `user:<id>` names one person per org, so one issuer alone signs for an org.
        for (a, spec_a), (b, spec_b) in itertools.combinations(issuers.items(), 2):
            shared = [(x, y) for x in spec_a.orgs for y in spec_b.orgs if _overlap(x, y)]
            if shared:
                raise ValueError(f"end-user issuers {a!r} and {b!r} both sign for {shared[0]}")
        return self

    def auth_actually_required(self) -> bool:
        """Whether the HTTP transport should require a bearer.

        single-user mode is "no auth at all" by definition and must override
        auth_required=True (common in prod-derived dev configs), or the SPA
        breaks.
        """
        if self.effective_identity_provider() == "single-user":
            return False
        return self.auth_required

    def effective_identity_provider(self) -> str:
        """Resolve identity_provider, inferring from auth_required +
        self_signup when unset: auth-off → single-user; auth-on + signup-open
        → trust-network; auth-on + signup-off → gateway."""
        if self.identity_provider:
            return self.identity_provider
        if not self.auth_required:
            return "single-user"
        if self.self_signup == "open":
            return "trust-network"
        return "gateway"

    @model_validator(mode="after")
    def _check_gateway_keys(self) -> AuthSettings:
        """A malformed gateway key map fails here, not on the first RS256 token."""
        self.jwt_public_key_map()
        return self

    @model_validator(mode="after")
    def _check_watermark_staleness(self) -> AuthSettings:
        """A watermark is polled every interval, so a stale limit at or under it
        reads grants on every request between two polls."""
        if self.acl_watermark_max_stale_seconds <= self.acl_watermark_poll_seconds:
            raise ValueError(
                "LOOM_ACL_WATERMARK_MAX_STALE_SECONDS must be larger than "
                "LOOM_ACL_WATERMARK_POLL_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _check_revocation_staleness(self) -> AuthSettings:
        """A snapshot is refreshed every cache TTL, so a stale limit at or under
        it refuses every bearer between two refreshes."""
        if self.jwt_revocation_max_stale_seconds <= self.jwt_revocation_cache_ttl_seconds:
            raise ValueError(
                "LOOM_JWT_REVOCATION_MAX_STALE_SECONDS must be larger than "
                "LOOM_JWT_REVOCATION_CACHE_TTL_SECONDS"
            )
        # The keyring goes stale at the same limit: a failed read, and the next
        # one landing a little after its interval, must not trip it.
        if self.jwt_revocation_max_stale_seconds < 3 * self.jwt_keyring_refresh_seconds:
            raise ValueError(
                "LOOM_JWT_REVOCATION_MAX_STALE_SECONDS must be at least three times "
                "LOOM_JWT_KEYRING_REFRESH_SECONDS"
            )
        return self

    def federated_key_numbers(self) -> set[object]:
        """The public numbers of every federated key that loads: a key is told
        apart by these, whatever kid it goes under."""
        return {_public_numbers(pem) for pem in self.jwt_public_key_map().values()} - {None}

    def jwt_public_key_map(self) -> dict[str, str]:
        """For RS256 verifiers: kid → PEM public key. Empty when none set."""
        raw = self.jwt_public_keys_json.strip()
        if not raw:
            return {}
        return dict(json_map_items(raw, "jwt_public_keys_json"))

    # Kill switch for restricted namespaces. True: served to a principal whose
    # token names the namespace AND who holds a live direct grant. False: every
    # read is refused while ingest continues.
    restricted_serve: bool = True
    # Wait for the `namespace.restricted_access` audit row of each admitted restricted read
    # before answering; false writes it beside the answer, lost if the process dies first.
    restricted_audit_inline: bool = True
    # Comma-separated origin list. "*" allows all (acceptable when
    # auth_required=True). Empty disables CORS entirely.
    cors_origins: str = ""
    # Request-body cap, bytes, enforced by ASGI middleware before any parser
    # (413 PAYLOAD_TOO_LARGE); uvicorn has no limit of its own. 0 disables.
    max_request_bytes: int = 32 * 1024 * 1024
    # Strict-Transport-Security max-age, seconds; browsers ignore it over plain HTTP.
    # 0 sends no header.
    hsts_max_age_seconds: int = Field(default=31_536_000, ge=0)

    # History policy over the agent trace: "off" reads/writes nothing;
    # "shadow" records verdicts in loom.policy_decisions without blocking;
    # "enforce" denies (403 POLICY_FORBIDDEN) and records them too. In
    # enforce, writes fail closed on evaluation errors; reads never do.
    policy_mode: Literal["off", "shadow", "enforce"] = "off"
    # Cap on any rule's look-back window, in seconds.
    policy_max_window_seconds: int = 86_400
    # Per-(org, namespace) rule cache TTL, seconds; other replicas see a rule
    # change within it.
    policy_cache_ttl_seconds: int = 30

    # ---- timeouts, budgets and caches ----
    # Seconds a machine issuer's discovery document (its JWKS URL) is cached.
    auth_machine_discovery_ttl_s: Annotated[int, LIVE] = Field(default=3600, gt=0)
    # Seconds the identity provider's OpenID configuration is cached.
    auth_oidc_discovery_ttl_s: Annotated[float, LIVE] = Field(default=3600.0, gt=0)


# ===== ClickHouse =============================================================

# A database name Loom writes into statements and KeeperMap paths unescaped.
_DATABASE_NAME = r"^[A-Za-z_][A-Za-z0-9_-]*$"
# Databases a ClickHouse server or other applications on it own.
_SHARED_DATABASES = frozenset({"default", "system", "information_schema"})


class ClickHouseSettings(BaseModel):
    """ClickHouse connection, query shaping and vector storage."""

    # Host name of the ClickHouse server Loom stores everything in.
    ch_host: str = "localhost"
    # ClickHouse HTTP port; 8443 (the Cloud HTTPS port) turns TLS on when `ch_secure` is unset.
    ch_port: int = 8123
    # User name the server connects to ClickHouse as.
    ch_user: str = "loom"
    # Password for `ch_user`; the default only suits a local development server.
    ch_password: str = "loomdev"
    # The database Loom's schema lives in: every table, view and the migrations ledger,
    # and the connection's default database. Statements are written against `loom`
    # and name this database where they reach ClickHouse (server/core/ch_names.py);
    # KeeperMap paths move with it. Set it before the first migration: while a schema is
    # in `loom`, a server pointed at a name that does not exist, or holds tables but no
    # Loom ledger, refuses to start (create it empty to start a second schema). A letter
    # or `_`, then letters, digits, `_` and `-`; never `default`, `system`,
    # `information_schema`, or a table or alias name the schema uses. Read once at startup.
    ch_database: Annotated[str, Field(pattern=_DATABASE_NAME)] = "loom"
    # The database the telemetry Loom derives from its own tables lives in
    # (`sli_minute`), outside the schema so a reader can be granted it alone. A schema
    # outside `loom` names its own: two schemas never share one.
    ch_telemetry_database: Annotated[str, Field(pattern=_DATABASE_NAME)] = "loom_telemetry"
    # The cluster `clusterAllReplicas` reads to find a stopped Loom process's
    # queries on every node (`default` on ClickHouse Cloud). A server without
    # it answers for itself; '' reads only the connected server.
    ch_cluster: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]*$")] = "default"
    # Connect over HTTPS. Unset infers TLS from the port (8443 on, anything else off).
    ch_secure: bool | None = None
    # Append `sql:<shape12> <first 200 chars>` of each statement to its
    # `log_comment` ("<op> <request_id>"), so query_log rows group by shape.
    # Off by default: readers that split log_comment on spaces gain tokens.
    ch_log_comment_statement_digest: bool = False
    # A file path: append one JSON line (`digest`, `sql`) per executed statement, to
    # collect the shapes an acceptance run executes for the analyzer corpus. '' = off;
    # never set it in production.
    ch_capture_statements: str = ""
    # HTTP connection pool size: request concurrency plus 3 x the batched-insert bound
    # (inserts a second x wait / 1000; docs/architecture/clickhouse.md#batched-inserts).
    ch_pool_maxsize: int = 64
    # Seconds an idle pooled connection is kept. Must stay BELOW the Cloud load
    # balancer's idle cutoff (8 s) so loom closes first: a peer-closed pooled
    # TLS connection is never reaped under uvloop (`LOOM_UVICORN_LOOP=uvloop`).
    ch_keepalive_timeout: float = 5.0
    # A point lookup that MISSES right after a write (get after set, verify,
    # update_by_id, delete) re-reads once with select_sequential_consistency=1.
    # Costs only on a miss; a no-op on single-node MergeTree.
    ch_read_your_writes: bool = True
    # memory.search on a namespace this process wrote within this many seconds
    # reads with select_sequential_consistency from the start. 0 = off (the
    # empty-pool retry remains the backstop).
    ch_read_your_writes_window_s: float = 20.0
    # Deadline, seconds, for read statements, applied as `max_execution_time` and as a
    # client-side bound that frees the pool slot. 0 = no bound for reads.
    ch_timeout_read_s: float = 30.0
    # Deadline, seconds, for INSERT and UPDATE statements; 0 = no bound.
    ch_timeout_write_s: float = 60.0
    # Deadline, seconds, for DDL statements (CREATE, ALTER, DROP); 0 = no bound.
    ch_timeout_ddl_s: float = 300.0
    # Batch the inserts written on every request into shared parts; false commits them, and the
    # waited async inserts keep their bounds (docs/architecture/clickhouse.md#batched-inserts).
    ch_batched_inserts: Annotated[bool, LIVE] = True
    # Shortest wait, ms, of a batched or waited async insert: what a lone insert waits when
    # writes are sparse; capped at ch_batched_insert_wait_max_ms (or the background bound).
    ch_batched_insert_wait_min_ms: Annotated[int, LIVE] = Field(default=10, gt=0, le=1000)
    # Longest wait, ms, of a batched or waited async insert a request waits on; it holds a
    # pooled connection meanwhile.
    ch_batched_insert_wait_max_ms: Annotated[int, LIVE] = Field(default=50, gt=0, le=1000)
    # Longest wait, ms, of the usage-event flusher's ticks and shutdown drain and the
    # write-mark timer's flush, which no request waits on.
    ch_batched_insert_background_wait_max_ms: Annotated[int, LIVE] = Field(
        default=200, gt=0, le=5000
    )
    # Max wait, ms, for a pool slot before failing fast with 503 OVERLOADED.
    # 0 = no slot gate.
    ch_pool_wait_ms: int = 500
    # Fresh connections a statement tries after a replica-bound error (a replica that
    # lost its Keeper session, a replica's read-only table). Behind a load balancer one
    # reconnect can land on the replica that just failed, so a single retry is not enough.
    ch_replica_bound_retries: int = Field(default=2, ge=1, le=5)
    # Ceilings for the reads a caller shapes (code.grep, code.search with a
    # pattern or globs, memory.aggregate, memory.record.query). A ceiling only
    # lowers the statement's own value, so no request can widen it; 0 = none.
    # The shipped loom_reader profile (nlp_user_settings.xml) caps these three at
    # their defaults, with a floor no statement can go under (time: 0.05 s,
    # code.grep's statement floor), so none can be set to 0 even in the SQL.
    # Raising a setting above its cap needs the profile raised too; a 0 sends
    # no ceiling, so the profile's cap applies (code.grep's deadline is then
    # clamped to ch_timeout_read_s, which the profile's cap matches).
    # --- caller-shaped read ceilings ---
    # Seconds one caller-shaped read may run (the read class default); 0 = no
    # ceiling, so the loom_reader profile's cap applies.
    ch_caller_shaped_max_execution_s: float = 30.0
    # Bytes of memory one caller-shaped read may use; 0 = no ceiling, so the
    # loom_reader profile's cap applies.
    ch_caller_shaped_max_memory_bytes: int = 4_000_000_000
    # Rows one caller-shaped read may scan; past it the read fails (422), never a
    # partial answer. 0 = no ceiling, so the loom_reader profile's cap applies.
    ch_caller_shaped_max_rows_to_read: int = 500_000_000
    # ClickHouse user the caller-shaped reads run as, with `ch_password`; the
    # shipped config defines `loom_reader` (SELECT only). Empty = `ch_user`.
    ch_reader_user: str = ""

    # Caps and pacing for insights.query. The statement caps are pinned as a MAX in
    # the loom_analyst user's caps profile so no statement can raise them; a boot
    # whose ClickHouse user has access management writes that profile when one changed.
    # --- analysis statements (insights.query) ---
    # Seconds one insights.query statement may run.
    ch_analysis_max_execution_s: float = Field(default=10.0, ge=0.1, le=300.0)
    # Rows one statement may scan; past it the statement fails (422), never a partial answer.
    ch_analysis_max_rows_to_read: int = Field(default=200_000_000, ge=1)
    # Bytes of memory one statement may use.
    ch_analysis_max_memory_bytes: int = Field(default=2_000_000_000, ge=1)
    # Rows one statement returns at most; a longer result comes back cut, marked truncated.
    ch_analysis_max_result_rows: int = Field(default=500, ge=1, le=10_000)
    # Bytes of result one statement may return; past it the statement fails (422).
    ch_analysis_max_result_bytes: int = Field(default=4_000_000, ge=1)
    # Statements the analyst user runs at once across every org and replica.
    ch_analysis_max_concurrent: int = Field(default=8, ge=1, le=256)
    # Threads one statement may use (pinned as a MAX, so a statement cannot take the host).
    ch_analysis_max_threads: int = Field(default=4, ge=1, le=64)
    # Statements one org runs at once on one replica; past it a call answers RATE_LIMITED.
    ch_analysis_max_concurrent_per_org: int = Field(default=2, ge=1, le=64)
    # Seconds between two probes of the analyst user in each web process; a problem
    # an admin fixes, or a repair a write makes, shows within one interval.
    ch_analysis_probe_interval_s: Annotated[float, LIVE] = Field(default=60.0, ge=5, le=3600)

    # --- traffic classes ---
    # JSON map {traffic class: ClickHouse settings} applied by
    # `server.core.ch.statement_settings` to every read (an explicit per-call
    # setting wins). `priority`: 1 served first, larger waits, 0 opts out;
    # `max_threads` caps CPU per statement. Neither changes the rows returned.
    # `background` (workers, operator CLIs) bypasses the filesystem cache on
    # a miss so a backfill scan does not evict interactive data.
    ch_settings_by_traffic_class: Annotated[str, JsonObject] = (
        '{"production": {"priority": 1}, "interactive": {"priority": 2}, '
        '"bench": {"priority": 10, "max_threads": 4}, '
        '"background": {"priority": 5, "max_threads": 3, '
        '"read_from_filesystem_cache_if_exists_otherwise_bypass_cache": 1}}'
    )
    # --- migrations ---
    # Apply pending migrations at boot. Set False for any server started from
    # a branch against a shared service, or it applies unmerged migrations.
    boot_migrate: bool = True
    # Seconds after which the boot migration lease's holder, once it stops renewing,
    # is replaced.
    boot_migrate_lease_ttl_s: int = 300
    # Seconds a contender waits for the holder before boot fails (exit 1, the
    # container restarts); also how long a booting process waits for the
    # provider configuration lock (a sibling's rebuild step may hold it).
    boot_migrate_lease_wait_s: float = 900.0
    # Seconds a migration file waits for the mutations it started (MATERIALIZE,
    # UPDATE, a column type change) to finish on every replica; past it the
    # file fails unrecorded.
    migrate_mutation_wait_s: int = 600

    # --- vector search ---
    # Send `vector_search_use_quantized_codes = 1` on vector-ranked reads whose
    # column has a `Quantized(...)` codec and whose table has no patch parts:
    # a codes-first scan plus full-precision rescore. A replica that rejects
    # the setting (code 115) drops the fragment and retries.
    vector_search_quantized_codes: bool = True
    # Over-fetch for the codes-first read: stage 1 keeps LIMIT × this by code
    # distance, stage 2 rescores. Only read when the codes setting is sent.
    vector_search_quantized_fetch_multiplier: float = 3.0
    # Seconds between re-reads of which vector columns are coded and which
    # coded tables carry patch parts. A write that embeds reads again on its own
    # when the reading is older than a few seconds (codes_state.FRESH_READING_S).
    vector_codes_state_probe_seconds: int = 60

    # ---- timeouts, budgets and caches ----
    # Seconds the schema status (applied against shipped migrations) is cached for the
    # health probe.
    schema_status_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _check_databases(self) -> ClickHouseSettings:
        """Statements name `loom` and `loom_telemetry` and are mapped to these two:
        neither is a database the server or another application owns, they differ, a
        database named after the other alias would be mapped twice, and a schema outside
        `loom` names its own telemetry database."""
        for field in ("ch_database", "ch_telemetry_database"):
            if getattr(self, field).lower() in _SHARED_DATABASES:
                raise ValueError(
                    f"LOOM_{field.upper()} must not be {getattr(self, field)!r}: a database "
                    "the server or other applications use. If Loom's schema is in `loom`, "
                    "unset it"
                )
        if self.ch_database == self.ch_telemetry_database:
            raise ValueError("LOOM_CH_DATABASE and LOOM_CH_TELEMETRY_DATABASE must differ")
        if self.ch_database == "loom_telemetry" or self.ch_telemetry_database == "loom":
            raise ValueError(
                "LOOM_CH_DATABASE must not be 'loom_telemetry', nor "
                "LOOM_CH_TELEMETRY_DATABASE 'loom'"
            )
        # A second schema on the service would read and roll up into the first's.
        if self.ch_database != "loom" and self.ch_telemetry_database == "loom_telemetry":
            raise ValueError(
                "LOOM_CH_DATABASE names a schema outside `loom` (if Loom's schema is in "
                "`loom`, unset it): name its LOOM_CH_TELEMETRY_DATABASE too"
            )
        return self


# ===== Code ===================================================================


class CodeSettings(BaseModel):
    """The code plane: ingest, indexes, search, ask, review."""

    # --- switch, test gate and code.ask ---
    # Master kill switch, checked at the top of every code.* handler but the
    # retirement (code.repo.remove), which only deletes.
    code_intel_enabled: bool = True

    # code.test_gate run recipes (LOOM_CODE_TEST_GATE_RUN_TEMPLATES): JSON list of
    # {"pattern": <regex over the repo-relative test path>, "run": <template>},
    # tried before the built-ins. Templates may use {path} {dir} {stem} {name}
    # {parent}. A path no recipe matches is reported run_unknown, never guessed.
    code_test_gate_run_templates: Annotated[str, JsonArray] = ""

    # Retrieval context (tokens) code.ask hands to synthesis when the caller
    # passes no token_budget.
    code_ask_token_budget: int = 4000
    # The code.ask answer cache's hit cosine. 0 = the per-embedding-model table
    # in code/app/answers.py; a value in (0, 1] pins one threshold for
    # every model.
    code_ask_cache_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    # A cached code.ask answer older than this many days is a miss (checked
    # and tombstoned at read time; no table TTL). 0 = no bound.
    code_ask_cache_max_age_days: int = Field(default=14, ge=0)
    # Seconds an abstain stays cached for its exact normalised question under
    # the index head it abstained at. 0 = off.
    code_ask_negative_cache_ttl_s: int = Field(default=3600, ge=0)
    # code.ask's pre-synthesis LLM calls (identifier proposal, HyDE, concept
    # legs) read the per-namespace exact-prompt response cache; no_cache or a
    # traffic class with llm_cache off bypasses it.
    code_ask_llm_cache: bool = True
    # One code namespace indexes one repository. No repository is configured by
    # default: set LOOM_CODE_REPO / ORG / NAMESPACE (or LOOM_CODE_REPOS_JSON, or add
    # one from the Repositories page) before anything is indexed. The index-building
    # workers (ingest, SCIP, PR history, graph stats, cards, embed recovery) default off
    # and no-op on a namespace without a git_head watermark or pending work.
    # --- repositories ---
    # The scalar repository entry's loom org; empty is never a default for a code.*
    # request, which then must name its own.
    code_org: str = ""
    # The scalar entry's code namespace, used by a code.* request or `loom code` command
    # that names none; empty = the request must name one.
    code_namespace: str = ""
    # The scalar entry's GitHub `owner/name`; empty = no scalar entry, so nothing is
    # indexed until a repository is configured.
    code_repo: str = ""

    # --- code.ask memory enrichment ---
    # The memory namespace code.ask fuses decision/rationale memories (the
    # "why") from into its answer; empty = no enrichment, never guessed.
    code_ask_memory_namespace: str = ""
    # The org of `code_ask_memory_namespace`; empty = `code_org`.
    code_ask_memory_org: str = ""
    # --- repository access ---
    # True lets every authenticated principal of the org read every configured
    # code namespace (right for public or org-internal repos); false leaves reads
    # to the namespace's own visibility and grants.
    # Writes stay grant-driven. Per-entry "org_readable" in LOOM_CODE_REPOS_JSON.
    code_org_readable: bool = False
    # The scalar repo entry is public: its namespace registers join_policy=open
    # and discoverable. Per-entry `"public": true` does the same.
    code_repo_public: bool = False
    # --- checks and conventions ---
    # Positive-control symbol code.test_home / code.coverage_claim grep for (one
    # every test suite of the repository names); empty makes every claim
    # `unknown`.
    code_coverage_control_symbol: str = ""
    # The setting code.setting_facts must find before it claims
    # `not_a_setting` for any other name; empty = it never claims it.
    code_setting_control: str = ""
    # Max bytes of a PR patch code.conventions reads from the mirror; a longer
    # patch is cut at a file boundary and flagged `diff_truncated`.
    code_conventions_diff_max_bytes: int = 1_500_000
    # A touched function is "widely called" when its hop-1 callers span more
    # than this many `src/<dir>` directories (rule wide_callers_untested).
    code_conventions_wide_caller_dirs: int = 5
    # --- multi-repo and self-service ---
    # Multi-repo: JSON array of repo configs (code/domain/index/repos.py);
    # empty = the scalar single-repo settings. Missing per-entry fields fall
    # back to the scalars.
    code_repos_json: Annotated[str, JsonArray] = ""
    # Repositories added from the Repositories page (loom.code_repos) merge with the
    # configured entries (`code_repos_json` and the scalar entry); the environment wins
    # on a namespace collision.
    # Off = the operator's JSON is the only source.
    code_self_service_enabled: bool = True
    # Where the code ingest worker clones a self-service entry's bare mirror (one per
    # GitHub repository, shared by its branches).
    code_repos_root: str = "var/code_repos/self"
    # Active self-service entries per org (branches count); past it the add
    # refuses with a message.
    code_self_service_max_repos: int = 20
    # Files indexed per pass while a new entry's initial index builds; the
    # cursor persists in code_index_state (400 ≈ 1 min at the live rate).
    code_bootstrap_files_per_pass: int = 400
    # Seconds between re-reads of loom.code_repos.
    code_repos_refresh_seconds: int = 30
    # Seconds a bare clone may take before the worker gives up for the pass
    # and retries next pass.
    code_bootstrap_clone_timeout_s: float = 1800.0
    # --- git history and mirrors ---
    # Whether a history read (blame_range, bisect, first_build, lesson checks) on a
    # host without a repository's mirror clones it there in the background. On:
    # every serving host keeps its own full copy of every mirror it is asked about.
    # Off: such a host answers `no_git_mirror` and only the worker's host clones.
    code_history_clone_on_read: bool = True
    # Where git-history reads (blame, citation commit checks, PR history, commit
    # coverage) come from. local = this host's mirrors; none = an edge host with
    # no mirrors: those reads answer without history.
    git_history: Literal["local", "none"] = "local"
    # Every serving process clones the configured entries' mirrors it lacks at boot,
    # in the background (code/app/index/mirrors.py): a history read answers
    # `no_git_mirror: cloning <entry>` until its mirror is there. A full bare clone per
    # host, again on every new disk: keep code_repos_root on a volume that outlives a
    # rollout. Off: a mirror is cloned only when a read asks for it.
    code_mirror_warmup: bool = True
    # Clones the warm-up runs at once on one host (each after a random wait), so N
    # replicas starting together do not all clone from the remote at once.
    code_mirror_warmup_concurrency: int = 2
    # Seconds between this host's fetches of its mirrors and sweeps of the mirrors,
    # checkouts and lock files no active entry names. A round fetches only the branch
    # tips, open PR heads and ledger merges the leader's pass recorded in ClickHouse
    # that the mirror lacks: nothing when it is current. 0 = no fetch loop and no sweep.
    code_mirror_fetch_interval_s: float = 300.0
    # Seconds one git command on a mirror may run (log, show, rev-parse).
    code_git_timeout_s: Annotated[float, LIVE] = Field(default=120.0, gt=0)
    # Seconds one fetch, full-tree `ls-tree`, forced checkout or whole-range diff may
    # run; the first fetch after a shallow clone of a large repository can exceed ten
    # minutes.
    code_git_fetch_timeout_s: Annotated[float, LIVE] = Field(default=3600.0, gt=0)
    # Hours between `git gc` runs of each mirror on this host (low CPU and IO
    # priority, only with room for a second copy plus `code_mirror_gc_margin_gb`).
    # 0 = never.
    code_mirror_gc_interval_h: float = 168.0
    # Free disk, in GB, a mirror's gc must leave beyond the mirror's own size; without
    # it the gc waits another interval.
    code_mirror_gc_margin_gb: float = 5.0
    # Resource limits (rlimits) for each process of a sandboxed repository job
    # (clone, fetch, dependency install, compiler-tier harness;
    # code/app/index/sandbox.py). They bound each process, not a job's total
    # (`code_sandbox_max_pids` aside): there is no cgroup.
    # --- repository job sandbox ---
    # CPU seconds one process of a sandboxed repository job may use.
    code_sandbox_cpu_seconds: int = 7200
    # Data memory (heap and private mappings, RLIMIT_DATA) one process of a sandboxed
    # repository job may use, in MiB. Not address space: node reserves far more of
    # that than it uses.
    code_sandbox_memory_mb: int = 16384
    # Processes and threads one job may run at once (RLIMIT_NPROC, set inside
    # the job's user namespace, so other jobs and the server do not count;
    # Linux 5.14+, older kernels count the uid's tasks host-wide).
    code_sandbox_max_pids: int = 2048
    # Set by deployments that ship the sandbox profiles: a repository job
    # refuses to run rather than run unsandboxed, and /v1/health reports the
    # missing sandbox as an error. False: without the sandbox, jobs fall back
    # to plain processes as "Without the sandbox" in code-index-operations.md says.
    code_sandbox_required: bool = False
    # Largest file one process of a sandboxed repository job may write, in MiB (a
    # pack or index.scip).
    code_sandbox_max_file_mb: int = 32768
    # --- mirror fetch ---
    # Bare clone the ingest worker fetches into (created by `loom code init`). Empty =
    # derived from code_repo: `var/code_repos/<name>.git`.
    code_git_dir: str = ""
    # The scalar entry's fetch remote; empty = `https://github.com/<owner>/<name>.git`.
    code_git_remote: str = ""
    # The scalar entry's branch: the one the ingest worker fetches and indexes.
    code_git_branch: str = "main"
    # Comma-separated fnmatch globs of extra remote branches the mirror
    # fetches besides the configured one (`release/*` = release branches,
    # `backport/*` = backport staging). Refs are never pruned. Empty = the
    # configured branch only.
    code_git_extra_branches: str = ""
    # Fetch ledger merge commits no branch reaches by sha and pin them under
    # refs/loom/merges/<sha>: one ledger page and at most
    # `code_git_ledger_merge_batch` shas per entry per pass; the cursor wraps. Off = branches only.
    code_git_fetch_ledger_merges: bool = True
    # Ledger merge rows checked for a missing sha per entry per pass.
    code_git_ledger_merge_page: int = 500
    # Missing ledger merge shas fetched per entry per pass (40 take about 8.5 s).
    code_git_ledger_merge_batch: int = 40
    # The mirror carries `refs/pull/N/head` for every PR the ledger holds OPEN
    # (refetched when the tip moves, never pruned), so pre-merge reads diff the
    # PR's own head. Only open heads: fetching every PR head is too costly.
    # Per-entry `git_fetch_pr_heads` overrides. Off = the overlay reports
    # `head_not_in_mirror`.
    code_git_fetch_pr_heads: bool = True
    # Max PR heads fetched per shared mirror per ingest pass.
    code_git_pr_head_batch: int = 200
    # --- open-PR fill ---
    # Open-PR rows per pass whose head_sha / base_sha / files the PR-history
    # worker fills from the mirror; rows the mirror lacks fall back to GitHub
    # (`code_gh_items_head_github_batch`).
    code_gh_items_head_fill_batch: int = 200
    # GitHub `GET /pulls/N` calls per pass for open PRs the mirror lacks;
    # 0 = the mirror path only.
    code_gh_items_head_github_batch: int = 20
    # --- ingest ---
    # CSV path prefixes of the scalar entry that are indexed; everything else
    # is skipped. Empty = the whole tree (the ingest denylist still applies).
    code_path_prefixes: str = ""
    # GitHub token for the default repository: git fetches of a private
    # remote, the PR-history and issue mirrors, and fork lookups. Empty = the
    # REST calls go anonymously (60 requests/h per IP, public repos only; one
    # warning) and the issue mirror is skipped.
    code_github_token: str = ""
    # Per-merge fast tier: git-poll → diff → re-chunk/re-embed changed files →
    # tree-sitter symbols/edges → context pack → advance watermark.
    code_ingest_enabled: bool = False
    # Seconds between fast-tier ingest passes; extra-branch fetches of a shared mirror
    # are spaced by 0.9 of it (at least 30 s).
    code_ingest_interval_seconds: int = 120
    # Seconds an ingesting process caches the namespace's live path set (the
    # import-resolution oracle); files it writes join at once.
    code_ingest_live_paths_ttl_s: float = 60.0
    # --- JIT risk model ---
    # Seconds after which the weekly pass refits the jit-risk model.
    code_jit_risk_refit_interval_s: int = 7 * 24 * 3600
    # Months of merged PRs the JIT risk model's fit reads.
    code_jit_risk_window_months: int = 12
    # Days after which a fit is served as `scored_stale` / `fit_stale`.
    code_jit_risk_max_age_days: int = 183
    # --- staleness (as_of.stale) ---
    # as_of.stale is true when the fast tier is more than this many
    # first-parent commits behind the fetched branch tip (60 s cache).
    code_stale_after_commits: int = 5
    # as_of.stale is also true when the mirror has not fetched for this many
    # seconds (lag is counted against the local mirror). 0 = off.
    code_fetch_stale_after_seconds: int = 3600
    # A code-index tier failing this many passes in a row is a `code_tier_failing`
    # health problem (warn); fewer is a transient failure the next pass retries.
    code_tier_failing_warn_passes: Annotated[int, LIVE] = Field(default=3, ge=1)
    # A code-index tier failing for this many hours is a `code_tier_failing` health
    # problem (warn), however few passes ran in that time.
    code_tier_failing_warn_hours: Annotated[float, LIVE] = Field(default=3.0, gt=0)
    # A `code_tier_failing` health problem is an error when the failing tier is also
    # this many commits behind the fetched tip; the fetch tier, whose lag is unknown,
    # stays a warn.
    code_tier_failing_error_lag_commits: Annotated[int, LIVE] = Field(default=100, ge=1)
    # --- code.callers ---
    # code.callers/callees: a bare name with more distinct definitions than
    # this returns `ambiguous` + candidates instead of walking them all.
    code_callers_max_distinct_seeds: int = 5
    # The floor below which a declined site code.callers / code.callees return
    # is counted, not returned; a request may pass `candidate_min_confidence`.
    code_callers_candidate_min_confidence: float = 0.2
    # --- ingest pass budget ---
    # Commits past the watermark one pass reads; later ones wait for the next pass.
    code_ingest_max_commits_per_pass: int = 20
    # Prior ingest cost per changed file (seconds), refined by observation. A
    # commit predicted to overrun the pass deadline is not started unless it
    # is the first of its pass.
    code_ingest_seconds_per_file: float = 2.5
    # Changed files indexed per commit; past it the rest are skipped with a warning
    # and heal on the next backfill.
    code_ingest_max_files_per_commit: int = 500
    # The ingest pass's own deadline (seconds), kept at the supervisor's cap;
    # `code_ingest_pass_budget_fraction` keeps a pass under it.
    code_ingest_max_pass_seconds: int = 900
    # Share of the commit phase one oversized commit may take per pass while
    # other entries wait (it is sliced across passes).
    code_ingest_slice_share: float = Field(default=0.5, ge=0.05, le=1.0)
    # Fraction of the deadline after which the pass stops STARTING units,
    # finishes the in-flight round, saves cursors and returns `partial: true`.
    code_ingest_pass_budget_fraction: float = 0.75
    # Share of the ingest pass budget (`code_ingest_pass_budget_fraction` of the
    # deadline) the commit phase leaves to the walk (reconcile heal,
    # extractor re-chunk). 0 = commits may take it all.
    code_ingest_walk_budget_share: float = 0.3
    # Files per round in the walk, the bootstrap slice and a commit's file
    # list; a round prefetches its per-file resolution reads in two
    # statements. 0 = per-file reads.
    code_ingest_round_files: int = 25
    # Entries whose commits ingest side by side in the commit phase. Each
    # entry's own commits stay one at a time, oldest first (the watermark
    # contract); a lane waits on ClickHouse round trips, not on its capacity,
    # so lanes add throughput. 1 = one commit at a time across all entries.
    code_ingest_lanes: int = Field(default=1, ge=1, le=8)
    # --- vector table compaction ---
    # A bulk `loom code` pass that wrote at least this many chunk rows ends by
    # compacting the prefix/twin vector tables past code_compact_min_ratio.
    # 0 = off; loom.code_chunks is compacted only by the CLI.
    code_compact_after_bulk_rows: int = 10_000
    # Copies per key (active rows / distinct keys) at which a code vector table
    # is OPTIMIZE ... FINAL'd.
    code_compact_min_ratio: float = Field(default=1.5, gt=0.0)
    # --- compiler-tier indexes (SCIP) ---
    # SCIP delta apply (compiler-grade tier uploaded via code.scip_ingest).
    code_scip_apply_enabled: bool = False
    # Seconds between SCIP apply passes.
    code_scip_apply_interval_seconds: int = 60
    # Max bytes of one code.scip_ingest payload part; a larger part is refused.
    code_scip_max_part_bytes: int = 8 * 1024 * 1024
    # Max `total_parts` of one code.scip_ingest upload; more is refused.
    code_scip_max_parts: int = 512
    # C++ compiler-tier per-merge deltas: scip-clang over changed TUs when
    # git_head outruns scip_commit. Needs the configured checkout and a
    # scip-clang binary; CPU-heavy in bursts.
    code_scip_delta_enabled: bool = True
    # Seconds between C++ delta passes.
    code_scip_delta_interval_seconds: int = 300
    # The scalar entry's working tree the compiler tiers index; other entries default to a
    # sibling named after their namespace, and an entry's `scip_checkout_dir` wins.
    code_scip_checkout_dir: str = "var/code_repos/checkout"
    # The scip-clang binary: a name looked up on PATH, or a path. The image
    # carries it on amd64 only (upstream builds no arm64 Linux binary).
    code_scip_clang_path: str = "scip-clang"
    # scip-clang's parallel jobs in a delta or full run; 0 = half the CPUs the
    # host shows (a container sees the node's). Each job holds a translation
    # unit's AST: a few GiB for a large C++ tree.
    code_scip_clang_jobs: int = 0
    # Where the C++ harness writes its index and delta (several GB for a full
    # run of a large tree); "" = the system temp dir.
    code_scip_work_dir: str = ""
    # Max translation units one delta indexes; past it only the changed `.cpp` files are
    # indexed and the header fan-out heals on the next full run.
    code_scip_delta_max_tus: int = 200
    # Seconds the C++ delta harness may run before it is killed.
    code_scip_delta_timeout_seconds: int = 3600
    # C++ full re-index: seeds an entry the delta has no watermark for, and
    # re-indexes each entry every `code_scip_full_every_hours` (heals deltas
    # that hit the TU cap). One entry per pass, under its entry lock.
    code_scip_full_enabled: bool = True
    # Seconds between the C++ full re-index's checks for an entry that is due.
    code_scip_full_interval_seconds: int = 900
    # Hours between full re-indexes of one entry.
    code_scip_full_every_hours: int = 168
    # Seconds the full re-index harness (CMake configure, scip-clang, convert)
    # may run; the checkout move and the apply get 6 h more in the pass deadline.
    code_scip_full_timeout_seconds: int = 8 * 3600
    # SQL <-> C++ crossref (code_test_map from registrations and test queries)
    # for each C++ entry whose git_head moved since its last run.
    code_crossref_enabled: bool = True
    # Seconds between crossref passes.
    code_crossref_interval_seconds: int = 1800
    # Directory of the per-entry lock files every compiler-tier pass on this
    # host takes (`scip.<org>.<namespace>.lock`): a full re-index and a delta
    # never work on one entry at once; different entries run side by side.
    # `loom code entry-lock` takes the same lock.
    code_scip_lock_dir: str = "var/cron/locks"
    # TypeScript compiler-tier deltas: scip-typescript over an entry with
    # `scip_producer` "typescript" when git_head outruns scip_commit_ts. The
    # worker prepares dependencies with lifecycle scripts disabled; needs Node
    # 22+ and the toolchain from scripts/install-code-toolchains.sh (in the image).
    code_scip_ts_delta_enabled: bool = True
    # Seconds between TypeScript delta passes.
    code_scip_ts_delta_interval_seconds: int = 600
    # The scip-typescript binary: a name looked up on PATH, or a path.
    code_scip_typescript_path: str = "scip-typescript"
    # Seconds the TypeScript delta harness may run before it is killed.
    code_scip_ts_delta_timeout_seconds: int = 3600
    # Rewrite consumers' tsconfig `paths` from workspace `exports` before
    # indexing an unbuilt checkout; off for a built checkout.
    code_scip_ts_paths: bool = True
    # Node heap size (MiB) given to scip-typescript.
    code_scip_ts_node_heap_mb: int = 8192
    # Python compiler-tier deltas: scip-python over an entry with
    # `scip_producer` "python", over the files the window touched (a first run
    # indexes the tree). Needs node and scip-python.
    code_scip_py_delta_enabled: bool = True
    # Seconds between Python delta passes.
    code_scip_py_delta_interval_seconds: int = 600
    # The scip-python binary: a name looked up on PATH, or a path.
    code_scip_python_path: str = "scip-python"
    # Seconds the Python delta harness may run before it is killed.
    code_scip_py_delta_timeout_seconds: int = 3600
    # Node heap size (MiB) given to scip-python; Pyright dies silently at 8 GB.
    code_scip_py_heap_mb: int = 16384
    # Go compiler-tier deltas: scip-go per touched module of an entry with
    # `scip_producer` "go". Needs the Go toolchain and scip-go
    # (`go install github.com/scip-code/scip-go/cmd/scip-go@v0.2.7`, the image's version).
    code_scip_go_delta_enabled: bool = True
    # Seconds between Go delta passes.
    code_scip_go_delta_interval_seconds: int = 900
    # The scip-go binary, looked up on PATH or in `code_scip_go_bin_dir`.
    code_scip_go_path: str = "scip-go"
    # Directory holding `go` and scip-go when they are not on PATH; empty = PATH only.
    code_scip_go_bin_dir: str = ""
    # Seconds the Go delta harness may run before it is killed; also the
    # pass deadline.
    code_scip_go_delta_timeout_seconds: int = 7200
    # Go modules the delta harness builds in parallel (about 4 GB of memory each).
    code_scip_go_jobs: int = 2
    # Root of the Go module caches kept across passes: one per entry beneath it
    # (<root>/<org>/<namespace>), never shared between repositories.
    code_scip_go_modcache: str = "var/gocache"
    # GOPRIVATE and GONOSUMDB for the Go delta harness: module paths fetched directly,
    # without the proxy or checksum database (a vanity import host). Empty = none.
    code_scip_go_private: str = ""

    # --- PR history ---
    # Merged-PR history sync (GitHub REST, cursor-resumable).
    code_pr_history_enabled: bool = False
    # Seconds between PR-history sync passes.
    code_pr_history_interval_seconds: int = 900

    # --- the set-wide backport report ----------------------------------------
    # Nightly pass: runs `code.backport_gap(exact, history)` for every trunk PR of the
    # last `code_backport_gaps_days` labelled for backport and writes one row per
    # release branch into loom.code_backport_gaps. Paced one fix per
    # `code_backport_gaps_pace_s`, at most `code_backport_gaps_prs_per_pass` a night,
    # stalest first, stopping at `code_backport_gaps_budget_s`; settled fixes are
    # skipped. Off = nothing written; the read op serves the table.
    code_backport_gaps_enabled: bool = True
    # Seconds between backport-gaps passes; the read op reports the table stale past
    # twice this.
    code_backport_gaps_interval_seconds: float = 86400.0
    # Days back of backport-labelled trunk PRs a backport-gaps pass considers.
    code_backport_gaps_days: int = 90
    # Max fixes a backport-gaps pass checks, stalest first.
    code_backport_gaps_prs_per_pass: int = 300
    # Seconds a backport-gaps pass pauses between two fixes.
    code_backport_gaps_pace_s: float = 1.0
    # Seconds after which a backport-gaps pass stops starting fixes.
    code_backport_gaps_budget_s: float = 1800.0
    # A backport-gaps pass's hard deadline (seconds) under the worker supervisor.
    code_backport_gaps_max_pass_seconds: float = 3600.0
    # --- coverage watches -----------------------------------------------------
    # Coverage watches: post one code.coverage.ready event on the watcher's task
    # inbox once every index tier covers a watched commit. A pass with no
    # pending watch is one read of an empty table.
    code_coverage_notify_enabled: bool = True
    # Seconds between coverage-notify passes while watches are pending.
    code_coverage_notify_interval_seconds: int = 60
    # Seconds between coverage-notify passes that found no pending watch.
    code_coverage_notify_idle_interval_seconds: int = 120
    # --- bisect ---------------------------------------------------------------
    # Days of the repo's own PR history bisect reads back from the window's
    # end when there is no tracker (or no changed files) and no `after`.
    code_bisect_history_days: int = Field(90, ge=1, le=3650)
    # Minor releases searched below the failing one when the report names no
    # clean one.
    code_bisect_lookback_minors: int = Field(6, ge=1, le=24)
    # Merge-time window (days) for a report naming no release.
    code_bisect_window_days: int = Field(90, ge=7, le=730)
    # The rerank's wall budget (seconds); a timeout keeps the fused order.
    code_bisect_rerank_timeout_s: float = Field(6.0, ge=1.0, le=30.0)
    # The wide read's merge-time window (days) with no release named.
    code_bisect_wide_days: int = Field(180, ge=30, le=730)
    # The dupcheck's budget inside a bisect, from the call's
    # start; it runs beside the resolve, the scan and the fusion reads and is
    # awaited only before the ranking stages. Past it the bisect answers
    # without it (`dupcheck_fix.status: timeout`).
    code_bisect_dupcheck_timeout_s: float = Field(4.0, ge=0.5, le=30.0)
    # --- PR review comments ---------------------------------------------------
    # Also fetch each merged PR's inline review comments into
    # loom.code_pr_reviews (one request per PR; skipped without a GitHub token).
    code_pr_reviews_enabled: bool = True
    # --- GitHub issues and PRs mirror -----------------------------------------
    # Keep loom.code_gh_items current: every issue and PR updated since the
    # last pass via /issues; the first pass reaches back backfill_days.
    # Skipped anonymously.
    code_gh_items_enabled: bool = True
    # Days back the first mirror pass (and `loom code` with no start date) reads.
    code_gh_items_backfill_days: int = 730
    # --- code.pr_check -------------------------------------------------------
    # Verdict: `duplicate_of` when a candidate's heuristic score reaches the
    # duplicate line (a closed-unmerged PR caps at related), `related` at the
    # related line, else `none`. A title alone never reaches duplicate.
    code_pr_check_duplicate_threshold: float = 0.55
    # Score at or above which a candidate is `related`.
    code_pr_check_related_threshold: float = 0.2
    # Days back code.pr_check reads merged PRs; older ones do not vote as duplicates.
    code_pr_check_merged_days: int = 365
    # Days back code.pr_check reads PRs closed without a merge.
    code_pr_check_closed_days: int = 180
    # Files above which a code.pr_check candidate is a mass change, whose files do not
    # vote.
    code_pr_check_mass_files: int = 200
    # --- merged-PR backfill ---------------------------------------------------
    # Months of merged PRs `loom code backfill-prs` (and a cold worker)
    # fetch; matches the churn window in code/app/risk.py.
    code_pr_backfill_months: int = 12
    # --- embedding recovery ---------------------------------------------------
    # Embedding recovery for code_chunks/code_summaries stubs (dim=0).
    code_embed_recovery_enabled: bool = False
    # Seconds between recovery passes while stubs are pending.
    code_embed_recovery_interval_seconds: int = 30
    # Sleep after a code embed-recovery pass that found no pending stub;
    # `code_embed_recovery_interval_seconds` resumes once a pass writes a row.
    code_embed_recovery_idle_interval_seconds: int = 120
    # The pending scan reads above a version watermark; once per this many
    # seconds it scans from version 0 to catch late rows. 0 = always from 0.
    code_embed_recovery_full_scan_seconds: int = 1800
    # Embed attempts per stub before it is poisoned (`embed_failed_at` set) and skipped.
    code_embed_max_attempts: int = 8
    # Stub keys the recovery scan takes per repo per pass (also texts per
    # embed call); raise it to onboard large repositories faster.
    code_embed_recovery_batch: int = 100
    # Share of embeddable chunks that must carry a vector before a repo is
    # `ready`; below it code.search says "lexical only". Poisoned rows count
    # against it; fixtures unembedded by design do not.
    code_ready_embed_coverage: float = 0.95
    # --- ingest limits and denylist -------------------------------------------
    # Files above this size are recorded (index_error) and not chunked.
    # 0 = no cap.
    code_ingest_max_file_bytes: int = 1_000_000
    # True applies the built-in generated, minified and vendored ingest rules
    # (code/app/index/denylist.py), which match 0.19% of rows. The `exclude` globs
    # (CSV here, a list per repo entry) and the repo-local ignore file are always
    # honoured.
    code_ingest_denylist_builtin: bool = False
    # CSV of path globs never indexed (the scalar entry's `exclude`).
    code_ingest_exclude: str = ""
    # Name of the repo-local ignore file whose patterns are never indexed.
    code_ingest_ignore_file: str = ".loomignore"
    # --- reconciliation and re-chunking ---------------------------------------
    # Content-based reconciliation (code/app/index/reconcile.py): each
    # interval, compare every path's chunk commit with the branch head and
    # re-ingest up to `code_reconcile_heal_per_pass` stale paths.
    code_reconcile_enabled: bool = True
    # Seconds between reconciliation runs per namespace.
    code_reconcile_interval_seconds: int = 3600
    # Stale paths re-ingested per run.
    code_reconcile_heal_per_pass: int = 200
    # Max distinct chunk commits a run compares; past it the stale set is a floor
    # (`truncated`).
    code_reconcile_max_commits: int = 1000
    # When the recorded parser/chunker/grammar stamp differs from the running
    # process, re-chunk the namespace in paced batches per ingest pass. A first
    # observation is recorded, never acted on.
    code_extractor_rechunk_enabled: bool = True
    # Live paths re-chunked per ingest pass while the stamp differs.
    code_extractor_rechunk_per_pass: int = 200
    # Files a re-chunk re-derives at once. A re-derive waits mostly on its
    # ClickHouse reads and writes, so raising this shortens a heal roughly in
    # proportion, at that much more concurrent load on the cluster.
    code_extractor_rechunk_concurrency: int = 4
    # --- dependents, closure repair and call guards ---------------------------
    # After a commit, re-extract edges only on untouched files whose call
    # sites or edges reference symbols it added, removed or tombstoned (or,
    # opt-in, that include a touched header). Capped per commit; overflow
    # drains through reconcile. A replay closed every gap at 20% of the ingest
    # wall.
    code_ingest_dependents_enabled: bool = True
    # Dependent files re-extracted per commit; the overflow queues for reconcile.
    code_ingest_dependents_max_files: int = 200
    # Dependents re-extracted at once; the batch is bound by ClickHouse read
    # throughput, so more is not faster.
    code_ingest_dependents_concurrency: int = Field(default=4, ge=1, le=64)
    # Size cap of the dependents re-extraction overflow queue; paths past it are dropped
    # and counted.
    code_ingest_dependents_pending_max: int = 5000
    # Closure repair: a bare function name the commit added with exactly ONE
    # live definition repo-wide binds its `unresolved` sites at 0.6 without
    # re-parsing; several definitions leave them declined. Capped per commit.
    code_closure_repair_enabled: bool = True
    # Added and removed names per commit closure repair considers; the rest are skipped.
    code_closure_repair_max_names: int = 500
    # Unresolved call sites closure repair reads per commit for the names it binds.
    code_closure_repair_max_sites: int = 2000
    # --- graph and symbol statistics ------------------------------------------
    # Weekly graph statistics: PageRank, degrees, test_map rebuild.
    code_graph_stats_enabled: bool = False
    # Seconds between graph-statistics passes.
    code_graph_stats_interval_seconds: int = 7 * 24 * 3600
    # A graph-statistics pass's deadline (seconds); a pass past it is cancelled and
    # counted a failure.
    code_graph_stats_max_pass_seconds: int = 3600
    # Seconds from the start of a graph-statistics pass during which its unit-risk
    # leg may schedule rounds; past it the pass succeeds with `unit_risk_partial: true` and the
    # next resumes. Must stay under `code_graph_stats_max_pass_seconds`.
    code_graph_stats_unit_risk_budget_s: int = 2400
    # Fan-in at or above which the `uses` block says `skipped: hub` instead of
    # listing sites. 0 = never.
    code_symbol_uses_hub_fan_in: int = 200
    # --- summary cards --------------------------------------------------------
    # Weekly LLM file/module summary cards (inputs_hash-gated).
    code_summary_enabled: bool = False
    # Seconds between card passes.
    code_summary_interval_seconds: int = 7 * 24 * 3600
    # Cards written per pass across every repository, shared out evenly (a
    # share one repository does not use rolls to the next).
    code_summary_max_scopes_per_pass: int = 200
    # Cards in flight per pass; 1 = sequential.
    code_summary_concurrency: int = 1
    # Days of merged PRs the summary cards' churn order counts: a bounded card pass
    # considers files by merged-PR count per path over these days (source tree), then
    # by include-graph rank.
    code_summary_churn_days: int = 365
    # CSV path prefixes the summary cards' churn order may propose; empty = any.
    code_summary_churn_prefixes: str = ""
    # CSV of path prefixes the summary cards' churn order never proposes, at any depth
    # (vendored trees).
    code_summary_churn_exclude: str = ""
    # CSV of summary models whose cards are rewritten when code_summary_model
    # differs, even for an unchanged file. Empty = an unchanged file keeps its
    # card.
    code_summary_upgrade_from_models: str = ""
    # --- chunking and language lanes ------------------------------------------
    # Chunking: whole AST units; functions larger than this split at top-level
    # statement boundaries, each piece re-prefixed with the signature header.
    code_chunk_max_chars: int = 6000
    # SQL tests as statement records: one `statement` symbol per record,
    # chunks packed by whole statements, facet rows in loom.code_test_facets.
    # Off = line-window chunks for `.sql`. Existing namespaces need `loom code
    # backfill --paths-file` (outside the extractor stamp).
    code_sql_statement_records: bool = False
    # Statement packing target (chars); a larger single statement splits at
    # lines at `code_chunk_max_chars`.
    code_sql_chunk_target_chars: int = Field(default=2000, ge=1)
    # SQL a Python file runs (database-client call arguments and SQL-looking
    # literals) becomes `statement` symbols, facets (confidence 0.7 with a
    # format hole) and table edges. Off = functions only. Needs a
    # `--paths-file` backfill of the Python tree.
    code_py_sql_islands: bool = False
    # Column-level SQL lineage: `<table>.<column>` symbols, `column_read` /
    # `column_write` facets and statement → column edges, for `.sql` and
    # Python SQL islands. Off = table-level lineage. Needs a
    # `--paths-file` backfill.
    code_sql_column_lineage: bool = False
    # A shell test's SQL strings (`$CLICKHOUSE_CLIENT -q`, heredocs, `echo |`)
    # become `statement` symbols with facets; leftover chunks coalesce up to
    # `code_shell_chunk_target_chars`, never across a function. Needs a `--paths-file`
    # backfill.
    code_shell_islands: bool = False
    # Leftover coalescing target in chars for shell files.
    code_shell_chunk_target_chars: int = Field(default=2000, ge=1)
    # Shell inside CI/build files (Actions `run:`, GitLab `script:`,
    # Dockerfile `RUN`, Makefile recipes, Jenkinsfile `sh`) is parsed for
    # facets, script `includes` edges and SQL; jobs, targets and stages become
    # symbols, and extensionless Dockerfile / Makefile / Jenkinsfile index as
    # config. Needs a `--paths-file` backfill.
    code_host_islands: bool = False
    # --- chunk descriptions and ingest rounds ---------------------------------
    # Contextual-embedding one-liner per chunk, inline on the per-merge path;
    # falls back to structural-only on any LLM failure.
    code_context_description_enabled: bool = True
    # Shape of the embedded chunk description: `oneliner` (what the code does)
    # or `questions` (file card + chunk → what it does and the questions it
    # answers). Changing it re-texts new rows only.
    code_context_description_prompt: Literal["oneliner", "questions"] = "oneliner"
    # A file's chunk descriptions in flight at once; 1 = serial.
    code_describe_concurrency: int = Field(default=8, ge=1, le=64)
    # Model calls one round runs at once.
    code_ingest_round_model_concurrency: int = Field(default=16, ge=1, le=128)
    # Buffered rows at which an ingest round writes a table early. A round writes one
    # INSERT per table, flushed when a read could see the rows, at this row count, or
    # at round end.
    code_ingest_round_write_rows: int = Field(default=2000, ge=1, le=100_000)
    # --- bot authors -----------------------------------------------------------
    # Comma-separated GitHub logins of autonomous agents that author pull requests
    # or issues in the indexed repositories, beyond the generic bots (dependabot,
    # github-actions, …). They are never suggested as owners, their names are not
    # tracker entities, and bisect ignores the lines they sign.
    code_bot_authors: str = ""
    # --- search ranking, legs and caches (code.search) ------------------------
    # Fire-and-forget code_search_trace/code_search_outcome telemetry.
    code_trace_log_enabled: bool = True
    # Post-fusion rank priors in code.search (path-noise penalty, query↔path
    # stem affinity, file coherence), applied after RRF and before the outcome
    # boost. Kill switch.
    code_fusion_signals: bool = True
    # Long-question trim: past this many lexemes the question is treated as an
    # issue body (template headings stripped, the rarest 32 tokens gate, the
    # first 512 cleaned words embed). Under the cap nothing changes. 0 = off.
    code_search_query_max_tokens: int = 48
    # Per-file saturation: the 2nd, 3rd... unit from one path is multiplied by
    # decay^excess before the cut; a file's best unit never decays. 0 = off.
    # Skipped under path_prefix, symbol routes and test intent.
    code_search_file_saturation: float = 0.5
    # CSV of vector-model specs a request may name as `vector_model` (e.g.
    # "prefix:512:text-embedding-3-large"); only the vector leg switches. Empty = the
    # override is refused.
    code_search_vector_models: str = ""
    # JSON {label: {"table": "loom.x", "dim": 256}}: where a label's chunk
    # rows live (the coded vector column is fixed-width).
    # Absent label = loom.code_chunks. Inserts and the vector leg route by it.
    code_vector_tables: Annotated[str, JsonObject] = ""
    # With a vector_model set and this > 0, the second model's top-N ids are
    # rescored by exact cosine on the served vectors. 0 = its own distances
    # rank. Per-request CodeSearchInput.vector_shortlist.
    code_search_vector_shortlist: int = 0
    # Over-fetch multiplier of the file-card leg's codes shortlist once
    # loom.code_summaries carries the codec.
    code_search_cards_fetch_multiplier: float = 10.0
    # Default vector-leg shortlist label (`prefix:<dim>:<served model>`, listed
    # in code_search_vector_models and code_vector_tables), rescored by the
    # served vectors. Needs code_search_vector_shortlist > 0; an empty
    # shortlist falls back to the full scan. Empty = off.
    code_search_vector_model_default: str = ""
    # Per-result risk annotations (churn/amp/tested) and one-hop callee
    # context on code.search results; reserves part of the token budget.
    # Kill switch.
    code_risk_annotations: bool = True
    # Age (seconds) past which a risk row is stale and annotated live; must
    # exceed the weekly pass interval.
    code_unit_risk_max_age_s: int = 10 * 24 * 3600
    # code.search `bodies` when the request omits it: "auto" fills bodies into
    # the budget; "none" serves handles only.
    code_search_bodies_default: Literal["auto", "none"] = "auto"
    # Memoise served code.search responses per (org, namespace, request shape)
    # in loom.code_search_memo and replay within the TTL (route "+memo");
    # `fresh=true` bypasses it.
    code_search_memo_enabled: bool = True
    # Seconds a memoised response, and a cursor's stored pool, stay replayable.
    code_search_memo_ttl_s: int = Field(default=1800, ge=1)
    # Max bytes of a response the memo stores; a larger one is not memoised.
    # 0 = no cap.
    code_search_memo_max_payload_bytes: int = 200_000
    # Seconds the BM25 corpus statistics (term df, chunk count, mean length)
    # are cached per process; 0 = no cache. Bounded by
    # `code_corpus_stats_max_terms`, oldest out.
    code_corpus_stats_ttl_s: int = 300
    # Max terms whose df the per-process cache holds; the oldest goes first.
    code_corpus_stats_max_terms: int = 50_000
    # A computed code.search / code.grep stores its ranked pool (no bodies)
    # in loom.code_search_memo and issues `next_cursor`; the next page is a
    # point read. The pool size is `code_search_cursor_pool`.
    code_search_cursor_enabled: bool = True
    # Units kept in a cursor's ranked pool; capped at 200.
    code_search_cursor_pool: int = Field(default=100, ge=1)
    # Wall deadline (ms) per namespace in a multi-namespace search; a late
    # namespace is dropped and named in `fanout.dropped`.
    code_search_fanout_timeout_ms: int = 3000
    # Max namespaces one request fans out to; can only lower the wire cap of 8.
    code_search_fanout_max_namespaces: int = 8
    # A `pattern` admitting more files than this is refused with
    # `pattern_too_broad` and the count (1,500 = 5% of a large namespace's files).
    code_search_pattern_max_files: int = 1500
    # The card leg: file cards are searched by cosine and the top files' chunks
    # join fusion as a capped RRF list (the history leg's shape). No per-request
    # override.
    code_search_card_leg: bool = False
    # Files the card leg votes for.
    code_search_card_leg_files: int = 15
    # Skeleton chunks per voted file (the history leg's shape).
    code_search_card_leg_units_per_file: int = 2
    # What a voted file contributes: `skeleton` (its class skeletons) or
    # `best_chunk` (its source chunks nearest the query by exact cosine).
    code_search_card_leg_mode: Annotated[Literal["skeleton", "best_chunk"], CaseFolded] = "skeleton"
    # Max share of the best two-leg RRF score a card row contributes.
    code_search_card_leg_cap: float = 1.0
    # CSV of prose question kinds the card leg votes on; `any` = all.
    code_search_card_leg_kinds: str = "responsibility"
    # Reranker code.search and code.ask use when the request names none (same
    # values as the per-request `rerank`: a builtin or a name in
    # LOOM_RERANK_ENDPOINTS_JSON; rerank="none" turns it off). Empty = no default rerank.
    code_search_rerank_default: str = ""
    # Prose question kinds the default rerank applies to (`any` = all);
    # symbol-shaped or constrained searches are never default-reranked.
    code_search_rerank_kinds: str = "responsibility"
    # How many fused units the default rerank reads.
    code_search_rerank_top: int = 20
    # Characters of each candidate shown to the default rerank (path and
    # signature first, then the body head).
    code_search_rerank_excerpt_chars: int = 300
    # Fusion multiplier for settings-catalogue units (Settings.cpp and kin) on the
    # question kinds `code_search_catalogue_kinds` names; 1.0 = off.
    code_search_catalogue_penalty: float = 1.0
    # Fusion multiplier for prose files (documentation, release notes, changelogs)
    # unless the query asks for documentation or the search is constrained to paths;
    # 1.0 = off.
    code_search_prose_penalty: float = 0.25
    # The question kinds `code_search_catalogue_penalty` applies to.
    code_search_catalogue_kinds: str = "responsibility"
    # Cap of code.search's fan-in rank prior:
    # score *= 1 + cap * log1p(fan_in) / log1p(max fan_in in the pool);
    # skipped under test intent and on noise paths; inert without the stats.
    # 0 = off.
    code_fan_in_prior_cap: float = 0.25
    # Lexical list's RRF weight for a responsibility question (vector = 1.0).
    # 1.0 = equal weights.
    code_search_rrf_lexical_weight_responsibility: float = 1.0
    # A prose question whose nearest file card's cosine is below this
    # abstains `off_domain`; independent of the card leg. 0 = off.
    code_search_abstain_card_below: float = 0.0
    # Calibrated confidence below which a result is flagged
    # `low_calibrated_confidence` (still returned). The deployment sets 0.30.
    code_search_abstain_confidence_below: float = 0.35
    # The calibrated confidence from which a code.search result reads
    # `retrieval_risk` "medium"; below it the result reads "high".
    # Code embeddings score a right hit lower than memory text does: on the
    # conceptual question set every right top 3 scored 0.40–0.63, so the
    # memory plane's 0.55 / 0.75 labelled three in four right answers "high".
    code_search_medium_risk_from: float = 0.40
    # The calibrated confidence from which a code.search result reads `retrieval_risk`
    # "low" (see code_search_medium_risk_from).
    code_search_low_risk_from: float = 0.55
    # The question kinds the hypothetical answer votes on.
    code_search_hyde_kinds: str = "responsibility"
    # Cap on the hypothetical-answer (HyDE) leg's candidate list, as a multiple of the
    # two-leg top (the history rule).
    code_search_hyde_cap: float = 1.0
    # Tokens the hypothetical-answer (HyDE) passage may run to.
    code_search_hyde_max_tokens: int = 220
    # Default code.search request budget (ms) when the request sets no
    # `deadline_ms`; what it cuts (late legs, rerank) makes the answer
    # `partial`. 0 = none.
    code_search_deadline_ms: int = 0
    # Ms a model-backed leg (HyDE) may run past the last plain leg before it
    # is dropped. 0 = no bound.
    code_search_llm_leg_grace_ms: int = 0
    # Default `llm_legs`: `auto` runs the configured model-backed legs; `none`
    # skips them.
    code_search_llm_legs_default: Annotated[Literal["auto", "none"], CaseFolded] = "auto"
    # Max share of the best two-leg RRF score a concept unit contributes.
    code_search_concept_expand_cap: float = 0.5
    # Identifiers + phrases code.search's concept expansion keeps from the model's answer.
    code_search_concept_expand_max_terms: int = 8
    # CSV of prose question kinds the concept leg expands; `any` = all.
    code_search_concept_expand_kinds: str = "responsibility"
    # Expansion tokens below this BM25 idf are dropped (2.0 ≈ in more than an
    # eighth of chunks).
    code_search_concept_expand_min_idf: float = 2.0
    # History leg: merged-PR text scored lexically votes for the files each PR
    # touched, as a third RRF list. "off" | "issue" (issue-shaped queries) |
    # "always".
    code_search_pr_leg: Literal["off", "issue", "always"] = "issue"
    # PRs code.search's PR leg reads per query.
    code_search_pr_leg_pool: int = 30
    # Files of the PRs code.search's PR leg reads that may vote per query.
    code_search_pr_leg_max_files: int = 10
    # Chunk units a file voted by code.search's PR leg contributes.
    code_search_pr_leg_units_per_file: int = 2
    # A namespace with fewer live voting PRs than this skips code.search's PR leg.
    code_search_pr_leg_min_prs: int = 500
    # Per-unit cap on code.search's PR leg contribution, as a fraction of the top
    # two-leg fusion score.
    code_search_pr_leg_cap: float = 0.5
    # Only files under these prefixes (CSV) receive votes from code.search's PR leg;
    # "" = every file.
    code_search_pr_leg_path_prefixes: str = ""
    # History intent: a question about a change is answered from the PR plane
    # as a `history` section. "off" | "number" (explicit issue/PR numbers,
    # point reads) | "on" (also why/when/who-changed phrasings).
    code_search_history_route: Literal["off", "number", "on"] = "number"
    # PR rows served in code.search's `history` section (no bodies; inside the token
    # budget).
    code_search_history_max_prs: int = 3
    # Max PR files of a number intent that anchor the ranking (present files
    # stay, absent ones insert at rank 1); a phrase intent never anchors.
    code_search_history_anchor_max_files: int = 5

    # --- measured coverage ---------------------------------------------------
    # Import per-test line coverage from an instrumented CI run into the test
    # map (`method='coverage'`) and the per-file executed lines
    # (`code_coverage_files`). Off by default; `loom code coverage-import`
    # runs one import by hand whatever this says.
    code_coverage_import_enabled: bool = False
    # ClickHouse HTTP endpoint holding the coverage tables
    # (`checks_coverage_lines`, `checks`); "" = no source, the import is a no-op.
    code_coverage_source_url: str = ""
    # User the coverage import signs in to the source endpoint as.
    code_coverage_source_user: str = ""
    # Password of `code_coverage_source_user`.
    code_coverage_source_password: str = ""
    # Database of the coverage source's tables.
    code_coverage_source_database: str = "default"
    # Namespaces (CSV) whose repository the source's runs cover; every other
    # namespace is skipped.
    code_coverage_namespaces: str = ""
    # The run's coverage is read in this many slices by file hash, two
    # statements per slice. One slice must fit the source server's per-query
    # memory, and a source with a per-statement-shape hourly quota (a public
    # playground allows 100) must take a whole import plus a retry: a nightly
    # run read in 16 slices is 16 statements of each shape.
    code_coverage_shards: int = Field(default=16, ge=1, le=4096)
    # A line executed by more tests than this names none of them (shared
    # plumbing every query runs); a test-map row needs a line under the cap.
    code_coverage_specific_max_tests: int = Field(default=200, ge=1)
    # Seconds one source query may take before the import stops (no rows
    # are tombstoned by an import that did not finish).
    code_coverage_source_timeout_s: float = 300.0
    # Seconds the coverage import waits to connect to the source endpoint.
    code_coverage_source_connect_timeout_s: float = Field(default=10.0, gt=0)
    # Check names (a LIKE pattern) whose runs carry per-test coverage.
    code_coverage_check_pattern: str = "%per_test_coverage%"
    # Jobs of one run start within this many hours of its newest job.
    code_coverage_run_window_hours: int = Field(default=6, ge=1)
    # Seconds between coverage import passes.
    code_coverage_import_interval_seconds: float = 86400.0
    # Seconds one coverage import pass may run before the supervisor abandons it.
    code_coverage_import_max_pass_seconds: float = 7200.0

    def code_coverage_namespace_set(self) -> frozenset[str]:
        """`code_coverage_namespaces` as a set."""
        return frozenset(n.strip() for n in self.code_coverage_namespaces.split(",") if n.strip())

    def code_bot_author_names(self) -> frozenset[str]:
        """`code_bot_authors` as a set of lower-cased logins."""
        return frozenset(a.strip().lower() for a in self.code_bot_authors.split(",") if a.strip())

    # ---- timeouts, budgets and caches ----
    # Seconds a code-table compaction waits for its OPTIMIZE to finish.
    code_compaction_timeout_s: Annotated[float, LIVE] = Field(default=900.0, gt=0)
    # Seconds one (org, namespace)'s index status serves every caller before it is
    # computed again.
    code_status_ttl_s: Annotated[float, LIVE] = Field(default=30.0, gt=0)
    # Seconds an entry's symbol statistics are cached for its status.
    code_status_symbol_stats_ttl_s: Annotated[float, LIVE] = Field(default=600.0, gt=0)
    # Seconds code.blame_range keeps a blame text, a window graph or a branch point per
    # key.
    code_blame_cache_ttl_s: Annotated[float, LIVE] = Field(default=600.0, gt=0)
    # Seconds a repository's known fork upstreams are cached.
    code_fork_upstream_cache_ttl_s: Annotated[float, LIVE] = Field(default=24 * 3600.0, gt=0)
    # Seconds the ingest keeps a sibling-text answer (a manifest, a listing, a config, a
    # barrel).
    code_ingest_sibling_text_ttl_s: Annotated[float, LIVE] = Field(default=600.0, gt=0)
    # Seconds a PR overlay (the PR's changed files over its base) is cached.
    code_pr_overlay_cache_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds one PR file's parse is kept, so a push that changes one file re-parses
    # that file alone; a PR's next review follows its next push.
    code_pr_overlay_file_ttl_s: Annotated[float, LIVE] = Field(default=3600.0, gt=0)
    # Seconds code.repos keeps the worker-ledger answer every repository's status
    # shares.
    code_status_ledger_passes_ttl_s: Annotated[float, LIVE] = Field(default=15.0, gt=0)
    # Seconds a repository's recent-commits listing for code.repos is cached.
    code_status_recent_commits_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds a repository's error-code table is cached for message search.
    code_message_grep_error_codes_ttl_s: Annotated[float, LIVE] = Field(default=600.0, gt=0)
    # Seconds the fix-blame stage of code.bisect_candidates may run.
    code_bisect_fix_blame_budget_s: Annotated[float, LIVE] = Field(default=8.0, gt=0)
    # Seconds the commit-graph read of code.blame_range may run.
    code_blame_graph_timeout_s: Annotated[float, LIVE] = Field(default=5.0, gt=0)
    # Seconds one `git diff` of the introduced-by test may run.
    code_blame_introduced_diff_timeout_s: Annotated[float, LIVE] = Field(default=5.0, gt=0)
    # Most commits code.blame_range's window graph reads; a longer history is a caller
    # asking for everything, which blame cannot rank.
    code_blame_max_graph_commits: Annotated[int, LIVE] = Field(default=200000, ge=1)
    # Seconds one `git blame` of code.blame_range may run.
    code_blame_timeout_s: Annotated[float, LIVE] = Field(default=5.0, gt=0)
    # Longest wait, in seconds, before a mirror fetches again for a commit no fetch has
    # brought.
    code_bootstrap_backoff_max_s: Annotated[float, LIVE] = Field(default=3600.0, gt=0)
    # Seconds of git time code.verify_citations may spend per request.
    code_citations_git_budget_s: Annotated[float, LIVE] = Field(default=5.0, gt=0)
    # Seconds one git call of code.verify_citations may run, within its request budget.
    code_citations_git_call_timeout_s: Annotated[float, LIVE] = Field(default=3.0, gt=0)
    # Seconds the git diff behind code.conventions may run.
    code_conventions_diff_timeout_s: Annotated[float, LIVE] = Field(default=25.0, gt=0)
    # Seconds a repository job's egress tunnel waits to connect to its upstream host.
    code_egress_connect_timeout_s: Annotated[float, LIVE] = Field(default=15.0, gt=0)
    # Seconds one enrichment of a code result may take before it is left empty.
    code_enrich_timeout_s: Annotated[float, LIVE] = Field(default=0.5, gt=0)
    # A full pass waits this long for a delta on its entry (past any delta's harness
    # timeout).
    code_entry_lock_wait_s: Annotated[float, LIVE] = Field(default=4 * 3600.0, gt=0)
    # Seconds a failed fork-upstream lookup is remembered as "not a known fork".
    code_fork_upstream_miss_ttl_s: Annotated[float, LIVE] = Field(default=600.0, gt=0)
    # Seconds the mirror's git log may run when it searches for merge commits.
    code_merge_shas_log_timeout_s: Annotated[float, LIVE] = Field(default=8.0, gt=0)
    # Most commits the mirror's git log prints when it searches for merge commits.
    code_merge_shas_max_log_commits: Annotated[int, LIVE] = Field(default=5000, ge=1)
    # The longest a history read waits for a clone it started; less when its op's budget
    # is.
    code_mirror_clone_wait_s: Annotated[float, LIVE] = Field(default=10.0, gt=0)
    # Seconds one `git gc` of a mirror may run.
    code_mirror_gc_timeout_s: Annotated[float, LIVE] = Field(default=4 * 3600.0, gt=0)
    # Seconds before a failed compiler-tier full re-index of an entry is tried again.
    code_scip_full_retry_s: Annotated[int, LIVE] = Field(default=6 * 3600, gt=0)
    # Seconds an entry's chunk counts are cached while it is still building: they move
    # then without a watermark moving.
    code_status_building_counts_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds an indexed entry's chunk counts are cached once it is no longer building.
    code_status_steady_counts_ttl_s: Annotated[float, LIVE] = Field(default=600.0, gt=0)
    # Milliseconds the history stage of the backport-gap check may run: one grep per
    # path and a few `-S` reads per undecided branch.
    code_backport_history_budget_ms: Annotated[int, LIVE] = Field(default=8000, gt=0)
    # Milliseconds the hunk-level stage of the backport-gap check may run: one chunk
    # read per branch, all at once.
    code_backport_patch_budget_ms: Annotated[int, LIVE] = Field(default=1000, gt=0)
    # Milliseconds the presence stage of the backport-gap check may run.
    code_backport_presence_budget_ms: Annotated[int, LIVE] = Field(default=1200, gt=0)
    # Most candidates the introduced-by diff test checks per request, blamed ones first,
    # then ledger-only ones by recency.
    code_blame_introduced_max_diff_calls: Annotated[int, LIVE] = Field(default=10, ge=1)
    # After a slice in which the provider answered nothing, the next slices store stubs
    # (the recovery worker fills them) rather than pay a timeout per file; the breaker
    # closes by itself after the hold.
    code_bootstrap_embed_breaker_hold_s: float = Field(default=600.0, gt=0)
    # Seconds after a successful mirror fetch before another read may start one.
    code_bootstrap_mirror_refresh_s: Annotated[float, LIVE] = Field(default=5.0, gt=0)
    # Seconds after a failed mirror clone or fetch before it is tried again; a commit no
    # fetch has brought waits this, doubled per fetch.
    code_bootstrap_mirror_retry_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Most git subprocesses code.verify_citations may start per request.
    code_citations_git_call_cap: Annotated[int, LIVE] = Field(default=20, ge=1)
    # Seconds the coverage control's answer is kept per (org, namespace). The control is
    # one read per namespace, not one per claim. The brief builder makes a
    # claim per touched function, and each would re-run the same `arrayJoin` grep over the
    # tests tree (176 MB a statement on the ClickHouse namespace). Its answer — is the
    # tests corpus measurable from here — holds for a namespace across a brief, so it is
    # kept, in this process only.
    code_coverage_control_ttl_s: Annotated[float, LIVE] = Field(default=300.0, gt=0)
    # Seconds a repository job's egress tunnel may move no byte before it is closed.
    code_egress_idle_timeout_s: Annotated[float, LIVE] = Field(default=300.0, gt=0)
    # Seconds a repository job's connection waits for a free egress tunnel slot.
    code_egress_slot_wait_s: Annotated[float, LIVE] = Field(default=5.0, gt=0)
    # Seconds one embed-recovery pass may spend chaining scans before it stops for the
    # next pass.
    code_embed_recovery_chain_budget_s: Annotated[float, LIVE] = Field(default=20.0, gt=0)
    # Seconds the lookup of a repository's fork upstream may take.
    code_fork_upstream_budget_s: Annotated[float, LIVE] = Field(default=1.5, gt=0)
    # Seconds before a failed warm-up clone of a mirror is tried again.
    code_mirror_warmup_retry_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds before the review brief reads a mirror again for an object it did not have
    # (the mirror may have fetched it); a hit is kept.
    code_review_brief_mirror_miss_ttl_s: Annotated[float, LIVE] = Field(default=600.0, gt=0)
    # Changed paths the `rationale` section reads, source paths first; paths past it make
    # `rationale` say `unknown` (`paths_not_read`).
    code_review_brief_rationale_paths: Annotated[int, LIVE] = Field(default=10, ge=1, le=50)
    # Decision and constraint rows the `rationale` section serves.
    code_review_brief_rationale_rows: Annotated[int, LIVE] = Field(default=8, ge=1, le=50)
    # The `rationale` section's own token budget; its first row is served even past it.
    code_review_brief_rationale_tokens: Annotated[int, LIVE] = Field(default=250, ge=50)


# ===== Context ================================================================


class ContextSettings(BaseModel):
    """context.pack, context offload, session briefs and the mentor's hints."""

    # context.offload: largest single parked tool result, in bytes.
    context_offload_max_bytes: Annotated[int, LIVE] = 8_000_000
    # session.brief_save. `session_brief_llm` off = every brief comes from the
    # deterministic builder (goal, status, files, errors, rules, pointers; no
    # decisions or dead ends), except under an operator's extraction policy,
    # where a save of events alone with no model to call writes nothing. The
    # model is the deployment's LLM when `session_brief_llm_model` has no key.
    # A save condenses its events into windows of `session_brief_window_chars`
    # (≈6k tokens each) and revises the brief once per window, at most
    # `session_brief_max_windows` calls per save (the last window takes the
    # remainder, condensed harder).
    session_brief_llm: Annotated[bool, LIVE] = True
    # Condensed size, characters, of one brief window (floor 2000); a window holds about three
    # times this much raw event text before it is condensed.
    session_brief_window_chars: Annotated[int, LIVE] = Field(default=24_000, ge=2000)
    # Most windows (and model revision calls) per brief save; the last window takes the rest.
    session_brief_max_windows: Annotated[int, LIVE] = 4
    # Seconds a per-process (org, namespace, agent) mentor profile is cached
    # (a failed read ≤ 300 s). 0 disables the mentor everywhere (no pack
    # section, no call hints, empty mentor.brief).
    mentor_ttl_seconds: Annotated[int, LIVE] = 3600
    # At most this many hints per session, each code once.
    mentor_max_hints_per_session: Annotated[int, LIVE] = 6


# ===== Erasure ================================================================


class ErasureSettings(BaseModel):
    """Erasure: saga recovery, reclaim, purge and the external backends it cascades to."""

    # ---- saga recovery and reclaim ----
    # Replay erasure sagas stuck `in_progress` past a heartbeat from
    # `steps_completed`.
    erasure_recovery_enabled: bool = True
    # Seconds between passes of the erasure recovery worker.
    erasure_recovery_interval_seconds: int = 60
    # Physically remove purged rows: a purge of a wide table only masks its
    # rows (lightweight DELETE), and large settled parts rarely merge, so the
    # bytes stay. The erasure reclaim worker gives the purge table with the
    # most dead bytes one `APPLY DELETED MASK` per pass. False = the masked
    # rows wait for merges.
    erasure_reclaim_enabled: bool = True
    # Seconds between passes of the erasure reclaim worker.
    erasure_reclaim_interval_seconds: int = 3600
    # A table is reclaimed once this share of its rows on disk are purged rows.
    erasure_reclaim_min_dead_fraction: float = 0.3
    # Least bytes the deleted rows must hold before the erasure reclaim compacts a
    # table (default 1 GiB).
    erasure_reclaim_min_dead_bytes: int = 1073741824
    # Seconds one erasure step waits for its mutation (submitted with
    # mutations_sync=0, polled in system.mutations); past it the saga fails
    # naming the mutation.
    erasure_mutation_wait_s: int = 3600
    # Seconds erasure waits for this replica's buffered events before deleting them;
    # a timeout leaves the saga retryable. Must be positive and finite.
    erasure_event_drain_timeout_s: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    # Bytes of conversation text after which `erasure.export` stops its `conversations`
    # section, marked `truncated`; a page holds up to this plus the turn that crosses it.
    erasure_export_conversation_max_bytes: Annotated[int, LIVE] = Field(default=67108864, ge=1)

    # ---- external erasure backends ----
    # The database ClickStack's OTel tables (HyperDX's) live in, on the same ClickHouse
    # service: where `otel_exporter = "ch"` writes spans, logs and metrics, and where
    # the erasure cascade deletes a user's rows with ALTER TABLE DELETE. "" = the
    # `default` database for the exporter, and no erasure cascade.
    clickstack_database: Annotated[str, Field(pattern=r"^(?:[A-Za-z_][A-Za-z0-9_-]*)?$")] = ""
    # Comma-separated OTel Map attribute keys holding user_id, ORed in the delete.
    clickstack_user_id_keys: str = "user_id,user.id,enduser.id,loom.user_id,langfuse.user.id"
    # Per-request seconds for external trace/chat backend lookups and deletes
    # during erasure.
    external_erasure_timeout_s: float = 30.0


# ===== Extraction =============================================================


class ExtractionSettings(BaseModel):
    """Fact extraction from messages and observed events."""

    # Value-update supersession on the extraction write path (memory.app.value_updates):
    # a fresh memory restating an existing one with a different value token marks the
    # older one superseded. Costs one ANN lookup per written row; a per-request
    # `detect_updates` overrides it.
    memory_extract_value_updates: Annotated[bool, LIVE] = False
    # Least cosine similarity a neighbour needs to be checked as the older value of a
    # fresh memory; set below `conflict_threshold`.
    memory_extract_value_updates_min_cosine: Annotated[float, LIVE] = Field(
        default=0.75, gt=0.0, le=1.0
    )

    # The observation date a write is anchored to when the caller sends
    # none. "request_time" = today's UTC date, so valid_at is never NULL for a live
    # agent; "none" = undated.
    memory_extract_observation_date_default: Annotated[
        Literal["request_time", "none"], CaseFolded, LIVE
    ] = "request_time"

    # Capture long / structured / enumerated assistant answers whole (not clipped at
    # EXCERPT_LIMIT) so a positional follow-up resolves: at write time the prompt gains
    # a structured-answer block and assistant-answer facts get a higher content_excerpt
    # ceiling. The value column is untouched; takes effect only on (re-)ingest.
    memory_capture_structured_answers: Annotated[bool, LIVE] = True
    # Whether extraction files an assistant's rules about itself as learnings to review; a request
    # can only turn it off. Turn on once every replica's build erases loom.learnings_supporters.
    memory_extract_agent_learnings: Annotated[bool, LIVE] = False
    # Current messages totalling at most this many characters are
    # extracted with the compact system prompt (every rule, the schema anchors a short
    # statement can match, no structured-answer block); longer ones keep the full
    # prompt. 0 turns it off.
    memory_extract_compact_prompt_max_chars: Annotated[int, LIVE] = 1200
    # Existing memories (fact rows only) an extraction in the compact-prompt tier
    # (`memory_extract_compact_prompt_max_chars`) sees for
    # update detection (sized on an extraction eval); 0 lets it see the caller's full
    # limit. A longer transcript sees none: shown the namespace's facts, the extractor
    # links later details to them instead of writing them.
    memory_extract_existing_short_limit: Annotated[int, LIVE] = 8
    # Seconds one durability-check call (`memory_extract_verify_models`) may take; a
    # timeout keeps every fact.
    memory_extract_verify_timeout_s: Annotated[float, LIVE] = 45.0
    # The coverage sweep gives the turns an extraction's first pass cited nowhere a
    # second reading of their own (one more call), when the namespace or the call turns
    # it on or its `memory_extract_coverage_sweep_auto_min_statements` /
    # `memory_extract_coverage_sweep_auto_min_uncovered` gates fire.
    # --- coverage sweep ---
    # Shortest turn, in characters, the coverage sweep reads; shorter "thanks!"-sized
    # turns are skipped.
    memory_extract_coverage_sweep_min_chars: Annotated[int, LIVE] = Field(default=40, ge=1)
    # Most uncovered turns one coverage sweep call reads; later ones are left out.
    # 0 = no cap.
    memory_extract_coverage_sweep_max_turns: Annotated[int, LIVE] = 60
    # Inside a turn the first pass cited, the individual statements no fact covers are
    # swept too (a fact list, a long update), at most this many.
    memory_extract_coverage_sweep_max_sentences: Annotated[int, LIVE] = Field(default=60, ge=1)
    # With the namespace and the call silent, the coverage
    # sweep still runs when the ingest is statement-dense — some turn holds
    # at least this many statements (a fact list, a long update). 0 = never.
    # Chatty conversation (one to four sentences a turn) never reaches it.
    memory_extract_coverage_sweep_auto_min_statements: Annotated[int, LIVE] = 10
    # With the namespace and the call silent and no list-shaped
    # turn, a conversation ingest still gets the coverage sweep when the first pass left at least
    # this many statements without a memory (lexical, LLM-free). 0 = never: on
    # conversation the gate fires on nearly every session, so enabling it is the
    # operator's token trade.
    memory_extract_coverage_sweep_auto_min_uncovered: Annotated[int, LIVE] = 0
    # The coverage sweep reads its listed turns and sentences in groups of this
    # many items, one concurrent call each (more recall for more tokens). 0 = one call.
    memory_extract_coverage_sweep_batch_items: Annotated[int, LIVE] = 0
    # A coding capture keeps the coverage sweep's density / uncovered
    # auto-gates only with at least this many user turns (on short sessions the
    # sweep's output is mostly junk). 0 = always keep. An explicit namespace /
    # per-call switch wins either way.
    memory_extract_coding_sweep_min_user_turns: Annotated[int, LIVE] = 4

    # --- size segments ---
    # Size segments: a session longer than this many messages is extracted in fixed
    # windows of `memory_extract_size_segment_turns` turns (a turn = one user message +
    # the assistant replies that follow). 0 disables. One call per window, concurrently.
    memory_extract_size_segment_messages: Annotated[int, LIVE] = 60
    # Turns per size segment window (a window never splits a turn).
    memory_extract_size_segment_turns: Annotated[int, LIVE] = Field(default=20, ge=1)
    # Most size segment windows per session; past it the last window takes the remainder
    # (windows grow past `memory_extract_size_segment_turns` turns first).
    memory_extract_size_segment_max: Annotated[int, LIVE] = Field(default=16, ge=1)
    # Size segment windows also start past this many characters of message content,
    # whatever the message count; a window closes early once its turns carry this much
    # (an agent transcript with tool results runs ~190K chars a session at the median).
    # 0 disables the character budget (turn budget only).
    memory_extract_size_segment_chars: Annotated[int, LIVE] = 60000
    # --- observed events ---
    # Buffer threshold: a session whose un-distilled
    # observations reach this many characters is distilled even without an
    # explicit observe.crystallize, like a short-term buffer that flushes when
    # full. 0 disables; the READY tag still works either way.
    observe_extract_buffer_chars: Annotated[int, LIVE] = 0
    # Observations a session needs before the buffer threshold can fire (at least 1);
    # a per-namespace value wins.
    observe_extract_buffer_min_observations: Annotated[int, LIVE] = Field(default=8, ge=1)
    # A session's procedure and hint (observe notes) are mined
    # only once the session holds this many non-trivial observations. 0 = any.
    observe_extract_notes_min_observations: Annotated[int, LIVE] = 15
    # Deferred-extraction worker for the observe plane: distills READY rows
    # (observe.record/crystallize) into durable memories through the
    # memory.set_from_messages pipeline. Off by default (makes LLM calls).
    observe_extract_enabled: bool = False
    # Seconds between observe-extraction worker passes.
    observe_extract_interval_seconds: int = 300
    # Seconds an observe-extract pass may run before the supervisor abandons it.
    observe_extract_max_pass_seconds: float = Field(default=900.0, gt=0)
    # Most transition events written per distilled session.
    observe_extract_max_events_per_session: Annotated[int, LIVE] = 12
    # Tool names (case-insensitive globs, comma-separated) whose
    # observations are stored content-less and already distilled: loom's own MCP tools
    # (observing one copies a memory outside the key the user can delete) and the
    # harness's meta tools, which say nothing about the task. "" disables the skip.
    observe_skip_tools: Annotated[str, LIVE] = (
        # The plugin-installed server's tools arrive as
        # `mcp__plugin_loom_loom__*`, hence the `mcp__plugin_loom*` glob.
        "mcp__loom__*,mcp__plugin_loom*,loom_memory_*,loom_knowledge_*,loom_search,loom_observe*,"
        "loom_procedure_*,loom_context_*,loom_ask,"
        "ToolSearch,ListAgents,ListMcpResourcesTool,ReadMcpResourceTool,"
        "ReadMcpResourceDirTool,AskUserQuestion,TodoWrite,Skill,EnterPlanMode,"
        "ExitPlanMode,EnterWorktree,ExitWorktree"
    )
    # --- extraction queue ---
    # Deferred set_from_messages replay. On by default: `defer`
    # is opt-in per call, and an empty queue costs one SELECT per interval.
    extraction_queue_enabled: bool = True
    # Agents whose set_from_messages runs deferred without the flag:
    # comma-separated agent names, "*" = every service-account
    # caller, "" = nobody. A call that sets `record_schema` is never deferred by this
    # list (it reads `written` back), and an explicit defer=false still wins.
    extraction_defer_agents: Annotated[str, LIVE] = ""
    # Seconds between deferred-extraction queue passes.
    extraction_queue_interval_seconds: int = 15
    # Most queued batches one pass claims.
    extraction_queue_max_batches_per_pass: Annotated[int, LIVE] = Field(default=8, ge=0)
    # Seconds a claimed batch stays leased to one worker before another may claim it; at
    # least a minute. Set it above the longest pass
    # (`extraction_queue_max_batches_per_pass` times one replay), or an overlapping
    # worker may claim a batch still waiting in the pass.
    extraction_queue_lease_seconds: Annotated[int, LIVE] = Field(default=600, ge=60)
    # Attempts before a failing batch stops returning to `pending` and is marked failed;
    # each failure waits 15 s, 1, 5, then 30 minutes before the next. A model provider or
    # gateway that cannot be reached, or a held call, spends none.
    extraction_queue_max_attempts: Annotated[int, LIVE] = Field(default=8, ge=0)
    # Namespaces replayed concurrently per pass; batches inside one namespace
    # stay serial in session-date order. At least 1: none would hang the pass.
    extraction_queue_namespace_concurrency: Annotated[int, LIVE] = Field(default=4, ge=1)
    # --- operator extraction policy ---
    # Most namespaces of one org that may hold their own extraction policy;
    # `extraction_policy.set` with scope `namespace` refuses one more.
    extraction_policy_max_namespaces: Annotated[int, LIVE] = Field(default=1000, ge=1)


# ===== Knowledge ==============================================================


class KnowledgeSettings(BaseModel):
    """Knowledge documents and conversations: chunking, search floor, thread titles."""

    # ---- conversations ----
    # conversation.search cosine floor; without it the top_k nearest threads
    # return however far. Stricter than memory recall: chat excerpts are noisy.
    conversation_search_min_similarity: Annotated[float, LIVE] = 0.30
    # Characters of a turn a conversation search hit or a saved-message listing shows;
    # it changes no stored row. At most 2048, the most an embedding row stores
    # (EMBEDDED_EXCERPT_CHARS), so the vector path can always fill it. A saved-message
    # listing reads this many characters and a fixed margin past them, and checks them
    # all for a high shape before it cuts the excerpt.
    conversation_search_excerpt_chars: Annotated[int, LIVE] = Field(default=240, ge=1, le=2048)

    # LLM-title untitled threads in namespaces with conversation_logging_enabled
    # (the Sessions "Recording" toggle is the off-switch). Skipped with the fake
    # LLM.
    conversation_titler_enabled: bool = True
    # Seconds between titler passes.
    conversation_titler_interval_seconds: int = 1800
    # Only threads with a turn in the last this-many hours are titled.
    conversation_titler_window_hours: int = 168
    # Threads one pass titles at most.
    conversation_titler_max_threads_per_pass: int = 10
    # Turns a thread needs before it is titled.
    conversation_titler_min_turns: int = 4

    # ---- search ----
    # Namespaces an all_mine knowledge.search reads besides the addressed one,
    # at most; the rest come back in `namespaces_skipped`. Each search leg is
    # one statement over all of them (measured 110 ms at 10,000 collections).
    knowledge_search_max_namespaces: Annotated[int, LIVE] = Field(default=10_000, ge=1)
    # Namespace kinds an all_mine knowledge.search spans (comma list over
    # loom.namespaces.kind): the memory fan-out's, and the caller's collections.
    knowledge_search_fanout_kinds: Annotated[str, LIVE] = "memory,collection,personal"
    # True: noise sections rank below the knowledge hits close to the query and stay
    # out of context.pack and memory.stack. False: they are served like any section.
    knowledge_noise_filter: Annotated[bool, LIVE] = True
    # Reranker knowledge.search uses when the request names none (same values as the
    # per-request `rerank`: a builtin or a name in LOOM_RERANK_ENDPOINTS_JSON;
    # rerank="none" turns it off for a call). Empty = no default rerank.
    knowledge_search_rerank_default: Annotated[str, LIVE] = ""
    # How many fused hits a rerank reads (the legs fetch at least this many).
    knowledge_search_rerank_top: Annotated[int, LIVE] = Field(default=30, ge=2, le=100)
    # Characters of each hit a reranker reads: its context (title, description,
    # breadcrumb; at most half), then the chunk text. builtin:cohere reads at most 1024.
    knowledge_search_rerank_excerpt_chars: Annotated[int, LIVE] = Field(
        default=800, ge=100, le=4000
    )
    # Seconds a knowledge.search waits on its reranker before it keeps the fused order.
    knowledge_search_rerank_timeout_s: Annotated[float, LIVE] = Field(default=8.0, gt=0)

    # ---- chunking ----
    # Sliding-window chunking on knowledge.add/upsert, in characters.
    # chunk_size 0 = one chunk per doc (hurts long-form retrieval). Smaller
    # chunks mean more embed calls; 1500/1230 stays well under the
    # text-embedding-3 context.
    knowledge_chunk_size: Annotated[int, LIVE] = 1500
    # Characters between chunk starts: below chunk_size the chunks overlap; above it
    # the step is chunk_size, so no text falls between chunks.
    knowledge_chunk_stride: Annotated[int, LIVE] = 1230

    # ---- server-side ingestion (knowledge.upload, knowledge.ingest) ----
    # Largest file POST /v1/knowledge.upload accepts, in bytes; a larger body is
    # refused with 413 before any of it is stored.
    knowledge_upload_max_bytes: Annotated[int, LIVE] = Field(default=64 * 1024 * 1024, ge=1)
    # Seconds an upload stays ingestible; the originals sweep keeps its bytes
    # until then, and knowledge.ingest refuses it after.
    knowledge_upload_ttl_seconds: int = Field(default=86_400, ge=60)
    # The worker that runs ingestion jobs: extract, chunk, embed, publish.
    knowledge_ingest_enabled: bool = True
    # Seconds between ingestion passes while jobs wait; 0 takes the next job as soon
    # as one ends, so the queue drains at the speed of its jobs.
    knowledge_ingest_interval_seconds: int = 0
    # Seconds between passes when the last one found nothing to do.
    knowledge_ingest_idle_interval_seconds: int = 15
    # Seconds one pass, which runs one job, may take before it is cancelled:
    # the extractor's timeout plus embedding the largest source. A job cut off
    # runs again from its start and counts toward its attempts.
    knowledge_ingest_max_pass_seconds: int = Field(default=900, ge=60)
    # Attempts a job gets after a transient failure (a provider or store
    # outage) before it fails for good.
    knowledge_ingest_max_attempts: int = Field(default=5, ge=1)
    # Most chunks one source may publish; a larger extraction fails with
    # `too_large` rather than flood the embedder.
    knowledge_ingest_max_chunks: Annotated[int, LIVE] = Field(default=20_000, ge=1)

    # ---- timeouts, budgets and caches ----
    # Seconds the repository-docs read for a brief may take.
    knowledge_repo_docs_timeout_s: Annotated[float, LIVE] = Field(default=1.5, gt=0)
    # Seconds the BM25 corpus statistics of a knowledge namespace are cached.
    knowledge_bm25_corpus_ttl_s: Annotated[float, LIVE] = Field(default=300.0, gt=0)
    # Seconds a repository's document list is cached for the docs section of a brief.
    knowledge_repo_docs_list_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)

    # ---- document extraction ----
    # The extractor service a format goes to unless LOOM_DOCUMENT_EXTRACTORS_JSON routes
    # it elsewhere, an http(s) base URL. Empty and unrouted: the format is not extracted.
    document_extractor_url: str = ""
    # JSON {format or media type: http(s) base URL}: per-format extractor services, over
    # LOOM_DOCUMENT_EXTRACTOR_URL; docs/reference/extractor-contract.md has the routing.
    document_extractors_json: Annotated[str, JsonObject] = "{}"
    # The bearer token the server sends an extractor service, which checks it, unless
    # LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON holds one for that service's URL.
    document_extractor_token: str = ""
    # JSON {base URL: bearer token}: each extractor service's own token, so none holds
    # another's. Each URL must be one an extractor URL setting names.
    document_extractor_tokens_json: Annotated[str, JsonObject] = "{}"
    # Seconds one call to an extractor may take as a whole: reading its capabilities, or
    # sending the original and reading its artifact.
    document_extractor_timeout_s: float = Field(default=180.0, gt=0)
    # Bytes of an extractor's response the server reads; a larger artifact is refused.
    document_extractor_max_artifact_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)

    # ---- document sources ----
    # Active document sources (knowledge.source.add) one repository namespace may
    # register; an add past it is refused with SOURCE_QUOTA.
    knowledge_sources_max_per_namespace: Annotated[int, LIVE] = Field(default=8, ge=1)
    # Seconds a document source's probe (add, check, rotate) may take in all; past it
    # add and rotate answer SOURCE_UNREACHABLE and check answers reachable: false.
    knowledge_source_probe_timeout_s: Annotated[float, LIVE] = Field(default=20.0, gt=0, le=25.0)
    # The knowledge_source_sync worker: pulls each registered document source's changed
    # files into its repository's documents. Off by default: it needs each source's
    # credential, spends embedding calls on every changed document and model calls
    # mining it for decisions.
    knowledge_source_sync_enabled: bool = False
    # Seconds between sync passes while there are sources.
    knowledge_source_sync_interval_seconds: int = Field(default=900, ge=60)
    # Seconds between passes that found no source to sync; one idle sleep plus one
    # pass stays under three intervals, so the worker never reads as stalled.
    knowledge_source_sync_idle_interval_seconds: int = Field(default=1800, ge=60)
    # Seconds one sync pass may run before it is cancelled; it stops starting sources
    # at three quarters of it. At least 180: 90 for the sync, then 60 of read time for
    # one document's extraction and 30 for its writes before the deadline.
    knowledge_source_sync_max_pass_seconds: int = Field(default=600, ge=180)
    # Documents one pass fetches, writes, retires or mines for decisions across all
    # sources; the rest wait for the next pass.
    knowledge_source_sync_documents_per_pass: Annotated[int, LIVE] = Field(default=40, ge=1)
    # Seconds after which a source's next pass lists every file, not only those
    # changed since the last one: what finds a file trashed or moved out of a folder.
    knowledge_source_sync_full_interval_seconds: Annotated[int, LIVE] = Field(default=86400, ge=900)
    # Bytes of one file a sync downloads or exports; a larger file is not ingested.
    knowledge_source_sync_max_file_bytes: Annotated[int, LIVE] = Field(
        default=64 * 1024 * 1024, ge=1024 * 1024
    )
    # The share of a namespace's daily LLM cost cap the sync may spend mining changed
    # documents for decisions, counted over its own calls; the rest stays for requests.
    knowledge_source_sync_cap_share: Annotated[float, LIVE] = Field(default=0.25, gt=0.0, le=1.0)

    def document_extractor_routes(self) -> dict[str, str]:
        """The operator's per-format extractor URLs, format or media type → URL;
        a media type without its parameters and lowercased, as routing reads it."""
        raw = self.document_extractors_json.strip()
        if not raw:
            return {}
        items = json_map_items(raw, "document_extractors_json", strings_only=True)
        return {route_key(key): url for key, url in items}

    def document_extractor_token_map(self) -> dict[str, str]:
        """Each extractor service's own token, base URL (no trailing `/`) → token."""
        raw = self.document_extractor_tokens_json.strip()
        if not raw:
            return {}
        items = json_map_items(raw, "document_extractor_tokens_json", strings_only=True)
        return {url.rstrip("/"): token for url, token in items}

    @model_validator(mode="after")
    def _check_document_extractors(self) -> KnowledgeSettings:
        """Every extractor URL is http(s) with a host. The error names the entry,
        never the URL (it can carry a credential)."""
        raw = self.document_extractors_json.strip()
        seen: set[str] = set()
        for key, _ in json_map_items(raw, "document_extractors_json") if raw else ():
            routed = route_key(key)
            if not routed:  # a typo would leave the format on the default URL
                raise ValueError(
                    f"LOOM_DOCUMENT_EXTRACTORS_JSON {key[:64]!r} is neither a document format "
                    "nor a media type that names one"
                )
            if routed in seen:  # two spellings of one media type: one would be dropped
                raise ValueError(
                    f"LOOM_DOCUMENT_EXTRACTORS_JSON {key[:64]!r} routes {routed!r} again"
                )
            seen.add(routed)
        routes = self.document_extractor_routes()
        urls = {"LOOM_DOCUMENT_EXTRACTOR_URL": self.document_extractor_url} | {
            f"LOOM_DOCUMENT_EXTRACTORS_JSON {key[:64]!r}": url for key, url in routes.items()
        }
        for name, url in urls.items():
            if url and not _is_http_url(url):
                raise ValueError(f"{name} is not an http(s) URL")
        # A token for a URL no route names would leave that service on the shared
        # token without a word: refused, as is a token that is not a string.
        configured = {url.rstrip("/") for url in urls.values() if url}
        raw = self.document_extractor_tokens_json.strip()
        items = (
            json_map_items(raw, "document_extractor_tokens_json", strings_only=True) if raw else ()
        )
        token_urls = [url.rstrip("/") for url, _ in items]
        for i, url in enumerate(token_urls):
            if url in token_urls[:i]:  # `http://x` and `http://x/`: one token would be dropped
                raise ValueError(
                    f"LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON entry {i + 1} names the URL of an "
                    "earlier entry again"
                )
            if url not in configured:
                raise ValueError(
                    f"LOOM_DOCUMENT_EXTRACTOR_TOKENS_JSON entry {i + 1} names no configured "
                    "extractor URL (LOOM_DOCUMENT_EXTRACTOR_URL or LOOM_DOCUMENT_EXTRACTORS_JSON)"
                )
        return self


# ===== Mcp ====================================================================


class McpSettings(BaseModel):
    """The MCP transport: tool listing, lean results, session runs."""

    # loom_search drops knowledge hits below this cosine similarity (the
    # conversation leg's floor, applied to the knowledge leg).
    mcp_loom_search_knowledge_min_similarity: float = 0.30
    # loom_search's profile leg: sections the words missed pass by cosine
    # against `label\nvalue` at this hard floor. 0 = lexical only.
    mcp_loom_search_profile_min_similarity: float = 0.38
    # Cosine floor of loom_search's profile leg for a question about the user only (the
    # soft tier).
    mcp_loom_search_profile_soft_similarity: float = 0.30
    # Lead a profile section must have over the namespace's runner-up to pass loom_search's
    # soft tier.
    mcp_loom_search_profile_margin: float = 0.08
    # Seconds loom_search's tracker leg (similar issues and PRs) may take before
    # the answer goes without it.
    mcp_loom_search_tracker_timeout_s: Annotated[float, LIVE] = Field(default=3.0, gt=0)

    # Sub-floor rows in loom_search: "drop" serves only what clears the floor;
    # "readmit" (the memory.search default) pads top_k with below-threshold
    # rows the reader is told to ignore; "ids_only" pads with empty bodies.
    mcp_loom_search_floor_mode: Literal["drop", "readmit", "ids_only"] = "drop"
    # Precision floor for loom_search memory hits. Negative = auto (midpoint
    # of the embedder's calibrated noise floor and question→fact p25);
    # 0 disables.
    mcp_loom_search_precision_floor: float = -1.0
    # Extra margin over the precision floor for hits from another namespace.
    mcp_loom_search_foreign_margin: float = 0.08
    # MCP transport auth mode (the endpoint exposes one tool per wire op, so an
    # anonymous prober could otherwise enumerate every tool):
    #   "required"        — every JSON-RPC method requires a bearer.
    #   "discovery_only"  — initialize/tools/list/ping open; tools/call needs auth.
    #   "open"            — no auth on any method; a non-loopback bind refuses it.
    # Any other value fails at load: a mistyped mode must not boot as some mode.
    mcp_auth_mode: Literal["required", "discovery_only", "open"] = "required"

    # Which tools `tools/list` shows when a connection names no `?toolset=` (a request that
    # names one gets that one instead). Rosters are the frozensets in
    # server/operations/toolsets.py plus loom_search and loom_activate; on every toolset
    # but "assistant" this is visibility only and every op stays callable by name over MCP
    # and /v1/*.
    #   "core"      — (default) _MCP_CORE_OPS (+ _MCP_CODE_CORE_OPS when
    #                 mcp_code_tools is on).
    #   "coding"    — _MCP_CODING_OPS + _MCP_CODE_CORE_OPS; the plugin selects it
    #                 per request with `?toolset=coding`.
    #   "tracker", "tasks" — the tracker plane and the task inbox rosters.
    #   "atomic"    — CRUD + scratchpad + run ledger (_MCP_ATOMIC_OPS),
    #                 policy-shaped descriptions.
    #   "readonly"  — the "all" listing without the tools whose readOnlyHint is false.
    #   "assistant" — MCP_ASSISTANT_OPS only, and only those are callable on a connection
    #                 that resolves to it. Not a server-wide read-only switch: a request
    #                 naming another toolset gets it, and the token is the boundary.
    #   "all"       — every op except _MCP_HIDDEN_OPS and the experimental ones
    #                 no curated tier lists (they stay callable by name).
    # An unknown value is "core".
    mcp_toolset: str = "core"
    # tools/list schema profile: "standard" (curated prose + inputSchema),
    # "full" (+ outputSchema) or "compact" (first paragraph, argument tree).
    # Per request: `?schema_profile=`.
    mcp_schema_profile: Annotated[Literal["standard", "full", "compact"], CaseFolded] = "standard"
    # Ops whose tools/list entry carries `_meta: {"anthropic/alwaysLoad": true}`, so a host
    # that defers MCP tools behind tool search lists these up front and an agent calls them
    # on its first turn instead of spending a turn loading them. Comma-separated op names;
    # empty = none.
    mcp_always_load_tools: str = "code.search,code.symbol"
    # Seconds without a tool call after which the mcp_session_run_sweep worker
    # closes an MCP session's implicit run (one per token and `Mcp-Session-Id`,
    # begun on the first run-scoped tool call); with no background workers
    # anywhere, idle runs stay open. At least 60: each web process re-checks its
    # cached run once a minute (mcp_runs.CHECK_INTERVAL_S), so a shorter window
    # would close runs a process still treats as open.
    mcp_session_run_idle_seconds: int = Field(default=1800, ge=60)
    # A rate-limited tool call waits out its Retry-After once and re-checks
    # before returning RATE_LIMITED (MCP clients don't retry). This is the
    # longest Retry-After, seconds, that one retry waits out; a longer hint is
    # refused at once, and 0 refuses every limited call at once.
    mcp_ratelimit_retry_max_wait_s: float = 2.0

    # List code.* tools on the MCP surface (core toolset union). Off = code
    # ops are HTTP/SDK-only.
    mcp_code_tools: bool = False


# ===== Memory =================================================================


class MemorySettings(BaseModel):
    """The memory plane outside search/ask/extraction: writes, conflicts, lifecycle, workers."""

    # ---- prompt safety and search priors ----
    # Cap on memory.search's metadata priors (confidence, soft tag/category) so
    # they break ties instead of re-ranking. 0.0 = uncapped; a request's
    # `prior_cap` overrides it.
    memory_prior_cap: float = 0.08
    # Memory bodies in every synthesis and extraction prompt sit between
    # nonce-carrying data markers (server/memory/domain/prompt_fence.py).
    # Key for the fence nonce, HMAC(secret, prompt): an identical prompt renders
    # identically (the response cache keys on it) while a memory author cannot
    # predict the closing marker. "" = derive from jwt_secret.
    prompt_fence_secret: str = ""

    # ---- scratchpad ----
    # Characters each injected working-memory block renders before it is cut with "(truncated)".
    memory_scratchpad_max_chars: int = Field(default=2000, ge=1)

    # ---- pre-edit block ----
    # Tokens (4 characters each) memory.for_file gives its decision and constraint rows, which
    # print whole outside token_budget; a row past it is left out and named.
    memory_for_file_whole_tokens: Annotated[int, LIVE] = Field(default=1500, ge=50, le=4000)

    # ---- deployment-specific names the fact filters recognise ----
    # Comma-separated names of the people whose sessions this deployment records
    # ("jane,jane doe"). The junk-fact filters read "Jane ran a command …" like
    # "The user ran a command …". Empty = only the generic actors (user, assistant,
    # Claude, the agent).
    memory_quality_actor_names: str = ""
    # Regex (case-insensitive) matching the host name of another agent session on
    # this deployment ("build-[0-9]+"); a fact whose subject is such a peer is
    # coordination chatter, not a durable fact. Empty = no host pattern.
    memory_quality_peer_host_pattern: str = ""

    # ---- write-path contracts and reinforcement ----
    # Negative memory.outcome signals after which a memory is quarantined
    # (status 'rejected', excluded from search). 0 = off: negatives stay
    # audit-only.
    memory_negative_demote_threshold: int = 0
    # Recall-equivalents one `used` event (run.complete memories_used,
    # memory.outcome "used") adds to search-side reinforcement; the boost
    # follows use, not exposure. 0 = recorded, not reinforcing.
    memory_used_reinforcement_weight: int = 2
    # Recall-equivalents one `harm` event (a relied-on row in a run that ended
    # badly) subtracts. Floored at the write-time base: harm cancels earned
    # reinforcement, never ranks below cosine and never deletes. None = the
    # `used` weight; 0 = counted in memory.health, not charged.
    memory_harm_reinforcement_weight: int | None = None
    # Recall-equivalents one `used_cited` event (memory.ask crediting the keys
    # its answer cited) adds; `used` stays for labels an agent wrote. None =
    # the `used` weight; 0 = recorded, not ranking.
    memory_used_cited_reinforcement_weight: int | None = None
    # Days a consumer's closer keeps its run-less reads credited.
    memory_reinforcement_closer_window_days: int = 14
    # Seconds one (org, consumer) closer answer is cached per worker.
    memory_reinforcement_closer_cache_seconds: int = 300
    # memory.list rows per call for a token below writer on the namespace; it
    # pages with `offset` (wire cap stays 1000). 0 = no viewer cap.
    memory_list_viewer_limit: int = 100
    # Transcript characters the session-digest call reads (~4 chars a token);
    # 0 = the built-in 3000.
    memory_session_digest_max_input_chars: int = 3000
    # content_excerpt ceiling for answer-bearing facts (question_topic set)
    # when memory_capture_structured_answers is on, so an enumerated answer
    # is not clipped mid-list; every other fact keeps EXCERPT_LIMIT (1,200 chars).
    memory_structured_answer_excerpt_limit: int = 4096
    # A searchable memory.set value serializing longer than this many chars
    # gets one advisory line (the write still lands): only the first
    # EXCERPT_LIMIT chars are embedded, and past the embedder's 8,191-token cap
    # nothing is. 30,000 chars ≈ 8k tokens. 0 disables.
    memory_value_advisory_chars: int = 30_000

    # ---- conflict detection ----
    # memory.set flags a write `conflicted` when an active peer in the same
    # (org, namespace, memory_type) sits at cosine ≥ this. Tuned
    # for text-embedding-3-*; 1.0 disables. Per-namespace override:
    # loom.namespaces.conflict_threshold.
    conflict_threshold: float = 0.85
    # The conflict resolver: a leader-only worker that judges each write marked for it
    # (a live peer of its type at cosine >= memory_conflict_resolver_min_similarity, or
    # flagged `conflicted`) against its nearest peers in one LLM call, then retires,
    # flags or clears. Unset, it is resolved at load: off when the LLM provider or
    # conflict_resolver_model's provider is `fake`, else on (without the key, writes are
    # marked and judged once it is set); true / false force it. ~$0.005 per judged write
    # on gpt-6.1-sol.
    memory_conflict_resolver_enabled: bool = False
    # Peers down to this cosine (same memory_type) mark a write for the resolver and
    # join the cluster it is judged in. The cosine `conflicted` flag stays at the
    # conflict threshold. A peer at this cosine or above can be an update of the row;
    # below it, almost never.
    memory_conflict_resolver_min_similarity: float = Field(default=0.60, gt=0.0, le=1.0)
    # Most peers judged with a row in one call, nearest first.
    memory_conflict_resolver_max_peers: Annotated[int, LIVE] = Field(default=8, ge=1, le=8)
    # An update at or above this confidence retires the older row (auto_reinforce, newest_wins);
    # one between the flag confidence and this is flagged for review.
    memory_conflict_resolver_retire_confidence: float = Field(default=0.98, ge=0.0, le=1.0)
    # An update or a contradiction at or above this confidence acts: it flags the pair for
    # review, or retires where the retire confidence and the policy allow; below it, it
    # changes nothing. Between the clear confidence and the retire confidence.
    memory_conflict_resolver_flag_confidence: float = Field(default=0.95, ge=0.0, le=1.0)
    # Below this confidence a verdict changes nothing. At or above it a compatible
    # verdict clears the pair's flag.
    memory_conflict_resolver_clear_confidence: float = Field(default=0.90, ge=0.0, le=1.0)
    # Marked rows read per batch; a full batch with no errors is followed at once by the
    # next, for up to memory_conflict_resolver_burst_seconds of a pass.
    memory_conflict_resolver_batch: Annotated[int, LIVE] = Field(default=40, ge=1)
    # Rows already `conflicted` that no resolver judged (a cosine flag, or an older build's
    # judge) re-checked per pass, after the marked rows. 0 = no re-check.
    memory_conflict_resolver_recheck_batch: Annotated[int, LIVE] = Field(default=8, ge=0)
    # The share of a namespace's daily LLM cost cap the conflict resolver may spend,
    # counted over its own calls; the rest stays for the requests.
    memory_conflict_resolver_cap_share: Annotated[float, LIVE] = Field(default=0.25, gt=0.0, le=1.0)
    # Days a write stays owed a judgement: an owed row older than this is no longer judged,
    # and stays as it was written.
    memory_conflict_resolver_max_owed_age_days: Annotated[int, LIVE] = Field(default=30, ge=1)
    # Rows of a pass judged at once (LLM calls in flight). 1 judges them one at a time;
    # doubling it roughly doubles the rows judged a minute, up to the provider's rate limit.
    memory_conflict_resolver_concurrency: int = Field(default=8, ge=1)
    # Seconds the resolver waits after a pass before the next one; at least 1.
    memory_conflict_resolver_interval_seconds: float = Field(default=10.0, ge=1.0)
    # Seconds a conflict-resolver pass may run before the supervisor abandons it.
    memory_conflict_resolver_max_pass_seconds: float = Field(default=600.0, gt=0)
    # Seconds a pass whose owed read came back full keeps reading the next batch at once; a
    # short read ends it. 0 = one batch a pass. At most half of
    # memory_conflict_resolver_max_pass_seconds, so a pass ends before its deadline.
    memory_conflict_resolver_burst_seconds: float = Field(default=300.0, ge=0)
    # Times a row is judged and left owed (a flagged peer the answer leaves out, an error
    # after the call) before it is stamped, errored: each is a paid call.
    memory_conflict_resolver_max_attempts: Annotated[int, LIVE] = Field(default=3, ge=1)
    # Seconds the resolver waits after a pass the model, the locks or a provider outage
    # stopped, and a namespace whose rows got the same refusal row after row waits alone.
    memory_conflict_resolver_pause_after_stop_seconds: Annotated[float, LIVE] = Field(
        default=300.0, ge=0
    )
    # Seconds before the re-check of unjudged `conflicted` rows runs again once its read
    # came back short; the read scans every `conflicted` row.
    memory_conflict_resolver_recheck_idle_seconds: Annotated[float, LIVE] = Field(
        default=300.0, ge=0
    )
    # Entries per bulk call that get the conflict check; later entries are written unchecked
    # and counted. 0 = none are checked. memory.import checks every entry whatever this says.
    bulk_conflict_check_max_entries: int = 200
    # Supersession pairs one bulk write follows on the triple plane in the request; the
    # rest are recorded for the projection repair to follow. 0 = the repair follows all.
    memory_bulk_follow_max_pairs: Annotated[int, LIVE] = Field(default=1024, ge=0)
    # Bulk-path conflict checks run at once (at least 1).
    bulk_conflict_check_concurrency: int = Field(default=8, ge=1)
    # Facts per batched candidate lookup (conflict and value-update peers) on
    # the write path; matches the per-fact concurrency so wall time stays flat.
    # 0 = one statement per fact.
    memory_write_candidate_batch: int = 6

    # Cosine ≥ this with a canonical-form mismatch marks a write 'duplicate'
    # with a duplicate_of pointer instead of 'conflicted'. Per-namespace
    # override: loom.namespaces.duplicate_threshold.
    duplicate_threshold: float = 0.97

    # ---- blacklist cache ----
    # Blacklist cache invalidation: in-process invalidate_cache(), a one-row
    # CH version watermark (loom.blacklist_version) polled at most every check
    # interval (bounds cross-replica lag), and a hard TTL backstop. No Redis.
    blacklist_version_check_interval_seconds: float = 2.0
    # Seconds after which the blacklist cache reloads regardless of the version watermark.
    blacklist_cache_ttl_seconds: int = 300

    # ---- workers ----
    # Periodically re-run the embedder calibration probe so thresholds follow a
    # silently revised embedder. Fake providers skip.
    recalibration_enabled: bool = True
    # Seconds between embedder calibration probes.
    recalibration_interval_seconds: int = 7 * 24 * 3600

    # Seconds between the entity extractor's mirror divergence heal, a
    # FINAL-to-FINAL join of loom.memory and loom.memory_embeddings (343 GB/day
    # if run every pass). Divergence arrives a few rows a day.
    entity_heal_interval_seconds: int = 900

    # Retry best-effort projections (chunks, evidence links, graph side tables)
    # that failed after the primary write. Bounded and idempotent; unsupported
    # ones stay in ops.projection_failures.list.
    projection_repair_enabled: bool = True
    # Seconds between projection repair passes.
    projection_repair_interval_seconds: int = 300
    # Recorded projection failures one pass retries (at least 1).
    projection_repair_max_failures_per_pass: int = Field(default=25, ge=1)

    # Fold loom.memory_usage_totals onto the ranking columns (reinforcement
    # boost, memory.list sort_by=score, auto-promote gate); off, they freeze at
    # write-time values.
    usage_fold_enabled: bool = True
    # Seconds between usage-fold passes.
    usage_fold_interval_seconds: int = Field(default=300, ge=1)

    # Re-emit, with a tombstone, vector rows still live after their loom.memory
    # row was tombstoned or erased, so search stops offering them. Bounded per
    # pass; loom.integrity.orphan_vectors should read 0 between passes.
    orphan_vector_reconcile_enabled: bool = True
    # Seconds between orphan vector reconcile passes.
    orphan_vector_reconcile_interval_seconds: int = 600
    # Orphaned keys one pass re-emits tombstones for (at least 1).
    orphan_vector_reconcile_max_keys_per_pass: int = Field(default=500, ge=1)
    # Seconds before an unmatched embedding counts as orphaned, so a write whose
    # two rows land apart is not taken for a deletion.
    orphan_vector_reconcile_grace_seconds: int = 600

    # ---- procedures ----
    # `procedure.evolve` quarantines a procedure whose trace log ends in this
    # many consecutive failed runs (out of procedure.match and the pack until a
    # run succeeds). 0 = off.
    procedure_strike_limit: int = 3

    # ---- consolidation ----
    # Clusters merged concurrently by memory.consolidate; 1 = sequential.
    consolidation_merge_concurrency: int = 4

    # Fold a user's durable preference/decision memories into a versioned
    # profile section (memory_type='profile') for profile.render /
    # context.pack; skipped when refreshed within the cooldown. LLM calls;
    # namespace opt-in via consolidation_enabled.
    profile_consolidation_enabled: bool = False
    # Seconds between profile fold passes.
    profile_consolidation_interval_seconds: int = 6 * 3600
    # Users (org, namespace, user_id) one pass folds, most preference/decision rows first.
    profile_consolidation_max_identities_per_pass: int = 10
    # Live preference/decision memories a user needs before a profile is folded.
    profile_consolidation_min_memories: int = 5
    # Memories read into one fold, highest confidence then newest first.
    profile_consolidation_memory_limit: int = 100
    # Seconds after a profile section is written before it is folded again.
    profile_consolidation_cooldown_seconds: int = 24 * 3600
    # `memory_key` of the folded profile row.
    profile_consolidation_label: str = "consolidated_profile"

    # ---- structured profiles ----
    # Run the worker that builds each user's structured profile in the namespace's
    # schema (LLM calls, only where `profile_schema.set` set one).
    structured_profile_enabled: bool = True
    # Seconds between structured profile passes. Above 0.
    structured_profile_interval_seconds: int = Field(default=120, gt=0)
    # Seconds the structured profile worker waits after a pass that reached every
    # namespace with a schema and took no user. Above 0.
    structured_profile_idle_interval_seconds: int = Field(default=600, gt=0)
    # Seconds one namespace's scan for users behind their structured profile may run;
    # past it the scan fails and the namespace backs off. Above 0.
    structured_profile_scan_timeout_seconds: Annotated[int, LIVE] = Field(default=60, gt=0)
    # Bytes of memory one namespace's structured profile scan may use (ClickHouse
    # max_memory_usage). At least 1 MiB.
    structured_profile_scan_max_memory_bytes: Annotated[int, LIVE] = Field(
        default=4 * 1024**3, ge=1024**2
    )
    # Structured profiles one pass builds; the namespaces in each page the pass reads
    # share it equally, and the pass reads on while budget is left. At least 1.
    structured_profile_max_users_per_pass: Annotated[int, LIVE] = Field(default=20, ge=1)
    # Seconds a structured profile pass may run before the supervisor abandons it;
    # it takes no user in the second half. Above 0.
    structured_profile_max_pass_seconds: float = Field(default=600.0, gt=0)
    # Memories read into one structured profile build, highest confidence then newest
    # first. At least 1.
    structured_profile_memory_limit: Annotated[int, LIVE] = Field(default=100, ge=1)
    # Seconds after a structured profile is built before new memories rebuild it; a
    # schema change or a deleted source skips this wait. 0 rebuilds on the next pass.
    structured_profile_cooldown_seconds: Annotated[int, LIVE] = Field(default=600, ge=0)
    # Seconds a user or namespace whose structured profile build failed waits before
    # the first retry; each further failure doubles it. Above 0.
    structured_profile_backoff_base_seconds: Annotated[int, LIVE] = Field(default=300, gt=0)
    # Longest wait, in seconds, of a user or namespace whose structured profile build
    # keeps failing. Above 0.
    structured_profile_backoff_cap_seconds: Annotated[int, LIVE] = Field(default=6 * 3600, gt=0)
    # Seconds a refused or cut-off structured profile build waits before it is tried
    # again with nothing changed. Above 0.
    structured_profile_refusal_retry_seconds: Annotated[int, LIVE] = Field(default=6 * 3600, gt=0)
    # Seconds a namespace's scan for users behind their structured profile serves
    # later passes before it is run again. Above 0.
    structured_profile_scan_max_age_seconds: Annotated[int, LIVE] = Field(default=600, gt=0)
    # `memory_key` of the structured profile row.
    structured_profile_label: str = "structured_profile"

    # ---- reflection distillation ----
    # Reflection distillation: label recent sessions that carry no outcome
    # (positive / negative / unclear into loom.session_outcomes_by_user), pair each
    # confidently failed session with its nearest successful one and distil
    # the difference into one hidden `reflection` memory. LLM calls; sessions
    # labelled through run.complete are not re-judged.
    reflection_distill_enabled: bool = False
    # Seconds between reflection distillation passes.
    reflection_distill_interval_seconds: int = 3600
    # Days back a pass looks for unlabelled sessions and for failed sessions to contrast.
    reflection_distill_lookback_days: int = 14
    # Sessions one pass sends to the outcome labeller.
    reflection_distill_max_judged_per_pass: int = 40
    # Failed sessions one pass tries to pair and distil.
    reflection_distill_max_contrasts_per_pass: int = 10
    # A judged label below this confidence neither pairs nor is paired with.
    reflection_distill_min_confidence: float = 0.7
    # Cosine floor between the failed and the successful summary; below it the
    # sessions were not about the same thing.
    reflection_distill_min_pair_similarity: float = 0.55

    # ---- entity extraction ----
    # Async LLM entity tagging of memory rows (fills `entities` for search's
    # entity match); off the memory.set hot path.
    entity_extract_enabled: bool = True
    # Seconds between entity extraction passes that found work.
    entity_extract_interval_seconds: int = 60
    # Sleep after an entity extraction pass that found no unentitied row.
    entity_extract_idle_interval_seconds: int = 300
    # Seconds between entity extraction's full candidate scans (without the value column);
    # other passes scan only `entity_extract_recent_seconds`. 0 = full scan every pass.
    entity_extract_full_scan_seconds: int = 1800
    # Window, seconds, of recent memories each pass scans for untagged rows.
    entity_extract_recent_seconds: int = 7200
    # Keys the full scan queues for the passes in between (refilled early when
    # the queue runs dry).
    entity_extract_full_scan_keys: int = 5000
    # Backlog keys per namespace in one refill, oldest first, round-robin
    # across namespaces so one cannot starve the rest. 0 = org-wide newest
    # first.
    entity_extract_backlog_per_namespace: int = 500

    # ---- dedup and expiry ----
    # Bulk-path exact dedup. "session" = a byte-identical value is skipped only
    # under the same key in the same session. "identity" = a live row anywhere
    # in (org, namespace, agent, user_id) with the same content_sha256 is the
    # fact; the entry becomes an `evidence` reinforcement of it, named in
    # `identity_duplicates`.
    memory_bulk_dedup_scope: Literal["session", "identity"] = "identity"

    # Apply each expired row's `on_expire` (tombstone | demote | anonymize) as a
    # version row. Reads already hide expired rows; the sweep records it.
    memory_expiry_sweep_enabled: bool = True
    # Seconds between expiry sweep passes.
    memory_expiry_sweep_interval_seconds: int = 300
    # Expired rows one sweep pass applies.
    memory_expiry_sweep_batch_size: int = 500
    # `demote` multiplies confidence by this factor, never below
    # `memory_expiry_demote_floor`.
    memory_expiry_demote_factor: float = 0.5
    # Lowest confidence a `demote` (expiry or run.complete) leaves on a row.
    memory_expiry_demote_floor: float = 0.05

    # ---- classify endpoint ----
    # Seconds a namespace's classify_endpoint may take to answer a write's
    # classification; past it the write keeps the caller's memory_type.
    memory_classify_timeout_s: Annotated[float, LIVE] = Field(default=8.0, gt=0)
    # Comma-separated hostnames or IP literals, no port, a classify_endpoint may reach on a
    # private address and over http, on any port: list only a host that serves nothing else.
    memory_classify_private_hosts: str = ""

    def memory_classify_private_host_set(self) -> frozenset[str]:
        return ascii_host_set(self.memory_classify_private_hosts)

    @model_validator(mode="after")
    def _check_classify_private_hosts(self) -> MemorySettings:
        refusal = host_list_refusal(
            "LOOM_MEMORY_CLASSIFY_PRIVATE_HOSTS", self.memory_classify_private_hosts
        )
        if refusal:
            raise ValueError(refusal)
        return self

    # ---- delete-version checks ----
    # Run the delete_version_check worker, which finishes the memory.delete_version checks a
    # 503 left owed (False: only a retry of the delete finishes them).
    delete_version_check_enabled: bool = True
    # Seconds between delete_version_check passes while checks are owed.
    delete_version_check_interval_seconds: int = Field(default=60, gt=0)
    # Seconds between delete_version_check passes that found none.
    delete_version_check_idle_interval_seconds: int = Field(default=300, gt=0)
    # Seconds a delete-version check is left to the delete that owes it before a pass runs it.
    delete_version_check_grace_seconds: int = Field(default=60, ge=0)
    # Delete-version checks one delete_version_check pass runs.
    delete_version_check_batch_size: int = Field(default=200, ge=1)

    # ---- namespace health, knowledge graph, communities ----
    # Seconds a namespace's health summary is cached per process (a failed lookup at
    # most `memory_namespace_health_failure_ttl_s`). 0 disables the summary and its pack
    # section.
    namespace_health_ttl_seconds: int = 3600
    # Label-propagation communities over the entity co-occurrence graph, per
    # namespace, into loom.entity_communities (read by namespace.taxonomy and
    # graph-mode expansion). A CPU pass over every namespace's edges.
    entity_communities_enabled: bool = False
    # Seconds between community partition passes.
    entity_communities_interval_seconds: int = 6 * 3600
    # Entity pairs read per namespace, strongest first; past it the rest are dropped and
    # the namespace is reported truncated.
    entity_communities_max_edges: int = 200_000
    # A pass covers the namespaces with consolidation_enabled plus those whose
    # graph was walked (facts.traverse / related / path, namespace.taxonomy)
    # within this many days; 0 = the opted-in set only.
    entity_communities_walked_days: int = 14
    # Leave the top fraction of entities by degree out of the partition, so
    # generic tokens cannot fuse the graph into one community. A graph under 500
    # entities drops none.
    # 0 = keep every entity.
    entity_communities_hub_drop_fraction: float = 0.002
    # When one community holds more than this share of a namespace's entities,
    # double `entity_communities_hub_drop_fraction` and re-partition, up to
    # `entity_communities_hub_drop_max_fraction`
    # (a code corpus parted at 0.016, 2.9%). 0 = fixed fraction.
    entity_communities_giant_share: float = 0.5
    # Largest `entity_communities_hub_drop_fraction` the re-partition doubles up to (four
    # doublings from its default); a graph still fused here is written as is and counted
    # in the pass outcome.
    entity_communities_hub_drop_max_fraction: float = 0.032
    # Co-occurrence count an entity pair needs to count as an edge.
    entity_communities_min_co_occur: int = 1
    # ---- bulk contextualization ----
    # memory.bulk_set honours contextualize=true (embed a chunk with its
    # context) only when allowed; `memory_bulk_contextualize_concurrency` bounds the
    # per-entry model calls, `memory_bulk_contextualize_max_entries` caps one call's spend.
    memory_bulk_contextualize_allowed: bool = True
    # Context model calls one bulk_set runs at once.
    memory_bulk_contextualize_concurrency: int = Field(default=8, ge=1)
    # A bulk_set with more pending entries than this is not contextualized at all.
    memory_bulk_contextualize_max_entries: int = Field(default=200, ge=1)
    # ---- graph walks (facts.traverse) ----
    # Relative hub rule: hold a reached node when the edge that reached it is at
    # most this share of the node's degree (via_co_occur / degree). Scale-free.
    # 0 = off; a request's `hub_share` overrides it.
    facts_traverse_hub_share: float = 0.01
    # What facts.traverse's per-hop trim ranks by: `co_occur` (most
    # co-mentioned pairs), `specificity` (via_co_occur / degree; request option
    # only, it fills hop 1 with singletons) or `cosine` (via_co_occur /
    # sqrt(degree)).
    facts_traverse_rank_by: Annotated[Literal["co_occur", "specificity", "cosine"], CaseFolded] = (
        "cosine"
    )

    # ---- sensitivity and provenance ----
    # With no explicit `sensitivity`, detect it from the value with regexes:
    # credential shapes, card numbers, SSNs and stated PINs -> 'high' (never
    # embedded, key-only reads, audited); email/phone/IBAN -> 'medium' (embedded,
    # labelled). Off = every row stays ''.
    memory_sensitivity_autodetect: bool = True
    # `medium`/`high` rows get expires_at = now + N days on the set and bulk
    # paths (a shorter caller expiry wins) and, if the caller set none, this
    # on_expire. 0 = off.
    memory_sensitive_retention_days: int = 0
    # `on_expire` given to a sensitive row whose caller set none.
    memory_sensitive_on_expire: Literal["tombstone", "demote", "anonymize"] = "anonymize"
    # Daily sweep: re-label live rows written `sensitivity = ''` by a path that
    # skipped the detector, as a new version per row; `high` rows' embeddings
    # are tombstoned, never ALTER UPDATE. Covers the trailing window; the
    # backlog is `loom sensitivity-sweep --apply`.
    sensitivity_sweep_enabled: bool = True
    # Seconds between sensitivity sweep passes.
    sensitivity_sweep_interval_seconds: int = 86400
    # Days back the sensitivity sweep re-labels rows created in.
    sensitivity_sweep_window_days: int = 2
    # Rows one sensitivity sweep pass re-labels, and the keys per page it reads
    # (at least 1).
    sensitivity_sweep_max_rows_per_pass: int = Field(default=500, ge=1)
    # Pages one sensitivity sweep pass reads at most, each of
    # `sensitivity_sweep_max_rows_per_pass` keys (1 to 1000).
    sensitivity_sweep_pages_per_pass: Annotated[int, LIVE] = Field(default=10, ge=1, le=1000)
    # Shapes the sensitivity sweep applies (comma-separated: credential, email, phone,
    # other); every high row is `credential`; `phone` and `other` (IBAN-only rows) are
    # left to the operator's `loom sensitivity-sweep --apply`.
    sensitivity_sweep_shapes: str = "credential,email"
    # Tag rows mentioning a protected attribute or common proxy with
    # `attribute:<class>` at write time (gender, religion, health, age, family,
    # income, ethnicity, sexuality, politics). A label, never a gate; the
    # profile fold leaves such rows out.
    memory_attribute_tagging: bool = True
    # ---- code-linked memories ----
    # Link memories to the code they name (symbols and files of the repository
    # their `repo:` tag or code namespace names) and keep the links current as the
    # code changes; the namespace's `code_change_policy` decides how reads treat a
    # memory whose code is removed or changed. Off stops linking and rechecks; reads
    # still show the stored states but no longer hide a memory, as nothing keeps them
    # current.
    memory_code_links_enabled: bool = True
    # Seconds between link passes.
    memory_code_links_interval_seconds: int = 60
    # Seconds a code-links pass may run before the supervisor abandons it.
    memory_code_links_max_pass_seconds: float = Field(default=600.0, gt=0)
    # Memory writes one pass links (at least 1).
    memory_code_links_batch: int = Field(default=500, ge=1)
    # Seconds the memory code-link pass stays behind the newest queued memory write, so
    # inserts running at the same moment are visible before the cursor passes them (at
    # least 10). A write visible later than this is passed and stays unlinked until a
    # backfill reads it; a longer wait makes that rarer and links every write that much
    # later.
    memory_code_links_settle_seconds: int = Field(default=60, ge=10)
    # Days a code-shaped name that names nothing on trunk yet (unmerged code) is
    # retried before it is dropped.
    memory_code_links_pending_days: int = Field(default=14, ge=1)
    # Changed claims one pass re-checks with the LLM under the `revise` policy.
    memory_code_links_revise_per_pass: int = Field(default=20, ge=0)

    # ---- memory chunks ----
    # Max ~800-char verbatim chunks one set_from_messages call writes (512 ≈
    # 400K chars; agent transcripts run ~190K chars a session at the median).
    # Text past the cap is recorded in loom.memory_drops as
    # chunk.split/truncated.
    memory_chunks_max_per_call: int = 512
    # Max chunks per memory.set value (48 ≈ 34 KB); the tail is recorded in
    # loom.memory_drops as chunk.split/truncated.
    memory_set_chunk_max_chunks: int = 48
    # ---- decision extraction (loom decisions extract) ----
    # Decisions already recorded from a document that a re-run lists and judges against the
    # text; one past this many is never flagged or retired. 0 = none.
    memory_decision_extract_known_max: Annotated[int, LIVE] = Field(default=30, ge=0)
    # Characters one extraction read takes: a shorter document is read whole, a longer one in
    # windows of whole sections. Sized so a dense window's reply fits the decision_extract cap.
    memory_decision_extract_window_chars: Annotated[int, LIVE] = Field(default=24000, ge=500)
    # Closest decision keys a candidate is checked against for a pinned or attested restatement
    # under another key (cosine only); the fetch caps at 500 rows.
    memory_decision_extract_restatement_top_k: Annotated[int, LIVE] = Field(default=20, ge=1)
    # ---- fact-leg reads of memory_embeddings ----
    # Seconds a process caches its count of patch parts on
    # loom.memory_embeddings (server/core/vectors/patch_parts.py); the fact leg
    # reads quantized codes only while the count is zero.
    memory_embeddings_patch_parts_probe_seconds: int = 60

    # ---- timeouts, budgets and caches ----
    # Seconds the BM25 corpus statistics (document count, mean length) of a namespace
    # are cached.
    memory_bm25_cache_ttl_s: Annotated[float, LIVE] = Field(default=300.0, gt=0)
    # Most memories one blacklist sweep takes from ClickHouse's match (under the Postgres
    # authority it also reads the heads ClickHouse keeps refusing, this many at a time).
    memory_blacklist_sweep_limit: Annotated[int, LIVE] = Field(default=5000, ge=1)
    # Most memories one usage-fold pass folds.
    memory_usage_fold_max_memories_per_pass: int = Field(default=1000, ge=1)
    # Seconds one usage-fold pass may run before it stops and leaves the rest to the
    # next.
    memory_usage_fold_pass_time_budget_s: float = Field(default=120.0, gt=0)
    # Seconds a namespace's open-conflict count is cached between reads.
    memory_conflicts_count_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds a namespace's coverage-sweep override is cached.
    memory_coverage_sweep_gate_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds a facts.traverse result is reused for the same walk.
    memory_facts_traverse_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds a namespace's retrieval defaults are cached; a change reaches search
    # within it.
    memory_namespace_retrieval_defaults_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # The most observations a session's notes read: the notes prompt is bounded by
    # characters anyway; this bounds the read.
    memory_observe_extract_session_context_limit: Annotated[int, LIVE] = Field(default=400, ge=1)
    # Seconds the pending-extraction count a read discloses is cached.
    memory_pending_disclosure_ttl_s: Annotated[float, LIVE] = Field(default=5.0, gt=0)
    # Attempts a profile write makes when a concurrent write moved the profile under it.
    memory_profile_cas_max_retries: Annotated[int, LIVE] = Field(default=3, ge=1)
    # Seconds a namespace's proposal policy is cached; a change applies within it.
    memory_proposals_policy_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # A listing reads at most this many proposals before `only` / `limit` — the graded
    # counts are one statement over them; a namespace holding more on trial than this is a
    # queue nobody is deciding, not a page.
    memory_proposals_scan_cap: Annotated[int, LIVE] = Field(default=2000, ge=1)
    # Seconds the entity vocabulary of a namespace's summaries is cached for the summary
    # read leg.
    memory_summary_entity_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds a namespace with no calibration row (or a failed read) is remembered as
    # having none.
    memory_calibration_namespace_miss_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds a namespace's calibration row is cached, so a row written on another
    # replica serves here within it.
    memory_calibration_namespace_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds between the starts of two cycles of the code-links sweep through the link
    # table.
    memory_code_links_sweep_interval_s: Annotated[int, LIVE] = Field(default=3600, gt=0)
    # Seconds a branch's own read and parse of a code lesson may take, a share of the
    # code plane's enrichment bound; past it the lesson is served as trunk last checked
    # it.
    memory_code_plane_parse_timeout_s: Annotated[float, LIVE] = Field(default=0.2, gt=0)
    # One call judges at most this many facts; a longer capture is judged in slices
    # (groups never span slices — a missed fold only keeps a paraphrase).
    memory_durability_verifier_max_facts_per_call: Annotated[int, LIVE] = Field(default=60, ge=1)
    # Consecutive unparseable model replies for one memory before the entity extractor
    # stamps it failed and moves on.
    memory_entity_extract_parse_fail_max: Annotated[int, LIVE] = Field(default=3, ge=1)
    # Seconds facts.traverse waits, when it starts, for the graph projections of its
    # namespace still in flight in this process and the others.
    memory_facts_projection_wait_s: Annotated[float, LIVE] = Field(default=3.0, gt=0)
    # Seconds a failed namespace-health lookup is remembered (never longer than
    # namespace_health_ttl_seconds), so the context pack does not re-run failing reads
    # on every call.
    memory_namespace_health_failure_ttl_s: Annotated[int, LIVE] = Field(default=300, gt=0)
    # The most sections one search embeds on the fly.
    memory_profile_search_embed_cap: Annotated[int, LIVE] = Field(default=8, ge=1)
    # Retries of a vector read that ClickHouse answers with an empty-block error (code
    # 10 on a part still being written) before the search fails.
    memory_retrieval_empty_block_retries: int = Field(default=3, ge=0)
    # Seconds before the first retry of an empty retrieval block
    # (`memory_retrieval_empty_block_retries`); each later one waits one more step of it.
    memory_retrieval_empty_block_retry_sleep_s: float = Field(default=0.2, ge=0)
    # Most exposure rows one usage-fold pass reads to credit acted-on memories; the
    # watermark holds until a pass drains below it.
    memory_usage_fold_acted_max_rows_per_pass: int = Field(default=5000, ge=1)
    # Backpressure: a pass is skipped — watermark untouched — while the destination table
    # already carries this many unfinished mutations on the connected replica. Every
    # lightweight UPDATE is a patch part plus a Keeper log entry; enough of them pending
    # on a large table can expire replicas' Keeper sessions and leave them read-only.
    memory_usage_fold_max_pending_mutations: int = Field(default=20, ge=1)

    # ===== MemoryAsk ==============================================================

    @model_validator(mode="after")
    def _check_resolver_settings(self) -> MemorySettings:
        """Retiring takes more confidence than flagging, and flagging than clearing: the
        band between the flag and the retire confidence is the one that goes to review. A
        pass's burst ends before the supervisor's deadline would cut its bookkeeping."""
        if not (
            self.memory_conflict_resolver_clear_confidence
            <= self.memory_conflict_resolver_flag_confidence
            <= self.memory_conflict_resolver_retire_confidence
        ):
            raise ValueError(
                "LOOM_MEMORY_CONFLICT_RESOLVER_FLAG_CONFIDENCE must lie between "
                "LOOM_MEMORY_CONFLICT_RESOLVER_CLEAR_CONFIDENCE and "
                "LOOM_MEMORY_CONFLICT_RESOLVER_RETIRE_CONFIDENCE"
            )
        if (
            self.memory_conflict_resolver_burst_seconds
            > self.memory_conflict_resolver_max_pass_seconds / 2
        ):
            raise ValueError(
                "LOOM_MEMORY_CONFLICT_RESOLVER_BURST_SECONDS must be at most half of "
                "LOOM_MEMORY_CONFLICT_RESOLVER_MAX_PASS_SECONDS"
            )
        return self


class MemoryAskSettings(BaseModel):
    """memory.ask: evidence assembly and answer synthesis."""

    # memory.ask only: re-rank the synthesis window by similarity + weight * recency,
    # recency = 1/(1 + age_days/14), so the freshest copy of a fact reaches synthesis. 0.0 =
    # cosine only.
    memory_ask_recency_weight: Annotated[float, LIVE] = 0.2

    # Fraction of the namespace's 24 h cost cap at which the cascade routes every ask cheap,
    # with no escalation. 0 = off.
    memory_ask_synth_budget_cheap_ratio: Annotated[float, LIVE] = 0.7
    # Adaptive synthesis output budget: max(window rule, ratio * evidence tokens), capped at
    # memory_ask_synth_max_tokens_ceiling. The window rule (600 tokens at <= 15 memories,
    # 900 past 15) is the floor; 0 = the window rule alone.
    memory_ask_synth_budget_ratio: Annotated[float, LIVE] = 0.75
    # Upper bound on the adaptive budget (a cost and p99 guard, not a target); the
    # provider's own maximum still applies.
    memory_ask_synth_max_tokens_ceiling: Annotated[int, LIVE] = Field(default=8000, ge=1)
    # Fixed synthesis output-token budget; 0 = adaptive.
    memory_ask_synth_max_tokens: Annotated[int, LIVE] = 0
    # The cheap tier answers only when the synthesis window holds at most this many evidence
    # tokens (chars/4); above it the ask routes strong (cheap losses averaged 5.9k tokens).
    # 0 = off.
    memory_ask_synth_cheap_max_evidence_tokens: Annotated[int, LIVE] = 2500
    # Routing prior from the consistency judge's contradicted rate per route. '' = rule-only
    # router; "measured" = synth_policy.MEASURED_ROUTE_PRIOR; or a JSON object route ->
    # rate. A strong-routed ask whose route rate is <=
    # memory_ask_synth_route_prior_max_cheap goes cheap (reason "<rule>:prior_cheap"); hard
    # stops (identifier, breadth, rich evidence, as_of, ungrounded) never move.
    memory_ask_synth_route_prior: Annotated[str, RoutePrior, LIVE] = ""
    # Route contradicted rate at or below which the routing prior sends a strong-routed ask cheap.
    memory_ask_synth_route_prior_max_cheap: Annotated[float, LIVE] = 0.10
    # Which strong-routed reasons go to the mid tier: "uncertain", "off", or a comma list of
    # reason tokens.
    memory_ask_synth_mid_routes: Annotated[str, LIVE] = "uncertain"

    # How many top memories the synthesizer sees, independent of the caller's top_k; also
    # sizes the fetch pool and, above 15, the answer-token budget. Raise it only with a
    # strong memory_ask_synth_model, since a weak one dilutes over more items.
    memory_ask_context_memories: Annotated[int, LIVE] = Field(default=15, ge=1)
    # Deep-evidence pass: a selector picks up to memory_ask_deep_evidence_max_memories
    # ranked candidates, their source turns are read (loom.memory_evidence via
    # loom.memory_belief_sources, scoped rows first) and synthesis reruns with the raw
    # turns. Runs on depth="raw" and temporal questions. This field: how many candidates
    # the selector sees (30 = the whole 2x top_k pool).
    memory_ask_deep_evidence_pool: Annotated[int, LIVE] = Field(default=30, ge=1)
    # Who picks the memories to open: "llm" = one selection call over the pool; "top" = the
    # top-ranked memories, no LLM call. A request's deep_select overrides.
    memory_ask_deep_evidence_select: Annotated[Literal["llm", "top"], CaseFolded, LIVE] = "llm"
    # At most this many memories are opened per ask.
    memory_ask_deep_evidence_max_memories: Annotated[int, LIVE] = Field(default=5, ge=1)
    # Source-text characters deep evidence may open per memory, so a window-linked
    # memory cannot expand into its whole conversation.
    memory_ask_deep_evidence_max_chars_per_memory: Annotated[int, LIVE] = Field(default=2500, ge=1)
    # Total source-text characters deep evidence may open across all memories of one ask.
    memory_ask_deep_evidence_max_chars: Annotated[int, LIVE] = Field(default=8000, ge=1)

    # Default answer length in characters for asks that pass max_answer_chars=0, except
    # breadth questions and structured count/preference/affect answers. 0 = unbounded
    # (synthesis is 88% of ask median latency).
    memory_ask_default_answer_chars: Annotated[int, LIVE] = 0
    # Selector the temporal trigger uses: "llm" (one selection call, about +3 s p50 on
    # temporal asks only) or "top" (no model call). Per-request deep_select overrides.
    memory_ask_temporal_deep_select: Annotated[Literal["llm", "top"], LIVE] = "llm"
    # Client for the deep selector: "cheap" = the cascade's cheap client when configured,
    # else the routed client; "routed" = the client that synthesizes the answer.
    memory_ask_deep_select_tier: Annotated[Literal["cheap", "routed"], LIVE] = "cheap"

    # When a query names an identifier (PR/issue number, @handle) that recall missed, inject
    # the memories citing it (exact substring) into the synthesis pool. The lexical leg
    # indexes only alpha tokens >= 3 chars, so bare numbers are otherwise unretrievable.
    # Additive.
    memory_ask_identifier_injection: Annotated[bool, LIVE] = True
    # After injection, follow identifier-shaped tokens the injected memories name (a value
    # that is a key, a PR naming a commit) for up to this many more exact-substring reads,
    # adding at most memory_ask_identifier_chase_cap rows. 0 = off. Runs only when the
    # question named an identifier.
    memory_ask_identifier_chase_hops: Annotated[int, LIVE] = 3
    # Identifier injection also reads up to this many verbatim chunks (loom.memory_chunks),
    # ranked by cosine to the question; 0 = facts only.
    memory_ask_identifier_inject_chunks: Annotated[int, LIVE] = 0
    # A set-shaped question about an identifier reads this many fact rows (and twice as many
    # chunks) from the exact fetch instead of eight.
    memory_ask_identifier_inject_set_facts: Annotated[int, LIVE] = 24
    # For a bug / error / flaky question, read this many verbatim chunks that cite the
    # identifier and carry fix-or-cause vocabulary (root cause, Closes #, backtrace ...)
    # into the pool ahead of the window. 0 = off.
    memory_ask_decisive_chunks: Annotated[int, LIVE] = 6
    # For a disposition / premise / classification question, read this many newest facts and
    # chunks citing the identifier into the pool, and tell the synthesizer the later-dated
    # memory is current when memories on one subject disagree. 0 = off.
    memory_ask_identifier_inject_recent: Annotated[int, LIVE] = 4
    # A question about what was mentioned at a time reads up to this many verbatim chunks
    # whose session date falls in the resolved window (±1 day), cosine-ranked; facts carry
    # the event date, not the mention date. 0 = off.
    memory_ask_mention_window_chunks: Annotated[int, LIVE] = 4
    # Fact rows the identifier chase (identifiers named by the rows the question's
    # identifier reached) may add across all its hops.
    memory_ask_identifier_chase_cap: Annotated[int, LIVE] = 8
    # Verbatim chunks the chase may add, counted apart from memory_ask_identifier_chase_cap.
    # 0 = facts only.
    memory_ask_identifier_chase_chunk_cap: Annotated[int, LIVE] = 8

    # Facet completion: a count question pulls every memory in scope whose key shares the hit
    # keys' attribute facet (user_maya_age -> _age), widening the window to
    # memory_ask_aggregational_window. At most this many rows.
    memory_ask_facet_completion_max: Annotated[int, LIVE] = 60
    # Bridge completion: the other attributes of the entities the top hits name (key-prefix
    # siblings) join the pool, so a question that names an entity by one attribute can
    # answer with another. This is the number of top hits whose entities it reads.
    memory_ask_bridge_completion_top_hits: Annotated[int, LIVE] = 12
    # Most rows bridge completion adds to the pool.
    memory_ask_bridge_completion_max: Annotated[int, LIVE] = 48
    # Most rows bridge completion adds per entity key prefix.
    memory_ask_bridge_completion_per_prefix: Annotated[int, LIVE] = Field(default=6, ge=1)
    # Hops over the rows — the
    # proper-noun objects of the top hits and their bridged siblings bridge
    # again, twice at most, so a multi-hop question is answered from rows when
    # the triple plane has no path.
    memory_ask_bridge_row_hops: Annotated[int, LIVE] = 2
    # Count questions: the raw turns naming the question's literal (a place, a number) join
    # the pool, at most this many — a fact the extractor dropped is still a turn.
    memory_ask_literal_completion_max: Annotated[int, LIVE] = 40
    # The synthesis window a count question widens to when facet or literal completion
    # adds rows (never narrower than the window it had).
    memory_ask_aggregational_window: Annotated[int, LIVE] = 40
    # Question decomposition (request `decompose`, off when unset): most
    # sub-questions a decomposed question splits into.
    memory_ask_decompose_max_parts: Annotated[int, LIVE] = 4
    # Rows retrieved for each decomposed sub-question.
    memory_ask_decompose_part_top_k: Annotated[int, LIVE] = 8
    # memory.ask general-knowledge bridging: forbid (default)
    # keeps the synthesizer memory-only; allow lets it combine a memory fact
    # with widely known public facts, marking each bridged sentence, and the
    # output's `grounding` says whether it did. Per-request `world_knowledge`
    # overrides. Agents that must never answer beyond the store keep forbid.
    memory_ask_world_knowledge: Annotated[Literal["forbid", "allow"], CaseFolded, LIVE] = "forbid"
    # The fewest synthesis rows memory.ask's relevance gate (a cut at the knee of the
    # rows' similarity curve) may leave.
    memory_ask_relevance_gate_min_keep: Annotated[int, LIVE] = 3
    # Synthesis window for count-shaped questions ("how many X", op=count), whose answer set
    # is scattered across sessions (a missed item sat at rank 18). 0 = no widening.
    memory_ask_count_window: Annotated[int, LIVE] = 30

    # Kill-switch for code-plane enrichment: when a query names code-shaped identifiers (a
    # symbol, a File.cpp, a snake_case setting), fuse the bound code index's file:line,
    # symbol and recent PRs into the grounding pool. Additive and fail-open. The opt-in is
    # the code namespace binding (_resolve_code_target): with no per-request override and no
    # memory_ask_code_namespace, enrichment is skipped, hence default on.
    memory_ask_code_enrichment: Annotated[bool, LIVE] = True
    # Symbols the retrieved window names (not only the question) are looked up in the bound
    # code index too, up to this many, one code.symbol definition each, appended after the
    # question's own grounding rows. 0 = off.
    memory_ask_code_hop_symbols: Annotated[int, LIVE] = 4
    # Token budget of the code.symbol read that grounds a code-entity question in the
    # code plane.
    memory_ask_code_symbol_token_budget: Annotated[int, LIVE] = Field(default=400, ge=200, le=12000)
    # Token budget of the code.search read that grounds a code-entity question.
    memory_ask_code_search_token_budget: Annotated[int, LIVE] = Field(
        default=1400, ge=200, le=12000
    )

    # For each PR the query or grounding references, inject its authoritative
    # code_pr_history record (author, merged_at, title) as a high-trust grounding fact; the
    # synthesizer prefers it over a memory's older claim (_PR_RECORD_NOTE). Gated by the
    # code binding (_resolve_code_target); additive, fail-open, as-of aware. See
    # _inject_pr_facts.
    # A judged arm shows fewer control regressions with it on than off.
    memory_ask_pr_grounding: Annotated[bool, LIVE] = True

    # For an as_of query, drop grounding memories that cite a PR opened after the anchor (an
    # under-dated bulk ingest can name a later PR). Open dates come from
    # code_pr_history.opened_at; a no-op without as_of or a bound code index; a PR with an
    # unknown open date is never gated. See _gate_future_prs.
    memory_ask_pr_temporal_gate: Annotated[bool, LIVE] = True

    # Check the code locations a retrieved memory asserts against the live index and flag
    # rotted ones (symbol moved or renamed) so synthesis prefers the current site.
    # Read-time, additive, gated by the code binding; ambiguous symbols are skipped
    # (code_anchor), never false-flagged.
    memory_ask_code_staleness: Annotated[bool, LIVE] = True

    # Code namespace enrichment reads when the request sets none (a per-request override
    # wins). '' = enrichment skipped: with many repos and branches indexed as separate
    # namespaces there is no safe global default. Pair with memory_ask_code_org when the org
    # differs.
    memory_ask_code_namespace: Annotated[str, LIVE] = ""

    # An ordering question with a time cue of up to this many days reads the user's turns
    # from that window with the first synthesis, so a member the extractor left as an aside
    # is listed.
    memory_ask_order_window_max_days: Annotated[int, LIVE] = 95
    # How many user turns dated inside the question's short time window ("a couple of days
    # ago") the not-recorded second reading adds, whatever words they carry.
    memory_ask_cue_window_turns_top_k: Annotated[int, LIVE] = Field(default=24, ge=1)
    # Dated raw turns the not-recorded second reading recalls.
    memory_ask_when_evidence_top_k: Annotated[int, LIVE] = Field(default=6, ge=1)
    # Org of memory_ask_code_namespace; "" = code_org, then the ask's own org.
    memory_ask_code_org: Annotated[str, LIVE] = ""

    # After synthesis one judge call (the cheap client when the cascade is on, else the
    # synth client) labels the answer entailed / neutral / contradicted against the memories
    # it cites (consistency, consistency_score, consistency_note). Off: one extra LLM call
    # per synthesized ask.
    memory_ask_consistency_check: Annotated[bool, LIVE] = False
    # Where the judge runs. "inline" awaits it and returns the verdict on the wire.
    # "deferred" answers first, leaves the wire fields empty, and writes the verdict as a
    # memory.ask.consistency ledger row (kind='verdict', joined by request_id) plus the
    # contradicted usage event; a precedent is stored only once the verdict
    # is in. A request's consistency field pins either; benches that read the label pin
    # "inline".
    memory_ask_consistency_mode: Annotated[Literal["inline", "deferred"], LIVE] = "deferred"
    # When the window holds a pinned or human-verified memory, one judge call asks whether
    # the question presupposes something it contradicts; if so the synthesizer holds the
    # trusted fact and corrects the premise (premise_conflict / premise_note on the wire).
    # One read per ask; an LLM call only with trusted rows in the window.
    memory_ask_premise_check: Annotated[bool, LIVE] = False
    # Synthesis window for a set question ("what books has she read"), like a count's, since
    # its items sit one per session. 0 = off. MemoryAskInput.set_window overrides.
    memory_ask_set_window: Annotated[int, LIVE] = 40
    # A count or set question reads up to this many rows (window and search pool) instead of
    # the 40-row pool; 0 = off. Such an ask costs about four times a plain one.
    memory_ask_set_full_scan: Annotated[int, LIVE] = 300
    # When the whole-set scan widens the pool, cap its chunk share so it is mostly fact
    # rows. 0 = uncapped.
    memory_ask_set_full_scan_chunk_share: Annotated[float, LIVE] = 0.15
    # A chain question ("the citizenship of the CEO of X") follows the first hop's answer
    # entity into one more retrieval per bridge entity; this is the most entities it follows.
    memory_ask_entity_follow_max: Annotated[int, LIVE] = 3
    # Rows retrieved for each followed bridge entity.
    memory_ask_entity_follow_top_k: Annotated[int, LIVE] = 6
    # Passages the knowledge.search leg appends to the synthesis window.
    memory_ask_knowledge_top_k: Annotated[int, LIVE] = Field(default=5, ge=1)

    # memory.ask keeps declared-external, unattested rows out of the synthesis
    # pool (they still surface as hits). Off = they are synthesized like any other row.
    memory_ask_exclude_unverified_external: Annotated[bool, LIVE] = True

    # Serve the earlier synthesized answer to the same question and shape, with no LLM call,
    # when the synthesis window is byte-identical (same keys, same content_sha256 per key);
    # otherwise synthesize and store. A per-request precedents field overrides.
    memory_ask_precedents: Annotated[bool, LIVE] = False
    # Hours a stored precedent stays servable; 0 = no age bound, the evidence alone decides.
    memory_ask_precedents_max_age_hours: Annotated[int, LIVE] = 0
    # Comma-separated fnmatch globs the precedent flag applies to; '' = every namespace.
    # Scope it to namespaces whose traffic is real repeats (e.g. "support-*"): on a shared
    # server a paired A/B would answer its second arm from the first.
    memory_ask_precedents_namespaces: Annotated[str, LIVE] = ""
    # With precedents on, an eligible precedent is served before retrieval when no plane its
    # namespaces read was written since (loom.namespace_write_marks), the blacklist is
    # unchanged and every cited row is live with the same hash; never under the Postgres
    # authority, whose heads ClickHouse's marks can trail. Hours a precedent stays
    # servable on that fast path (a bound on rank drift, not staleness); 0 = no bound.
    memory_ask_precedents_fast_max_age_hours: Annotated[int, LIVE] = 1

    # Exact-prompt LLM response cache for every memory.ask synthesis-path call, keyed by
    # sha256(org, system, user, max_tokens, response_format) per model in
    # loom.llm_response_cache; a miss writes fire-and-forget. The evidence is in the key, so
    # a changed memory misses. `memory_ask_llm_cache_namespaces` scopes the default; a
    # per-request llm_cache wins (benches measuring synthesis send false).
    memory_ask_llm_cache_default: Annotated[bool, LIVE] = True
    # Comma-separated fnmatch namespace globs the LLM cache default applies to; '' = all.
    memory_ask_llm_cache_namespaces: Annotated[str, LIVE] = ""

    # memory.ask pending evidence: the turns of in-flight deferred batches that share words
    # with the question join the synthesis window as raw rows (source="pending").
    # Most pending turns read into the synthesis window per ask.
    memory_ask_pending_evidence_turns: Annotated[int, LIVE] = Field(default=5, ge=1)
    # Newest in-flight deferred batches the pending-evidence read scans.
    memory_ask_pending_evidence_batches: Annotated[int, LIVE] = Field(default=20, ge=1)

    # A summary-shaped question ("summarize the report") routes to knowledge.synthesize over
    # the documents the knowledge search concentrates on, falling through to the memory
    # path when none concentrates. Documents that route may synthesize over at most.
    memory_ask_synthesize_route_max_docs: Annotated[int, LIVE] = Field(default=3, ge=1)
    # Characters of one knowledge passage in the answer's context block; longer
    # ones are cut and marked.
    memory_ask_knowledge_passage_max_chars: Annotated[int, LIVE] = Field(default=800, ge=100)
    # Characters of the whole knowledge block; the lowest-ranked passages go first.
    memory_ask_knowledge_block_max_chars: Annotated[int, LIVE] = Field(default=6000, ge=100)
    # Conversation turns the recent-turns block carries at most, newest last.
    memory_ask_recent_max_turns: Annotated[int, LIVE] = Field(default=40, ge=1)
    # Characters of one turn in memory.ask's recent-turns block; longer ones are cut and
    # marked.
    memory_ask_recent_turn_max_chars: Annotated[int, LIVE] = Field(default=1500, ge=100)
    # Characters of the whole recent-turns block; the oldest turns go first.
    memory_ask_recent_block_max_chars: Annotated[int, LIVE] = Field(default=12000, ge=100)


# ===== MemorySearch ===========================================================


class MemorySearchSettings(BaseModel):
    """memory.search: legs, fusion, ranking and gates."""

    # Default `namespace_scope` when a request leaves it unset. "this" reads
    # the addressed namespace only; "all_mine" also reads the caller's own
    # namespaces (the MCP loom_search asks for it by name), see
    # docs/concepts/identities-and-namespaces.md#reading-several-namespaces.
    memory_search_default_namespace_scope: Annotated[Literal["this", "all_mine"], LIVE] = "this"
    # Namespace kinds an all_mine fan-out spans (memory.search,
    # conversation.search, MCP loom_search; knowledge.search reads
    # knowledge_search_fanout_kinds): comma list over loom.namespaces.kind; a
    # namespace with no kind row stays; "" = every kind. Code and QA indexes
    # are reached by name or their own tools (measured sweep of 24 namespaces: 6.2 s).
    memory_search_fanout_kinds: Annotated[str, LIVE] = "memory"
    # Upper bound on how many of the caller's namespaces an all_mine fan-out
    # reads besides the addressed one; a search names the rest in
    # `namespaces_skipped`. Every memory leg issues at least one statement per
    # namespace. A guard, not a tuning knob: set well above what one person
    # holds, so it bounds a runaway sweep rather than trimming normal ones.
    memory_search_fanout_max_namespaces: Annotated[int, LIVE] = Field(default=32, ge=1)
    # Seconds memory.search's reranker endpoint (a named endpoint of rerank_endpoints_json,
    # or builtin:cohere) may take to answer; past it the search keeps its input order and
    # says so in `degraded`. builtin:openai is an LLM call and waits llm_request_timeout_s.
    memory_search_rerank_timeout_s: Annotated[float, LIVE] = Field(default=8.0, gt=0)
    # Key-embedding axis on the fact leg: "on" scores least(value_dist,
    # key_dist) and reads both vector columns (measured 399 vs 203 MB/query on
    # a 30k-row namespace); "off" reads the value vector only; "auto" turns it
    # on per namespace when its key-reuse share is at or above
    # memory_search_key_axis_auto_reuse_share (cached for
    # memory_search_key_axis_auto_ttl_seconds; a failed probe reads as off).
    # A per-request `key_axis` wins.
    memory_search_key_axis: Annotated[Literal["auto", "on", "off"], CaseFolded, LIVE] = "auto"
    # "auto" key-axis threshold: share of a namespace's keys written more than
    # once. 0.05 sits between product namespaces and chat namespaces.
    memory_search_key_axis_auto_reuse_share: Annotated[float, LIVE] = Field(
        default=0.05, ge=0.0, le=1.0
    )
    # Seconds a namespace's "auto" key-axis probe result is cached before it is re-read.
    memory_search_key_axis_auto_ttl_seconds: Annotated[int, LIVE] = Field(default=600, ge=1)
    # No-probe fallback for the query-rewrite gate the calibration reports
    # (calibration.get's scalar row): ≈ question→fact P10 for
    # text-embedding-3-small.
    memory_search_query_rewrite_threshold: float = 0.38

    # Recursive rounds the fallback cascade (iterative-judge retry) may run; it stops at
    # this or `memory_search_fallback_max_wall_ms`, whichever trips first. The first-pass
    # retrieve always runs.
    memory_search_fallback_max_rounds: Annotated[int, LIVE] = 1
    # Wall-clock ms the fallback cascade may spend across all rounds before it stops.
    memory_search_fallback_max_wall_ms: Annotated[int, LIVE] = 4000
    # LLM calls one fallback round may spend before further ones are denied.
    memory_search_fallback_max_llm_calls: Annotated[int, LIVE] = 2
    # Vector queries one fallback round may issue before further ones are denied.
    memory_search_fallback_max_vector_queries: Annotated[int, LIVE] = 12

    # First-stage pool = top_k × multiplier, per retrieval profile, capped at
    # memory_search_max_pool_top_k (all_mine searches multiply by namespace
    # and sub-query count).
    memory_search_pool_multiplier_standard: Annotated[int, LIVE] = 3
    # Rerank gets the widest pool: a reranker only reorders what the first
    # stage surfaced (measured: items it should promote sat at rank 11-30).
    # Affects rerank-enabled searches only.
    memory_search_pool_multiplier_rerank: Annotated[int, LIVE] = 6
    # A deep-budget search's pool multiplier (the budget class `deep`).
    memory_search_pool_multiplier_deep: Annotated[int, LIVE] = 5
    # Upper bound on the first-stage pool size; a caller's top_k above it raises the bound to
    # top_k.
    memory_search_max_pool_top_k: Annotated[int, LIVE] = 150
    # Pool widening factor for a quality-filtered search (loom_search,
    # context.pack), applied before junk is dropped.
    memory_search_quality_pool_multiplier: Annotated[int, LIVE] = 3

    # Boost for any preference-typed/tagged row on any query; 0.0 makes it
    # intent-gated (only a query naming the attribute earns +0.3).
    memory_search_preference_fallback: Annotated[float, LIVE] = 0.15
    # Comma list of source_class values whose rows may earn the user-derived
    # rank terms (preference, prior group, reinforcement); "" = every row.
    # Leaving out "extracted" keeps an inference from outranking a statement
    # by its priors.
    memory_search_source_class_boost_eligible: Annotated[str, LIVE] = ""
    # Top-hit similarity below which a search abstains (weak abstention): the
    # scalar gate for fake embedders or LOOM_ADAPTIVE_THRESHOLDS=false; a
    # calibrated namespace or embedder derives its own.
    memory_search_abstain_threshold: float = 0.30

    # Identifier-grounding abstain: when the query carries a salient
    # identifier (PR/issue number, test id, @handle) and no returned hit
    # contains any of them, abstain with abstain_code "entity_absent"
    # regardless of the top cosine — catches the on-topic but wrong neighbour
    # the cosine gate cannot.
    memory_search_identifier_grounding: Annotated[bool, LIVE] = True
    # The grounding leg applies only to queries of at most this many
    # whitespace tokens; longer inputs (a PR body, a path list) carry
    # incidental numbers. 0 = no cap.
    memory_search_identifier_grounding_max_tokens: Annotated[int, LIVE] = 32

    # Recency-intent searches ("latest", "current …") re-rank the fused pool
    # by date: the newest dated hit gains this much, the oldest and undated
    # hits nothing. 0.0 = off. A namespace can set its own in
    # retrieval_defaults.
    memory_search_recency_pool_weight: Annotated[float, LIVE] = 0.0

    # No-probe fallback for the question→fact P25 gate (≈ P25 for
    # text-embedding-3-small): the loom_search precision floor and
    # calibration.get's scalar row read it.
    memory_search_hyde_fallback_threshold: float = Field(default=0.45, gt=0.0, le=1.0)
    # Seconds the HyDE model call may take before the search embeds the query itself.
    memory_search_hyde_timeout_s: Annotated[float, LIVE] = Field(default=6.0, gt=0)
    # search_mode used when the caller sets neither search_mode nor a recipe.
    # The wire default stays "rrf"; planner shape rules still apply on top.
    memory_search_default_mode: Annotated[
        Literal[
            "semantic", "lexical", "hybrid", "bm25_hybrid", "related", "graph", "rrf", "temporal"
        ],
        CaseFolded,
        LIVE,
    ] = "semantic"
    # Weight of the decayed-reinforcement rank term (recalls and positive
    # memory.outcome signals, fading with time since last reinforcement).
    # 0 disables; the term is capped below relevance.
    memory_search_reinforcement_weight: Annotated[float, LIVE] = 0.15
    # Additive penalty on rows tagged `sensitivity:medium|high` unless the
    # query has contact intent. 0.0 disables.
    memory_search_sensitivity_demote: Annotated[float, LIVE] = 0.1
    # Days after which a use's reinforcement weighs half: the base half-life, which
    # grows with a row's use count.
    memory_search_reinforcement_half_life_days: Annotated[float, LIVE] = 90.0
    # Per-query recall telemetry into loom.search_trace_log (query, returned
    # keys+scores, abstain, latency), fire-and-forget. Off by default: query
    # text is logged, so it is an operator opt-in.
    search_trace_log_enabled: Annotated[bool, LIVE] = False
    # Past queries as nodes: one loom.query_nodes row per memory.search
    # (normalised text, embedding, returned keys), gated on the namespace's
    # capture consent; the erasure saga deletes the table.
    memory_query_nodes_enabled: Annotated[bool, LIVE] = False
    # Set-expansion instance legs (collective nouns -> "has a sister"): each
    # phrase runs as a narrow sub-query leg of this many rows; a row joins only above
    # `memory_search_set_expansion_min_similarity`, at most
    # `memory_search_set_expansion_max_added` per search, never above the main leg's best
    # row.
    memory_search_set_expansion_leg_top_k: Annotated[int, LIVE] = 2
    # Similarity a set-expansion leg's row must exceed to join the pool.
    memory_search_set_expansion_min_similarity: Annotated[float, LIVE] = 0.5
    # Most rows the set-expansion legs add to one search.
    memory_search_set_expansion_max_added: Annotated[int, LIVE] = 5
    # Read-side exposure ledger in loom.memory_exposures: one row per read
    # (token subject, returned keys, query digest never the text, scope) plus
    # an hourly per-actor roll-up. Carries no user content.
    memory_exposure_ledger_enabled: Annotated[bool, LIVE] = True
    # Cosine floor: hits below it are dropped as noise (ANN always returns
    # top_k). The backstop when rerank is bypassed or fails.
    memory_search_min_similarity: float = 0.22
    # Sub-floor handling when the caller sets top_k: "readmit" (fill k, tag
    # below_floor), "ids_only" (fill k with empty bodies), "drop". A request's
    # floor_mode overrides.
    memory_search_floor_mode: Annotated[
        Literal["readmit", "ids_only", "drop"], CaseFolded, LIVE
    ] = "readmit"
    # Per-attempt bound (seconds) on the query embedding; after the retries
    # the search answers 503 EMBED_TIMEOUT. 0 = unbounded.
    memory_search_embed_bound_s: Annotated[float, LIVE] = 2.5
    # Retries of a query embedding after a timed-out first attempt, each bounded by
    # `memory_search_embed_bound_s`.
    memory_search_embed_bound_retries: Annotated[int, LIVE] = 1
    # A query of at most this many words embeds each all-lowercase word of 4+
    # letters in the identifier spelling the caller's rows in the addressed
    # namespace use most ("datalakecatalog" -> "DataLakeCatalog"; never
    # "what" -> "What"). One read of the namespace beside the embed (100-300 ms
    # for 40k rows), reused for a minute; fail-open. 0 = off.
    memory_search_query_spelling_max_words: Annotated[int, LIVE] = Field(default=6, ge=0)
    # Body length (chars) for bodies="excerpt" (the MCP search default); a decision or
    # constraint row keeps its stored excerpt.
    memory_search_excerpt_chars: Annotated[int, LIVE] = Field(default=200, ge=1)
    # Above this top_k a search that pinned no `bodies` serves excerpts, and
    # per-hit explanation / score_components ride only under `explain: true`.
    # Keys, order and scores are unchanged. 0 = never.
    memory_search_excerpt_bodies_k: Annotated[int, LIVE] = 15
    # Seconds loom_search's read of the scoped repository's decisions may take; a slower
    # read adds nothing and the memory section serves the home search alone.
    memory_repo_decisions_timeout_s: Annotated[float, LIVE] = Field(default=1.5, gt=0)

    # Visibility-ordered private-first fallback (non-scoped tokens): the best
    # private-namespace similarity that skips the public pass. "Private" is
    # namespace visibility, not the caller's role; scoped tokens use their
    # priority order instead.
    memory_search_private_first_stop_similarity: Annotated[float, LIVE] = 0.30

    # Tier waterfall stop: a per-tier best similarity at or above this stops
    # the search before lower-priority tiers. A control cutoff, not a return
    # filter (memory_search_min_similarity still gates rows); keep it above
    # the noise band (~0.10-0.25) or a middling hit hides a stronger one.
    memory_search_tier_stop_similarity: Annotated[float, LIVE] = 0.40

    # A key matches when it covers at least this share of the query's
    # idf-weighted stems (idf over the namespace's own keys).
    memory_search_key_match_min_strength: Annotated[float, LIVE] = 0.5
    # Boost (cosine units) of a full-strength key match: a tie-break inside
    # the pool's band, not a lever over it (a 0.5 scale lifted wrong rows
    # over gold).
    memory_search_key_match_boost_scale: Annotated[float, LIVE] = 0.03
    # Tie-break boost (cosine units) for a hand-asserted fact row (source_class
    # 'asserted', not a hint:/procedure/summary:/obs: row) against derived
    # rows; never a lever over a stronger match. 0 disables.
    memory_search_asserted_boost_scale: Annotated[float, LIVE] = 0.03
    # When the key-match arm runs: "always" (every search; measured 585-781 ms
    # p50 on live namespaces), "gated" (only when an added row could reach the
    # served window: rrf/rerank pool, pool short of top_k, an under-covered
    # identifier, or a pooled key already matching), "off".
    memory_search_key_match_mode: Annotated[Literal["always", "gated", "off"], LIVE] = "gated"
    # Rows of one memory_key kept by the final block, key-match and the entity follow (0 = no cap;
    # the first two keep all for aggregational/temporal_diff), and one-per-key callers' over-fetch.
    memory_search_per_key_cap: Annotated[int, LIVE] = 3
    # How many of a writer's served rows of one key, those with the newest vectors, a pooled
    # memory.set chunk is matched against, by value, to find the row it was cut from.
    memory_search_set_chunk_rows: Annotated[int, LIVE] = Field(default=64, ge=1)
    # How memory.search dates a chunk: "session" by its conversation's digest, "parent" by
    # the session's first fact anchor.
    memory_search_chunk_date_source: Annotated[Literal["session", "parent"], LIVE] = "session"
    # Top session digests in the pool that digest descent expands: a digest among them
    # adds its session's top facts as candidates (+300 ms ask p50);
    # MemorySearchInput.digest_descent overrides.
    memory_search_digest_descent_top_m: Annotated[int, LIVE] = Field(default=3, ge=1)
    # Facts digest descent adds as candidates from each expanded session.
    memory_search_digest_descent_k: Annotated[int, LIVE] = Field(default=5, ge=1)
    # What seeds memory.search's graph leg: "query", "recent" or "both". Under recent or
    # both, the entities of the identity's newest memories
    # (`memory_search_graph_seed_recent_memories`) join the query's seeds.
    memory_search_graph_seed: Annotated[Literal["query", "recent", "both"], LIVE] = "query"
    # Newest memories of the identity whose entities seed the graph under recent/both.
    memory_search_graph_seed_recent_memories: Annotated[int, LIVE] = Field(default=5, ge=1)
    # Most recent-memory entities added to the graph seeds under recent/both.
    memory_search_graph_seed_recent_max: Annotated[int, LIVE] = Field(default=8, ge=1)
    # bm25_hybrid merges the once-a-minute BM25 roll-ups with a live aggregate
    # over rows newer than their mark, so df and corpus size are exact. Off =
    # roll-ups alone (up to ~70 s behind a write).
    memory_search_bm25_live_tail: Annotated[bool, LIVE] = True
    # Seconds a parked page (a `paging=true` search's tail) stays fetchable
    # before the TTL merge reclaims it.
    memory_search_page_ttl_s: Annotated[int, LIVE] = Field(default=1800, ge=1)

    # Entity-boost candidate shape (server/memory/app/search/ann_entity_boosts.py).
    # loom.entity_vectors holds one row per (entity, memory_key) edge, so the
    # lookup ranks distinct entities first, then expands to edges (a flat
    # LIMIT surfaced as few as 1 distinct entity). Distinct entities the lookup keeps;
    # `memory_search_entity_boost_max_pairs` bounds the edge expansion (a hub can carry
    # hundreds), which bounds work rather than results.
    memory_search_entity_boost_top_entities: Annotated[int, LIVE] = 20
    # Most (entity, memory_key) edges the entity-boost lookup expands to.
    memory_search_entity_boost_max_pairs: Annotated[int, LIVE] = 400
    # Weight on the provenance term (source_class + verified); 0 = off, a
    # strict no-op in every mode.
    memory_search_trust_weight: Annotated[float, LIVE] = 0.0
    # Prepend the extractor's `question_topic` to the embedded text of
    # assistant-direct-answer facts, so a topic-only follow-up matches them.
    # The stored value is untouched; takes effect on (re-)ingest.
    memory_embed_answer_topic: Annotated[bool, LIVE] = True
    # Pre-built entity summaries memory.search's entity-summary leg fetches per namespace.
    memory_search_entity_summaries_top_n: Annotated[int, LIVE] = 5

    # Per-token top_k ceiling by the token's role on the namespace: viewer and
    # writer each have one, admin and above are uncapped. 0 = no ceiling. The
    # applied ceiling rides the response as `top_k_ceiling` and the ledger as
    # `out_top_k_ceiling`.
    memory_search_top_k_ceiling_viewer: Annotated[int, LIVE] = 0
    # top_k ceiling for a writer token on the namespace; 0 = no ceiling.
    memory_search_top_k_ceiling_writer: Annotated[int, LIVE] = 0

    # RRF adaptive lexical weight: code/path-shaped queries (CamelCase,
    # extensions, separators, digit runs) down-weight the lexical leg so
    # fusion leans semantic; prose keeps full weight.
    # Shape at/below which w_lex == 1.0 (prose).
    memory_rrf_code_shape_lo: Annotated[float, LIVE] = 0.20
    # Shape at/above which w_lex == memory_rrf_lexical_weight_floor (code).
    memory_rrf_code_shape_hi: Annotated[float, LIVE] = 0.30
    # Least weight the RRF adaptive lexical weight gives the lexical leg on a code-shaped
    # query, so it is never fully zeroed.
    memory_rrf_lexical_weight_floor: Annotated[float, LIVE] = 0.1

    # Smallest window share verbatim chunks keep when the pool has them.
    # 0.0 = off.
    memory_search_chunk_min_share: Annotated[float, LIVE] = 0.0
    # Which cosine the abstain gate reads: "first" = the fused rank-1 row,
    # "window" = the best cosine among returned rows.
    memory_search_abstain_top_sim_scope: Annotated[str, LIVE] = "first"
    # Codes-first main fact leg (`vector_search_use_quantized_codes = 1`),
    # safe only while loom.memory_embeddings has no patch part (ClickHouse
    # skips the lazy rescore under patch parts). "auto" follows a 60 s
    # system.parts probe (gauge loom.memory_embeddings.patch_parts); "on" /
    # "off" pin it. The setting ships only where
    # the replica probe finds it.
    memory_search_fact_codes_first: Annotated[Literal["auto", "on", "off"], CaseFolded, LIVE] = (
        "auto"
    )
    # Over-fetch of the codes pass on the main leg; x1 is too low (it changes the
    # top-10 on a quarter of queries).
    memory_search_fact_codes_fetch_multiplier: Annotated[float, LIVE] = 3.0
    # `max_threads` pin on the main fact statement; 0 = no pin (threads follow
    # the marks); 4 bounds each statement to four threads. Unpinned costs more
    # CPU and peak memory per statement.
    memory_search_fact_leg_max_threads: Annotated[int, LIVE] = 0

    # ---- timeouts, budgets and caches ----
    # Seconds the query-spelling lookup may take; it reads every row of the namespace it
    # may read (100-300 ms server-side for 40k rows) and overlaps the embed on a cache
    # miss.
    memory_search_spelling_lookup_timeout_s: Annotated[float, LIVE] = Field(default=0.4, gt=0)


# ===== MemoryWebhook ==========================================================


def parse_backoff_steps(raw: str) -> tuple[float, ...]:
    """`memory_webhook_backoff_steps_seconds` as seconds, in order; ValueError when a
    step is not a number over 0 or there is no step."""
    steps = tuple(float(step) for step in raw.split(",") if step.strip())
    if not steps or not all(step > 0 for step in steps):
        raise ValueError("memory webhook backoff steps: one or more numbers over 0 seconds")
    return steps


def _backoff_steps(raw: str) -> str:
    parse_backoff_steps(raw)
    return raw


# Startup and the settings file refuse an unusable backoff schedule.
BackoffSteps = AfterValidator(_backoff_steps)


class MemoryWebhookSettings(BaseModel):
    """Memory webhooks: their delivery worker and the URLs they may reach."""

    # Deliver the signed memory-change events of loom.memory_webhook_outbox to
    # each namespace's registered webhooks (the memory_webhook_deliver worker).
    memory_webhook_deliver_enabled: bool = True
    # Seconds between memory webhook delivery passes while events are going out.
    memory_webhook_deliver_interval_seconds: int = Field(default=5, ge=1)
    # Seconds between memory webhook delivery passes that found nothing due.
    memory_webhook_deliver_idle_interval_seconds: int = Field(default=30, ge=1)
    # Seconds a memory webhook delivery pass may run before the supervisor abandons it
    # (at least 30).
    memory_webhook_deliver_max_pass_seconds: float = Field(default=120.0, ge=30)
    # Seconds before the pass deadline a memory webhook delivery pass stops its POSTs, kept
    # for the insert after them; less than memory_webhook_deliver_max_pass_seconds.
    memory_webhook_deliver_reserve_seconds: float = Field(default=20.0, gt=0)
    # Seconds past memory_webhook_deliver_max_pass_seconds a memory webhook event a pass
    # leased stays passed over by other passes, so a crashed pass's events come back after it.
    memory_webhook_lease_margin_seconds: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Leases (max pass plus lease margin each) after a webhook.remove in which delivery
    # passes still mark the removed memory webhook's events dead.
    memory_webhook_removal_window_leases: Annotated[int, LIVE] = Field(default=10, ge=1)
    # Due memory webhook events one delivery pass POSTs at most; the rest go next pass.
    memory_webhook_max_posts_per_pass: Annotated[int, LIVE] = Field(default=40, ge=1)
    # Events of one memory webhook a delivery pass POSTs at most, so every webhook with
    # work gets its turn.
    memory_webhook_posts_per_webhook: Annotated[int, LIVE] = Field(default=10, ge=1)
    # Memory webhook POSTs one delivery pass has in flight at once (one per webhook at most).
    memory_webhook_deliver_concurrency: Annotated[int, LIVE] = Field(default=4, ge=1)
    # Seconds a memory webhook receiver has to answer a POST (the name lookup, the connection
    # and the status line) before it counts as failed; also the registration's lookup limit.
    memory_webhook_ack_timeout_seconds: Annotated[float, LIVE] = Field(default=10.0, gt=0)
    # Comma-separated seconds a failed memory webhook event waits before each retry, one
    # step per failed attempt; every step over 0.
    memory_webhook_backoff_steps_seconds: Annotated[str, LIVE, BackoffSteps] = "5,30,120,600,1800"
    # Seconds a failed memory webhook event waits at most: caps each backoff step and is
    # the wait after the last.
    memory_webhook_max_backoff_seconds: Annotated[float, LIVE] = Field(default=3600.0, gt=0)
    # POSTs per memory webhook event before it is marked dead; the 2.75-day delivery
    # deadline marks it dead sooner whatever this is.
    memory_webhook_max_attempts: Annotated[int, LIVE] = Field(default=60, ge=1)
    # Failed POSTs in a row after which the delivery worker passes a memory webhook over
    # for a cooldown (kept per process; a 2xx resets it).
    memory_webhook_failures_before_cooldown: Annotated[int, LIVE] = Field(default=5, ge=1)
    # Seconds of a memory webhook's first cooldown; each one after it doubles, up to
    # memory_webhook_max_cooldown_seconds.
    memory_webhook_cooldown_seconds: Annotated[int, LIVE] = Field(default=60, ge=1)
    # Seconds a memory webhook's cooldown grows to at most.
    memory_webhook_max_cooldown_seconds: Annotated[int, LIVE] = Field(default=3600, ge=1)
    # Seconds between each process's reads of the registered memory webhooks; a webhook
    # registered or removed on another process applies to writes here within it.
    memory_webhook_refresh_seconds: Annotated[float, LIVE] = Field(default=10.0, gt=0)
    # Live memory webhooks a namespace may hold: every memory write inserts one outbox row
    # per webhook that takes its event.
    memory_webhook_max_per_namespace: Annotated[int, LIVE] = Field(default=10, ge=1)
    # True lets a memory webhook use plain http (a local receiver in development);
    # false: https only.
    memory_webhook_allow_http: bool = False
    # Comma-separated hostnames or IP literals, no port (exact, any case; an IDN in either
    # spelling) a memory webhook may reach although they resolve to a private, loopback or
    # link-local address; "" = none.
    memory_webhook_private_hosts: str = ""

    @model_validator(mode="after")
    def _check_delivery_reserve(self) -> MemoryWebhookSettings:
        """A reserve at or over the pass deadline leaves a pass no time to POST."""
        if (
            self.memory_webhook_deliver_reserve_seconds
            >= self.memory_webhook_deliver_max_pass_seconds
        ):
            raise ValueError(
                "LOOM_MEMORY_WEBHOOK_DELIVER_RESERVE_SECONDS must be less than "
                "LOOM_MEMORY_WEBHOOK_DELIVER_MAX_PASS_SECONDS"
            )
        return self

    def memory_webhook_private_host_set(self) -> frozenset[str]:
        return ascii_host_set(self.memory_webhook_private_hosts)

    @model_validator(mode="after")
    def _check_webhook_private_hosts(self) -> MemoryWebhookSettings:
        refusal = host_list_refusal(
            "LOOM_MEMORY_WEBHOOK_PRIVATE_HOSTS", self.memory_webhook_private_hosts
        )
        if refusal:
            raise ValueError(refusal)
        return self


# ===== Model ==================================================================

# `local:<alias>` embedding models: alias -> (Hugging Face id, the width it emits).
# `local` alone is DEFAULT_LOCAL_EMBEDDING_MODEL.
LOCAL_EMBEDDING_MODELS: dict[str, tuple[str, int]] = {
    "bge-small": ("BAAI/bge-small-en-v1.5", 384),
    "bge-base": ("BAAI/bge-base-en-v1.5", 768),
    "minilm": ("sentence-transformers/all-MiniLM-L6-v2", 384),
}
DEFAULT_LOCAL_EMBEDDING_MODEL = "bge-small"


class ModelSettings(BaseModel):
    """Models: which model does each job.

    Every setting that names a model lives here, one per job, so a deployment changes
    a job's model with one env var (`LOOM_<FIELD>`). The provider is inferred from the
    name (`claude*` → Anthropic, `gpt*` / o-series → OpenAI), so any job can run on
    either provider when its API key is set. "" means the job uses `llm_model`.
    """

    # --- defaults ---------------------------------------------------------------
    # The deployment's LLM: memory extraction, rerank and every job whose own model
    # setting (the other *_model settings) is left "". Empty takes the resolved
    # provider's default model (gpt-6-luna / claude-haiku-4-5 / fake-echo-1).
    llm_model: str = ""
    # The assistant agent's model, which LibreChat runs on its own Anthropic key and Loom
    # writes into the agent definition at boot. "" = the definition's own
    # (integrations/librechat/agent.yaml), not llm_model.
    assistant_model: str = ""
    # Model to answer with when a client's provider is still unavailable after
    # its retries (429 / 5xx / transport / timeout) or rejected the request
    # (400 / 413 / 422). Counted in `loom.llm.fallback`. 401 / 403 / 404 are
    # configuration and never fall back. "" = fail the op.
    llm_fallback_model: str = ""
    # Embeddings for memories, queries and code chunks. fake-ngram-3072 needs no
    # network or key; production uses text-embedding-3-* (OPENAI_API_KEY). Change
    # it together with `embedding_dim`; a new embedding space needs a full re-embed.
    embedding_model: str = "fake-ngram-3072"
    # Where `local` models live (relative: under the working directory), one directory per
    # alias and revision; a missing file is fetched there. The image sets it by env.
    local_embedding_model_dir: str = "var/models"

    # --- memory -----------------------------------------------------------------
    # memory.ask synthesis (the strong tier when the cascade is on).
    memory_ask_synth_model: str = ""
    # memory.ask cheap tier. "" = off: every ask uses memory_ask_synth_model. When
    # set, server/memory/domain/synth_policy.py routes easy asks cheap and hard ones
    # strong; an empty, uncited or hedging cheap answer escalates to strong once.
    # route_tier / route_reason / synth_model on the output record the choice.
    memory_ask_synth_model_cheap: str = ""
    # memory.ask mid tier. "" = none. With a model set, strong-routed asks whose
    # reason is in memory_ask_synth_mid_routes go mid; an empty, uncited or hedging
    # mid answer escalates to strong once.
    memory_ask_mid_model: str = ""
    # The durability check on a coding capture: one batched call per capture per
    # listed model (CSV) judges each fact durable or not and folds same-claim
    # paraphrases and replaced states. A fact is dropped only when every model says
    # drop; folds come from the first. Fails open. "" turns it off.
    memory_extract_verify_models: str = "claude-opus-5-5"
    # The conflict resolver's judge (memory_conflict_resolver_enabled). Its confidence
    # thresholds were set on this model; a cheaper one is only safe for clears.
    conflict_resolver_model: str = "gpt-6.1-sol"
    # Model of the profile fold, which consolidates an identity's memories into its
    # profile; "" = `llm_model`.
    profile_consolidation_model: str = ""
    # Model of the structured profile builder, which fills a user's profile in the
    # namespace's schema; "" = `llm_model`.
    structured_profile_model: str = ""
    # Reflection distillation: the outcome labeller and the contrast writer.
    reflection_distill_model: str = ""
    # The code-link `revise` judge: does a memory's claim still hold for its code's
    # new text.
    code_link_revise_model: str = ""
    # Session briefs; without its provider's key the deployment's LLM writes them.
    session_brief_llm_model: str = "gpt-6-luna"

    # --- code -------------------------------------------------------------------
    # code.ask synthesis. Answers are memoized fleet-wide, so a stronger model costs
    # once per unique question.
    code_ask_llm_model: str = ""
    # code.search HyDE: writes a hypothetical indexed passage whose embedding runs a
    # second vector list (see code_search_hyde_*). "" = off.
    code_search_hyde_model: str = ""
    # code.search concept expansion: maps a prose, unconstrained question to likely
    # code identifiers — via this model, or `cards` / `cards:<n>` (symbols of the n
    # nearest file cards) — fused as a capped lexical list. "" = off.
    code_search_concept_expand_model: str = ""
    # code.bisect_candidates' one-call rerank of the fused top 20.
    code_bisect_llm_rerank_model: str = "gpt-6-luna"
    # File and module summary cards.
    code_summary_model: str = ""

    # --- tracker ----------------------------------------------------------------
    # tracker.duplicate_check's rerank call.
    tracker_dupcheck_rerank_model: str = "gpt-6-luna"
    # tracker.duplicate_check's judge call.
    tracker_dupcheck_judge_model: str = ""

    # --- observability ----------------------------------------------------------
    # The sampled retrieval judge (retrieval_judge_sample_rate). Deliberately a
    # different model from the ones answering: a same-model judge measures
    # agreement, not quality.
    retrieval_judge_model: str = "gpt-6.1-sol"


# ===== Namespace ==============================================================


class NamespaceSettings(BaseModel):
    """Namespace registration, write marks and bench-namespace GC."""

    # ---- namespace write marks ----
    # loom.namespace_write_marks is an in-process max per (org, namespace,
    # plane), flushed as one INSERT this often.
    namespace_write_marks_flush_interval_s: float = 5.0

    # ---- namespace registration and GC ----
    # A principal's first write into a namespace with no loom.namespaces row
    # inserts one: owner = token subject, `kind = 'bench'` under a bench
    # prefix, `retention_days = 0` (never a TTL). No
    # ALTER.
    namespace_autoregister_enabled: bool = True
    # Op-name prefixes the auto-register hook ignores: ops on a namespace
    # rather than into it, the code plane (registers `kind = 'code'` itself),
    # and the operator planes.
    namespace_autoregister_skip_prefixes: str = (
        "namespace.,code.,admin.,ops.,grants.,policy.,blacklist.,erasure."
    )
    # Comma-separated name prefixes of throwaway harness namespaces: stamped
    # `kind = 'bench'` by `namespace.register` (when no kind is given) and
    # auto-register, and scanned by `namespaces-gc`.
    namespace_bench_prefixes: str = (
        "lme-,mb-,td-,locomo-,kcurve-,wpath-,bench-,ma-verbatim-,mab-,musique-"
    )
    # namespace_gc: `kind = 'bench'` namespaces with live memory and no write for
    # `namespace_gc_idle_hours` are purged by one list-scoped erasure saga per batch, at
    # most `namespace_gc_sagas_per_pass` a pass, kept to loom tables.
    namespace_gc_worker_enabled: bool = True
    # Seconds between namespace GC passes.
    namespace_gc_interval_seconds: int = 3600
    # Hours without a write before a bench namespace is purged.
    namespace_gc_idle_hours: int = 24
    # Namespaces one erasure saga purges, oldest write first (at least 1).
    namespace_gc_batch: int = Field(default=200, ge=1)
    # Erasure sagas one GC pass submits (at least 1).
    namespace_gc_sagas_per_pass: int = Field(default=1, ge=1)

    # ---- timeouts, budgets and caches ----
    # Most live rows the namespace audit's value scan reads (it is O(total value
    # bytes)); above it that check is skipped and says so.
    namespace_audit_index_row_scan_cap: Annotated[int, LIVE] = Field(default=20000, ge=1)
    # Largest value (bytes) namespace.audit, namespace.validate and memory.inspect read
    # to cite a row's content hash; a larger row is cited with none. 0 = cite no hash.
    namespace_report_hash_max_bytes: Annotated[int, LIVE] = Field(default=65536, ge=0, le=2**32 - 1)
    # Most live documents namespace.tune re-chunks in process to judge the chunk size and
    # stride, picked by a hash of their source_uri; the report states the cap.
    namespace_tune_document_sample: Annotated[int, LIVE] = Field(default=50, ge=1, le=1000)
    # Seconds a namespace's relation cardinalities (one or many) are cached for the
    # triple writer.
    namespace_ontology_cardinality_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds a namespace's taxonomy is reused: one is four FINAL scans of the namespace
    # (about 20 MB per open on a large namespace) and changes only when the namespace
    # does.
    namespace_taxonomy_ttl_s: Annotated[float, LIVE] = Field(default=300.0, gt=0)


# ===== Observability ==========================================================


def parse_latency_budgets(raw: str) -> dict[str, int | float]:
    """`latency_budget_overrides` as {op: ms | factor}; ValueError names the op whose
    budget is neither milliseconds nor "<n>x", never the value. An op this build lacks
    is kept: the reader logs it and nothing asks for it."""
    if not raw.strip():
        return {}
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    out: dict[str, int | float] = {}
    for op_name, value in obj.items():
        parsed = parse_budget_value(value)
        if parsed is None:
            raise ValueError(f"{op_name!r}: neither milliseconds nor '<n>x'")
        out[op_name] = parsed
    return out


def _latency_budgets_map(raw: str) -> str:
    parse_latency_budgets(raw)
    return raw


# Startup and the settings file refuse an unusable budget. An op name is not checked
# here: builds N-1 and N read one settings file, and an op one of them lacks must not
# stop it starting.
LatencyBudgetsMap = AfterValidator(_latency_budgets_map)


class ObservabilitySettings(BaseModel):
    """Telemetry, audit, tracing and the ops surface."""

    # Root log level. (Bind host/port are flags of `loom serve`, not settings.)
    log_level: Annotated[Literal["debug", "info", "warning", "error", "critical"], CaseFolded] = (
        "info"
    )
    # "text" (dev) or "json" (production).
    log_format: Annotated[Literal["text", "json"], CaseFolded] = "text"

    # Days of ops the `op_mix` check of ops.health reads: the create/read/update/delete mix
    # over them warns when reads per write exceed `ops_op_mix_reads_per_write_warn`
    # (polling load) or, after `ops_op_mix_min_writes`, the update share falls below
    # `ops_op_mix_update_share_warn` (append-only bank).
    ops_op_mix_window_days: int = 7
    # Reads per write above which the `op_mix` check warns (polling load).
    ops_op_mix_reads_per_write_warn: float = 20.0
    # Share of writes that are updates below which the `op_mix` check warns (append-only
    # bank).
    ops_op_mix_update_share_warn: float = 0.01
    # Writes in the `op_mix` check's window before its update-share warning can fire.
    ops_op_mix_min_writes: int = 500
    # Days the `procedure_plane` check of ops.health reads: it warns when
    # ≥ `ops_procedure_plane_min_packs` packs flowed and none showed a procedure while
    # procedures exist, or when success retention over ≥ `ops_procedure_plane_min_pairs`
    # pairs is below `ops_procedure_plane_retention_warn`.
    ops_procedure_plane_window_days: int = 7
    # Context packs in the `procedure_plane` check's window before "no pack showed a
    # procedure" can warn.
    ops_procedure_plane_min_packs: int = 50
    # Success retention (fraction) below which the `procedure_plane` check warns.
    ops_procedure_plane_retention_warn: float = 0.5
    # Pairs the `procedure_plane` check measures success retention over before its
    # warning can fire.
    ops_procedure_plane_min_pairs: int = 20
    # Periodic re-hash of audit rows; a mismatch writes `audit.tamper_detected`.
    # Per-row detection, not a hash chain (see server/observability/audit.py).
    audit_verifier_enabled: bool = True
    # Seconds between audit verifier passes (row re-hash and checkpoint sealing).
    audit_verifier_interval_seconds: int = 3600
    # Length, seconds, of one audit window. Each closed window is sealed as a
    # chained checkpoint once `audit_checkpoint_grace_seconds` old, and the last
    # `audit_checkpoint_verify_windows`
    # are recounted every pass: a short recount is a deletion, which per-row
    # hashes cannot see.
    audit_checkpoint_window_seconds: int = 3600
    # Seconds after a window closes before it is sealed.
    audit_checkpoint_grace_seconds: int = 600
    # Most recent sealed windows recounted every pass (at least 1).
    audit_checkpoint_verify_windows: int = Field(default=24, ge=1)
    # Most windows one pass seals; a backlog is sealed over later passes.
    audit_checkpoint_max_seal_per_pass: int = 48

    # OTel exporter, for spans and metrics: "ch" writes to `otel_traces` and
    # `otel_metrics_*` in `clickstack_database` (`default` when empty) on the same
    # service, "otlp" ships to otel_exporter_otlp_endpoint (headers: comma-separated
    # K=V), "" = auto (otlp when the endpoint is set, else ch).
    otel_exporter: Annotated[Literal["", "ch", "otlp"], CaseFolded] = ""
    # Seconds between refreshes of the per-namespace code-plane COUNT gauges
    # (one FINAL scan each of code_files, code_chunks, code_symbols and
    # code_edges, on the lease holder). The counts move once an index run, so
    # an hour is enough; each refresh reads every live code row. 0 = every export.
    otel_code_counts_cache_s: int = 3600
    # Seconds between recounts of the code.grep wrong-abstain gauge (a
    # `content` scan per namespace with abstains). 0 = gauge off.
    otel_code_grep_replay_cache_s: int = 3600
    # Max namespaces `loom.memory.agent_labels_24h` names per probe (top N by
    # label total); the rest sum under `loom.namespace = "other"`. 0 = no cap.
    otel_agent_labels_max_namespaces: int = 20
    # Seconds between the state probe's whole-table readings (embed backlog,
    # integrity invariants), taken on the lease holder only. The invariants
    # read 0 and exist to catch drift, so an hour is enough; each reading is
    # one FINAL pass over memory, memory_embeddings and entity_vectors.
    otel_invariants_probe_s: int = 3600
    # Seconds between retention censuses (`loom.retention.*`: per-org counts
    # over 7- and 45-day windows; one FINAL pass over loom.memory on the lease
    # holder). 0 = every export.
    otel_retention_census_s: int = 3600
    # Who takes the whole-table censuses (invariants, code-plane counts,
    # retention, agent labels, storage): "leader" = only the supervisor-lease
    # holder, "process" = every process (costly).
    otel_census_scope: Literal["leader", "process"] = "leader"
    # Metric export cadence, seconds. Spans keep their own 3 s batch delay,
    # which bounds span loss at SIGTERM.
    otel_metrics_export_interval_s: int = 60
    # Delta temporality for counters and histograms (an idle synchronous
    # series writes no row). Off: the dashboards (deploy/hyperdx_dashboards.md)
    # read cumulative rows — flip together with them.
    otel_metrics_delta_temporality: bool = False
    # Write a gauge point only when its value changed or this many seconds
    # after the last write; 0 = every export. The ClickHouse exporter only
    # (LOOM_OTEL_EXPORTER=ch); OTLP exports every point. Off because gauge
    # tiles fill gaps with 0 and would draw a sawtooth.
    otel_gauge_heartbeat_s: int = 0
    # OTLP/HTTP collector base URL (`/v1/traces` and `/v1/metrics` are appended); set with
    # `otel_exporter` = "", it selects the otlp exporter.
    otel_exporter_otlp_endpoint: str = ""
    # Comma-separated K=V headers sent to `otel_exporter_otlp_endpoint`, e.g.
    # `Authorization=Bearer <token>`.
    otel_exporter_otlp_headers: str = ""
    # The OTel resource `service.name` stamped on every span, log and metric.
    otel_service_name: str = "loom"
    # The OTel resource `deployment.environment.name` stamped on every span, log and metric.
    otel_environment: str = "dev"
    # Comma-separated path regexes the FastAPI instrumentation opens no span
    # for (health polls). "" = instrument every path but the sign-in routes
    # (/oauth/), which are always left out: their URLs carry codes.
    otel_excluded_urls: str = "/v1/health"

    # Scales every op's p99 budget (OP_BUDGETS_P99_MS in server/settings/catalog.py,
    # tuned for local CH) at the comparison site; set e.g. 20.0 on Cloud so SLO alerts
    # don't fire on baseline RTT.
    latency_budget_multiplier: float = 1.0
    # JSON map {op: ms | "<n>x"} over the op budgets table (OP_BUDGETS_P99_MS in
    # server/settings/catalog.py): an integer replaces that op's budget, "3x"
    # multiplies it. Checked after the per-consumer overrides and `admin/budget.set`;
    # a value that is neither ms nor "<n>x" stops startup, an op this build lacks is
    # logged and ignored. '' = the table as declared.
    latency_budget_overrides: Annotated[str, JsonObject, LatencyBudgetsMap] = ""
    # JSON map {consumer: {op | "*": ms | "<n>x"}}: an integer is a budget in
    # ms, "3x" multiplies the op's declared budget. Checked after runtime
    # `admin/budget.set` overrides, before per-op ones; malformed = logged once
    # and ignored. '' = none.
    latency_budget_overrides_by_consumer: Annotated[str, JsonObject] = ""

    # One agent_events row per worker pass that spent something or failed
    # (operation worker.<name>.pass, kind='worker'); loom.worker_status records
    # every pass.
    worker_ledger_rows: bool = True

    # Master switch for OTel emission. `init_otel()` also refuses under pytest
    # regardless: server/main.py calls it at import, and a test run must never
    # ship rows into production observability tables.
    otel_enabled: bool = True

    # Langfuse: with both set, OTel spans are forwarded to Langfuse's OTLP
    # endpoint as a second span processor beside the ClickHouse exporter,
    # retrieval scores are posted, and the erasure saga makes real HTTP DELETE
    # calls (empty = log intent only). The token is `pk-lf-…:sk-lf-…`.
    langfuse_url: str = ""
    # Langfuse `pk-lf-…:sk-lf-…` key pair for erasure, scores and the OTLP forward.
    langfuse_admin_token: str = ""
    # Fraction of TRACES forwarded, sampled by trace id so a trace arrives
    # whole. Span volume is dominated by worker embeddings; turn down in
    # production.
    langfuse_sample_rate: float = 1.0
    # Span-name prefixes never forwarded: high-volume infrastructure spans with
    # no LLM work (they belong in ClickStack).
    langfuse_span_denylist: str = "ch.query,ch.insert,GET /v1/health,POST /v1/health"
    # Score-POST circuit breaker: consecutive failures that open it.
    langfuse_circuit_failure_threshold: int = 5
    # Seconds the open breaker waits before a trial call (floor 1).
    langfuse_circuit_cooldown_s: float = Field(default=30.0, ge=1.0)
    # Seconds one Langfuse API call may take: posting a score, and the traces and
    # scores the prompt evaluation reads.
    langfuse_timeout_s: Annotated[float, LIVE] = Field(default=15.0, gt=0)
    # How much LLM prompt and completion text is recorded, by what the prompt holds:
    # "off" records none (the default; consent is given, not withdrawn); "query" only
    # the legs whose prompt is the user's own search text (hyde, query_rewrite,
    # enrichment, …); "all" also the legs carrying retrieved memories or conversation
    # turns (inline_rerank, iterative_judge, extract, session_summary, …).
    # A namespace's `capture_generation_io` overrides it (false ⇒ off, true ⇒
    # all, NULL ⇒ this). It also gates loom.search_trace_log.query. Token
    # counts, cost, latency and quality scores are never gated.
    langfuse_capture_io_level: Annotated[Literal["off", "query", "all"], CaseFolded] = "off"

    # Per-field cap on recorded prompt/completion text; truncation adds an
    # explicit marker.
    langfuse_generation_io_max_chars: int = 2000
    # Fraction of searches sent to an independent LLM judge, written back as a
    # `retrieval_relevance` score (unlike loom's own confidence scores, it
    # shares no state with retrieval). 0.0 = off: it costs money per search;
    # a few percent (e.g. 0.05) suits production.
    retrieval_judge_sample_rate: float = 0.0
    # Secret that `loom export-analytics` hashes org, namespace and agent names
    # under: a random string of 32 or more characters; a new key changes every hash.
    analytics_export_name_key: str = ""

    # ---- timeouts, budgets and caches ----
    # Seconds one agents.connected answer per (org, namespace, window, caller) is
    # reused: the sidebar badge asks every 30 s per tab and the answer is a 24 h scan of
    # agent_events.
    insights_agents_connected_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Most audit rows one verifier pass checks; past it the newest go unchecked and the
    # pass says so.
    audit_verify_max_rows_per_pass: Annotated[int, LIVE] = Field(default=500000, ge=1)


# ===== Provider ===============================================================


def ascii_host(host: str) -> str:
    """`host` as a host list names it: lower-case, no trailing dot, a non-ASCII name as
    its UTS 46 IDNA form (`faß.example` is `xn--fa-hia.example`, a fullwidth name its
    ASCII one). An ASCII name or an address literal is kept as it is. ValueError when
    IDNA cannot encode it."""
    host = host.strip().lower().rstrip(".")
    if host.isascii():
        return host
    return idna.encode(host, uts46=True).decode("ascii").lower().rstrip(".")


def ascii_host_set(hosts: str) -> frozenset[str]:
    """The comma-separated `hosts`, each as `ascii_host` spells it. One IDNA cannot
    encode is refused at load (`host_list_refusal`), and dropped here: no URL's host
    can equal it."""
    out = set()
    for h in hosts.split(","):
        try:
            out.add(ascii_host(h))
        except ValueError:
            continue
    out.discard("")
    return frozenset(out)


def url_host(url: str) -> str:
    """The host httpx sends `url` to, as LOOM_INFERENCE_GATEWAY_HOSTS names it:
    ASCII (an IDN as punycode), lower-case, no trailing dot; "" when httpx
    cannot parse it. httpx's own parser, so the check and the send never read
    a URL differently (several `@`, a backslash)."""
    try:
        return httpx.URL(url.strip()).raw_host.decode("ascii").lower().rstrip(".")
    except (httpx.InvalidURL, UnicodeDecodeError):
        return ""


def _is_http_url(url: str) -> bool:
    """An http(s) base URL with a host that the extractor client's httpx can
    parse, and no query or fragment (the client appends `/v1/...`): a bad one is
    refused at load, not on the first call."""
    try:
        parsed = httpx.URL(url)
        urlsplit(url).port  # noqa: B018 — over 65535 raises, which httpx lets by
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.host)  # `host` decodes IDNA
            and "?" not in url  # a bare `?` or `#` too: httpx reads it as empty
            and "#" not in url
        )
    except (httpx.InvalidURL, ValueError):
        return False


# A DNS name LOOM_INFERENCE_GATEWAY_HOSTS may name: no scheme, port, path or wildcard.
# `_` too: a Compose service name has one, and httpx and urlsplit read it alike.
_GATEWAY_HOST_RE = re.compile(r"[a-z0-9_][a-z0-9_.-]*")


def is_bare_host(host: str) -> bool:
    """A bare DNS name or IPv4 address, or an IPv6 address (its colons are not a port)
    with no zone: no `%`, so no percent-encoding either."""
    if "%" in host:
        return False
    if ":" not in host:
        return bool(_GATEWAY_HOST_RE.fullmatch(host))
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def host_list_refusal(setting: str, hosts: str) -> str:
    """Why an entry of the comma-separated `hosts` is not a bare hostname or IP literal
    as `ascii_host` spells it (a scheme, port, brackets, path, wildcard, or a name IDNA
    cannot encode); "" when every entry is one."""
    for entry in hosts.split(","):
        if not entry.strip():
            continue
        try:
            host = ascii_host(entry)
        except ValueError:
            host = ""
        if not is_bare_host(host):
            return (
                f"{setting}: {entry.strip()[:64]!r} is not a hostname or IP literal (no "
                "scheme, port, brackets, path, wildcard, zone, percent-encoding or a name "
                "IDNA cannot encode)"
            )
    return ""


# The settings the gateway check reads; a dashboard configuration overrides the embedding ones.
_GATEWAY_CHECKED = (
    "openai_base_url",
    "anthropic_base_url",
    "embedding_base_url",
    "inference_token_url",
    "embedding_provider",
    "embedding_model",
)


# The rerank choice that calls Cohere's own API, optionally `:<model>`.
BUILTIN_COHERE = "builtin:cohere"


def is_builtin_cohere(name: str) -> bool:
    """Whether a rerank choice is `builtin:cohere[:<model>]`, which calls Cohere's own API."""
    return name == BUILTIN_COHERE or name.startswith(BUILTIN_COHERE + ":")


# A configured rerank endpoint's name: what a request or a namespace sends instead of a URL.
RERANK_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")


# RFC 9110 token characters: what an HTTP header name may hold.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# What a customer or feature header value carries unencoded (`customer.headers`).
_HEADER_TEXT = re.compile(r"[A-Za-z0-9._~:@-]*")
# A customer or feature name sent as a header value: the same characters, at least one.
_HEADER_VALUE = re.compile(r"[A-Za-z0-9._~:@-]+")
# What an identity header template names, each as `{name}` (server/llm/identity.py).
IDENTITY_PLACEHOLDERS = ("org", "namespace", "agent", "tenant", "subject", "user_id", "email")
IDENTITY_PLACEHOLDER = re.compile(r"\{(" + "|".join(IDENTITY_PLACEHOLDERS) + r")\}")
# The text around them: printable ASCII but braces, which a header carries as it is.
_IDENTITY_TEXT = re.compile(r"[\x20-\x7a|~]*")
# What one entry of LOOM_INFERENCE_IDENTITY_REFUSALS_JSON may match besides its status:
# fields of the answer's error body (`{"error": {...}}`), each matched exactly.
IDENTITY_REFUSAL_FIELDS = ("code", "type", "message")


@functools.lru_cache(maxsize=4)
def _identity_headers(raw: str) -> tuple[tuple[str, str], ...]:
    """LOOM_INFERENCE_IDENTITY_HEADERS_JSON decoded once per value: every model call
    reads it, several times."""
    if not raw:
        return ()
    pairs = json_map_items(raw, "LOOM_INFERENCE_IDENTITY_HEADERS_JSON", strings_only=True)
    return tuple((name, template.strip()) for name, template in pairs)


@functools.lru_cache(maxsize=4)
def _identity_refusals(raw: str) -> dict[str, tuple[dict[str, Any], ...]]:
    """LOOM_INFERENCE_IDENTITY_REFUSALS_JSON decoded and checked, once per value: every
    gateway answer to a model call is matched against it."""
    parsed = json.loads(raw) if raw.strip() else {}
    if not isinstance(parsed, dict) or set(parsed) - {"refused", "wait"}:
        raise ValueError("LOOM_INFERENCE_IDENTITY_REFUSALS_JSON keys are 'refused' and 'wait'")
    out: dict[str, tuple[dict[str, Any], ...]] = {}
    for kind, entries in parsed.items():
        if not isinstance(entries, list):
            raise ValueError(f"LOOM_INFERENCE_IDENTITY_REFUSALS_JSON: {kind!r} is a list")
        for e in entries:
            status = e.get("status") if isinstance(e, dict) else None
            fields = set(e) - {"status"} if isinstance(e, dict) else set()
            if (
                not isinstance(status, int)
                or isinstance(status, bool)
                or not 400 <= status <= 599
                or not fields
                or fields - set(IDENTITY_REFUSAL_FIELDS)
                or not all(isinstance(e[f], str) and e[f] for f in fields)
            ):
                raise ValueError(
                    f"LOOM_INFERENCE_IDENTITY_REFUSALS_JSON: each {kind!r} entry is "
                    '{"status": 400..599} and at least one of "code", "type", "message"'
                )
        out[kind] = tuple(entries)
    return out


# Hop-by-hop headers (RFC 9110 §7.6.1): a proxy drops them, so no identity rides one.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
# Headers the model-call senders set themselves, or the HTTP client does: a customer or
# feature header of that name would overwrite them (the provider key, the body's type).
_SENDER_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "x-goog-api-key",
        "anthropic-version",
        "content-type",
        "content-length",
        "host",
    }
)


def parse_llm_max_tokens(raw: str) -> dict[str, int]:
    """`llm_max_tokens_overrides` as {call: tokens}; ValueError names the call whose count
    is not an integer of at least 1, never the count. A call this build lacks is kept:
    the reader logs it and nothing asks for it."""
    if not raw.strip():
        return {}
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    out: dict[str, int] = {}
    for call, tokens in obj.items():
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 1:
            raise ValueError(f"{call!r}: not an integer of at least 1")
        out[call] = tokens
    return out


def _llm_max_tokens_map(raw: str) -> str:
    parse_llm_max_tokens(raw)
    return raw


# Startup, the settings file and `loom settings set` refuse a count below 1. A call name
# is not checked here: builds N-1 and N read one settings file and one stored override.
LlmMaxTokensMap = AfterValidator(_llm_max_tokens_map)


class ProviderSettings(ModelSettings):
    """Embedding and LLM providers.

    It inherits ModelSettings for `llm_model`, which the provider resolution reads and
    fills.
    """

    # Vector width the embedder is asked for and the schema is sized from; the width probed from
    # the embedder at boot wins when they differ. Change it together with `embedding_model`.
    # Unset with a `local:*` model, it is that model's own width: leave it unset for a
    # local model.
    embedding_dim: int = Field(3072, ge=1)

    # "" = infer from `embedding_model` (fake-ngram-* → fake, text-embedding-*
    # → openai, gemini* → gemini, local/local:* → local CPU embedder); or pin
    # "openai", "gemini", "local", "fake".
    # Switching providers changes the embedding space: a full re-embed is
    # required.
    embedding_provider: Annotated[
        Literal["", "openai", "gemini", "compatible", "local", "fake"], CaseFolded
    ] = ""
    # API root for an explicitly selected OpenAI-compatible embedding service,
    # e.g. https://provider.example/v1. Empty is invalid for compatible providers.
    embedding_base_url: str = ""
    # Send the optional dimensions request parameter to a compatible endpoint.
    # False uses its native output width; responses must still match embedding_dim.
    embedding_send_dimensions: bool = False
    # Attempts an embedding call makes on a retryable failure.
    embedding_retry_attempts: Annotated[int, LIVE] = Field(default=3, ge=1)
    # Longest backoff, in seconds, between two attempts of an embedding call.
    embedding_retry_max_wait_s: Annotated[float, LIVE] = Field(default=10.0, ge=0)
    # OpenAI's API root, for chat and embeddings: point it at an
    # OpenAI-compatible inference gateway in front of OpenAI (its /v1 root).
    openai_base_url: str = "https://api.openai.com/v1"
    # Anthropic's API root (the Messages API is <root>/v1/messages): point it at
    # a gateway in front of Anthropic. Off the default, the key also goes as a
    # bearer, the scheme gateways take.
    anthropic_base_url: str = "https://api.anthropic.com"
    # Model calls with no provider key: exchange an identity token (RFC 8693)
    # at this token endpoint of the gateway, and send the answer as the bearer
    # (server/llm/gateway.py). The gateway must trust the token's issuer. "" =
    # the provider keys.
    inference_token_url: str = ""
    # The workload's OIDC token, as a file its platform rotates (a projected
    # service-account token on Kubernetes). With inference_token_url, this or
    # inference_token_audience.
    inference_workload_token_file: str = ""
    # How inference_workload_token_file reaches the gateway: "exchange" (RFC 8693, at
    # inference_token_url) or "bearer" (the file itself, read again at most every
    # minute and after a 401; requires the gateway).
    inference_workload_token_mode: Annotated[Literal["exchange", "bearer"], CaseFolded] = "exchange"
    # With inference_token_url and no workload token: the audience of the identity token Loom
    # signs (issuer <https public_base_url>/v1, newest RS256 key); never jwt_audience.
    inference_token_audience: str = ""
    # The scope sent with the inference gateway token exchange; "" sends none.
    inference_token_scope: str = ""
    # Seconds one token exchange with the inference gateway may take.
    inference_token_exchange_timeout_s: float = Field(default=10.0, gt=0)
    # Seconds a provider check (the dashboard's Models & keys validate and save) waits for
    # its probe call before it reports `timeout`.
    provider_check_timeout_s: Annotated[float, LIVE] = Field(default=30.0, gt=0)
    # True: model calls and memory content go only to inference_gateway_hosts;
    # on whenever a workload token is sent (inference_token_url, or the bearer mode).
    inference_gateway_required: bool = False
    # Comma-separated hostnames (exact, any case) of the gateway; required, and
    # the only hosts sent to, while the gateway is required.
    inference_gateway_hosts: str = ""
    # Header naming the customer (the org) on every model call, for a gateway that
    # meters by it; set, a call naming no customer is refused (server/llm/customer.py).
    inference_customer_header: str = ""
    # Header naming the feature that made the call (hyde, memory.search, worker.embedding, ...),
    # with inference_customer_header only; "" sends none.
    inference_feature_header: str = ""
    # The customer named on calls made for the deployment itself (startup probe, the
    # recalibration worker's fixed pairs, provider checks), which belong to no org.
    inference_system_customer: str = "loom"
    # True (a cell serving several customers): refuse to start without
    # inference_customer_header, so no model call goes out unattributed.
    inference_customer_required: bool = False
    # How the customer header names an org, as the gateway knows it: `{org}` is the
    # org id, around it letters, digits and . _ ~ : @ - only ("{org}" = the id as is).
    inference_customer_format: str = "{org}"
    # JSON object from Loom's feature names (hyde, memory.search, worker.embedding, ...)
    # to the gateway's (letters, digits, . _ ~ : @ -); "*" for any other; "" = Loom's.
    inference_feature_map_json: Annotated[str, JsonObject] = ""
    # JSON object {header: template} every model call carries, naming whom it is for:
    # {org} {namespace} {subject} {user_id}, {agent} (of a workload trust's agent token),
    # {tenant} (that trust's inference_tenant_claim) and {email} (a person's). A header
    # whose template renders empty is not sent, and those naming {agent} or {tenant} go
    # together or not at all; set, a call naming no org is refused. "" sends none
    # (server/llm/identity.py).
    inference_identity_headers_json: Annotated[str, JsonObject] = ""
    # The workload trust claim {tenant} names: the value the trust requires of it, exactly.
    inference_tenant_claim: str = "tenant_id"
    # JSON object of the gateway answers that hold one identity, not the provider:
    # {"refused": [...], "wait": [...]}, each entry {"status": <int>} and at least one of
    # the error body's exact "code", "type" or "message". A refused identity's job work
    # waits inference_refusal_cooldown_seconds, a waiting one the answer's Retry-After.
    # The default: an agent over its budget (by code, and by type and message where the
    # Anthropic-shaped answer carries no code), denied, or named incompletely or
    # malformed; too many of its requests in progress waits. Never list an answer the
    # gateway also gives for its own failure (a credential it cannot fetch, no default
    # agent): health would stop seeing it. Read only with identity headers set
    # (server/llm/gateway.py).
    inference_identity_refusals_json: Annotated[str, JsonObject] = (
        '{"refused": [{"status": 429, "code": "budget_exceeded"}, '
        '{"status": 429, "type": "rate_limit_error", "message": "budget exceeded"}, '
        '{"status": 403, "code": "agent_denied"}, '
        '{"status": 400, "code": "identity_incomplete"}, '
        '{"status": 400, "code": "identity_malformed"}], '
        '"wait": [{"status": 429, "code": "agent_request_limit"}]}'
    )
    # Seconds a background job leaves the work of an identity the gateway refused (an
    # answer listed under "refused") before it calls for it again; 0 calls again at once.
    inference_refusal_cooldown_seconds: float = Field(600.0, ge=0)
    # Seconds between each process's reads of the orgs whose model calls are
    # switched off (loom.org_model_calls) and of the operators' extraction
    # policies (loom.extraction_policies); a change reaches every process within it.
    model_calls_policy_refresh_seconds: float = Field(10.0, gt=0)
    # Seconds a process trusts its copy of the org model-call switch, and of the
    # extraction policies, when it cannot read it again; past it, every model call
    # for an org is refused and no extraction runs (fail closed). 15 to 300.
    model_calls_policy_max_stale_seconds: float = Field(60.0, ge=15, le=300)
    # Days a switched-off org's queued requests (the extraction queue) wait, from
    # when the org was switched off, before they are dropped, each with a
    # memory_drops record. 1 to 29: the queue's own 30-day TTL would otherwise
    # remove them first, with no record.
    model_calls_off_max_wait_days: int = Field(7, ge=1, le=29)
    # Waiting requests of a switched-off org one extraction-queue pass drops; the rest
    # go on the next pass.
    model_calls_off_drop_per_pass: int = Field(default=500, ge=1)
    # Shared query-embedding cache in loom.embedding_cache behind the per-process LRU,
    # rows keyed (model, sha1(org, model, version salt, normalised text)); a call outside
    # a request skips both tiers. Only single-text (query) embeds up to
    # `embedding_cache_shared_max_chars` (0 = no cap) use it. Off = process cache only.
    embedding_cache_shared: bool = True
    # Longest query text, characters, that uses the shared cache; 0 = no cap.
    embedding_cache_shared_max_chars: int = 2000
    # Entries the per-process query-embedding LRU holds; 0 = cache nothing.
    embedding_cache_size: int = Field(10_000, ge=0)
    # Seconds one per-process LRU entry lives.
    embedding_cache_ttl_s: float = 3600.0

    # Derive the similarity gates from this embedder's cosine distribution at
    # boot (server/memory/app/embedder_calibration.py). False pins the scalar
    # defaults (tuned for text-embedding-3-small) — re-bench after an embedder
    # change. Fake embedders and probe failures always use the scalars.
    adaptive_thresholds: bool = True
    # Seconds between each replica's re-read of the shared calibration
    # (loom.embedder_calibration), so every replica serves the same gates.
    calibration_refresh_seconds: float = 60.0

    # "openai" | "anthropic" | "fake"; "" = infer from LOOM_LLM_MODEL's name
    # (gpt*/o1*/o3*/o4*/chatgpt* → openai, claude* → anthropic, fake* → fake), else from the key
    # present (OPENAI_API_KEY, then ANTHROPIC_API_KEY), else fake. Resolved once
    # at load; an explicit value is never overridden. Run `make bench-quick`
    # before making anthropic a default.
    llm_provider: str = ""
    # A fake or key-less provider is a `warn` in /v1/health `problems`; true
    # (production) makes it an `error` and `status: degraded`, which preflight
    # and monitors catch. Never changes the HTTP code (503 is ClickHouse-only).
    require_real_providers: bool = False
    # What decided `llm_provider` — "LOOM_LLM_PROVIDER" when
    # explicit, else the evidence ("LOOM_LLM_MODEL=…", "OPENAI_API_KEY is set",
    # "nothing set"). Read through `llm_provider_source()`; not an env var.
    _llm_provider_source: str = PrivateAttr(default="LOOM_LLM_PROVIDER")
    # Fields the env left unset and load filled in (the LLM pair, a local
    # model's width): assigning them marks them set, but they are not the env's.
    _inferred_fields: frozenset[str] = PrivateAttr(default=frozenset())

    @model_validator(mode="after")
    def _resolve_local_embedding_dim(self) -> ProviderSettings:
        """A local model emits its own width, whatever LOOM_EMBEDDING_DIM says, and
        the schema is sized from `embedding_dim`: unset, it takes the model's."""
        if "embedding_dim" in self.model_fields_set:
            return self
        name = self.embedding_model
        if name == "local" or name.startswith("local:"):
            alias = name.partition(":")[2].strip() or DEFAULT_LOCAL_EMBEDDING_MODEL
        elif self.embedding_provider == "local":
            alias = DEFAULT_LOCAL_EMBEDDING_MODEL
        else:
            return self
        if alias in LOCAL_EMBEDDING_MODELS:  # an unknown alias fails when the embedder is built
            self.embedding_dim = LOCAL_EMBEDDING_MODELS[alias][1]
            self._inferred_fields |= {"embedding_dim"}
        return self

    def feature_map(self) -> dict[str, str]:
        """`inference_feature_map_json` parsed: Loom feature -> the gateway's."""
        raw = self.inference_feature_map_json.strip()
        if not raw:
            return {}
        return dict(json_map_items(raw, "LOOM_INFERENCE_FEATURE_MAP_JSON", strings_only=True))

    def identity_refusals(self) -> dict[str, tuple[dict[str, Any], ...]]:
        """`inference_identity_refusals_json` parsed: "refused" and "wait" entries."""
        return _identity_refusals(self.inference_identity_refusals_json)

    def identity_headers(self) -> tuple[tuple[str, str], ...]:
        """`inference_identity_headers_json` parsed: (header, template) pairs, each
        template without its surrounding whitespace."""
        return _identity_headers(self.inference_identity_headers_json.strip())

    @model_validator(mode="after")
    def _check_model_calls_refresh(self) -> ProviderSettings:
        """A process refuses every model call once its copy of the model-call
        switch is older than its stale bound: reading it less often would refuse
        between reads, with nothing wrong."""
        if self.model_calls_policy_refresh_seconds >= self.model_calls_policy_max_stale_seconds:
            raise ValueError(
                "LOOM_MODEL_CALLS_POLICY_REFRESH_SECONDS must be below "
                "LOOM_MODEL_CALLS_POLICY_MAX_STALE_SECONDS"
            )
        return self

    @model_validator(mode="after")
    def _check_customer_headers(self) -> ProviderSettings:
        """Refuses a configuration under which a call could not name its customer
        as the gateway reads it."""
        for field in ("inference_customer_header", "inference_feature_header"):
            name = getattr(self, field)
            if name and not _HEADER_NAME.fullmatch(name):
                raise ValueError(f"LOOM_{field.upper()} must be an HTTP header name, got {name!r}")
            if name.lower() in _SENDER_HEADERS:
                raise ValueError(
                    f"LOOM_{field.upper()} must not be {name!r}, a header the model-call "
                    "senders set themselves"
                )
        customer, feature = self.inference_customer_header, self.inference_feature_header
        if feature and not customer:
            raise ValueError(
                "LOOM_INFERENCE_FEATURE_HEADER needs LOOM_INFERENCE_CUSTOMER_HEADER: "
                "without it no metering header is sent"
            )
        if customer and feature and customer.lower() == feature.lower():
            raise ValueError(
                "LOOM_INFERENCE_FEATURE_HEADER must differ from LOOM_INFERENCE_CUSTOMER_HEADER, "
                "or the feature overwrites the customer"
            )
        if self.inference_customer_required and not customer:
            raise ValueError(
                "LOOM_INFERENCE_CUSTOMER_REQUIRED needs LOOM_INFERENCE_CUSTOMER_HEADER: "
                "the header the gateway meters customers by"
            )
        fmt = self.inference_customer_format
        if "{org}" not in fmt or not _HEADER_TEXT.fullmatch(fmt.replace("{org}", "")):
            raise ValueError(
                "LOOM_INFERENCE_CUSTOMER_FORMAT must contain {org}, and around it only "
                "letters, digits and . _ ~ : @ -, which a header carries as they are"
            )
        if not all(_HEADER_VALUE.fullmatch(v) for v in self.feature_map().values()):
            raise ValueError(
                "LOOM_INFERENCE_FEATURE_MAP_JSON: each gateway feature name is letters, digits "
                "and . _ ~ : @ -, which a header carries as they are"
            )
        if self.inference_customer_header and not _HEADER_VALUE.fullmatch(
            self.inference_system_customer
        ):
            raise ValueError(
                "LOOM_INFERENCE_SYSTEM_CUSTOMER must name a customer while "
                "LOOM_INFERENCE_CUSTOMER_HEADER is set: letters, digits and . _ ~ : @ -"
            )
        return self

    @model_validator(mode="after")
    def _check_identity_headers(self) -> ProviderSettings:
        """Every identity header is a header name, once, that neither a sender nor the
        customer or feature header sets, with a template of known placeholders; they go
        only to the gateway's hosts, so they need them. The refusals table parses."""
        self.identity_refusals()
        if self.identity_headers() and not self.gateway_hosts():
            raise ValueError(
                "LOOM_INFERENCE_IDENTITY_HEADERS_JSON needs LOOM_INFERENCE_GATEWAY_HOSTS: "
                "identity headers go only to the gateway's hosts"
            )
        taken = {h.lower() for h in (self.inference_customer_header, self.inference_feature_header)}
        for name, template in self.identity_headers():
            refused = _SENDER_HEADERS | _HOP_BY_HOP | taken
            if not _HEADER_NAME.fullmatch(name) or name.lower() in refused:
                raise ValueError(
                    f"LOOM_INFERENCE_IDENTITY_HEADERS_JSON: {name[:64]!r} must be an end-to-end "
                    "HTTP header name, once, that no sender and neither metering header sets"
                )
            taken.add(name.lower())
            if not template:
                raise ValueError(
                    f"LOOM_INFERENCE_IDENTITY_HEADERS_JSON: the template of {name!r} is empty"
                )
            if not _IDENTITY_TEXT.fullmatch(IDENTITY_PLACEHOLDER.sub("", template)):
                raise ValueError(
                    f"LOOM_INFERENCE_IDENTITY_HEADERS_JSON: the template of {name!r} may name "
                    "only {org} {namespace} {agent} {tenant} {subject} {user_id} {email}, "
                    "around them printable ASCII"
                )
        return self

    @model_validator(mode="after")
    def _check_gateway(self) -> ProviderSettings:
        """A mistyped API root or a token exchange missing its token fails at
        load, not at the first model call."""
        urls = {
            "openai_base_url": self.openai_base_url,
            "anthropic_base_url": self.anthropic_base_url,
            "inference_token_url": self.inference_token_url,
        }
        for name, value in urls.items():
            parts = urlsplit(value)
            if value and (parts.scheme not in ("https", "http") or not parts.hostname):
                raise ValueError(f"LOOM_{name.upper()} must be an http(s) URL, got {value!r}")
        token = urlsplit(self.inference_token_url)
        if token.scheme == "http" and token.hostname not in ("localhost", "127.0.0.1", "::1"):
            # The identity token goes there.
            raise ValueError("LOOM_INFERENCE_TOKEN_URL must be https")
        if self.inference_token_audience and not self.inference_token_url:
            raise ValueError("LOOM_INFERENCE_TOKEN_AUDIENCE needs LOOM_INFERENCE_TOKEN_URL")
        sources = bool(self.inference_workload_token_file) + bool(self.inference_token_audience)
        if self.inference_token_url and sources != 1:
            raise ValueError(
                "LOOM_INFERENCE_TOKEN_URL needs one of LOOM_INFERENCE_WORKLOAD_TOKEN_FILE (the "
                "workload's token) and LOOM_INFERENCE_TOKEN_AUDIENCE (a token Loom signs)"
            )
        if self.inference_workload_token_mode == "bearer":
            if not self.inference_workload_token_file:
                raise ValueError(
                    "LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer needs "
                    "LOOM_INFERENCE_WORKLOAD_TOKEN_FILE, the token it sends"
                )
            if self.inference_token_url:
                raise ValueError(
                    "LOOM_INFERENCE_WORKLOAD_TOKEN_MODE=bearer sends the workload token as it "
                    "is: LOOM_INFERENCE_TOKEN_URL is for the exchange; unset one of them"
                )
        return self

    @model_validator(mode="after")
    def _resolve_llm_provider(self) -> ProviderSettings:
        """Resolve an unset LLM provider / model once, at load,
        so `settings.llm_provider` is concrete everywhere (client factory,
        /v1/health, `loom doctor`) and the three never disagree."""
        from server.core.llm_provider import DEFAULT_MODEL_BY_PROVIDER, resolve_llm_provider

        # An explicit provider is never overridden, so a typo (`openia`) would
        # load as itself with no model and only fail at the first LLM call.
        explicit = (self.llm_provider or "").strip().lower()
        if explicit and explicit not in DEFAULT_MODEL_BY_PROVIDER:
            raise ValueError(
                f"unknown LOOM_LLM_PROVIDER={explicit!r}; expected one of: "
                f"{', '.join(DEFAULT_MODEL_BY_PROVIDER)} (or leave empty to infer "
                "from LOOM_LLM_MODEL or the API key present)"
            )
        # The pair moves together: an env that names either set both.
        pair = frozenset({"llm_provider", "llm_model"})
        inferred = pair if not (pair & self.model_fields_set) else frozenset()
        resolved = resolve_llm_provider(self.llm_provider, self.llm_model)
        self.llm_provider = resolved.provider
        self.llm_model = resolved.model
        self._llm_provider_source = resolved.source
        self._inferred_fields |= inferred
        return self

    def env_fields_set(self) -> frozenset[str]:
        """The fields this process's env (or constructor) set, without those
        load inferred."""
        return frozenset(self.model_fields_set) - self._inferred_fields

    def llm_provider_inferred(self) -> bool:
        """True when LOOM_LLM_PROVIDER was unset and the provider came from
        the model name or a key — the case `loom doctor`
        and the health problem list call out when the key is also missing."""
        return self._llm_provider_source != "LOOM_LLM_PROVIDER"

    def llm_provider_source(self) -> str:
        """The evidence behind `llm_provider`, for logs."""
        return self._llm_provider_source

    # Reasoning effort sent to OpenAI models. "" = the lowest the model accepts
    # (learned from its refusals, see server/llm/traits.py); set to pin one value
    # for every call.
    llm_openai_reasoning_effort: str = ""
    # Thinking effort sent to Claude models as `output_config.effort`: "low" |
    # "medium" | "high"; "" = send none (the model's own default). A model that
    # does not take it refuses it once and is not sent it again.
    llm_anthropic_effort: str = ""
    # Most output tokens a call adds over the caller's cap for a model's hidden
    # reasoning. The reserve per model is learned from the reasoning tokens its
    # replies report (twice the most it has spent) and capped here.
    llm_hidden_reasoning_reserve_max: int = Field(16384, ge=0)
    # Seconds an LLM call waits for its reply before the client retries. Generous
    # on purpose: it only catches a hung connection, so a slow model or a long
    # output never hits it. Calls that must be fast set their own, shorter
    # deadline (HyDE, search budgets).
    llm_request_timeout_s: float = Field(300.0, gt=0)
    # Attempts an LLM call makes on a retryable failure (a 429, a 5xx, a dropped
    # connection) before the fallback model takes over.
    llm_retry_attempts: Annotated[int, LIVE] = Field(default=4, ge=1)
    # Longest backoff, in seconds, between two attempts of an LLM call.
    llm_retry_max_wait_s: Annotated[float, LIVE] = Field(default=20.0, ge=0)
    # JSON {call: tokens} over LLM_MAX_TOKENS (in server/settings/catalog.py): the most
    # tokens one kind of LLM call may write, keyed by its `llm_stage`. A count below 1 is
    # refused; a call this build lacks is logged and ignored. '' = the table as declared.
    llm_max_tokens_overrides: Annotated[str, JsonObject, LlmMaxTokensMap, LIVE] = ""

    # JSON {class: {"llm_cache": bool, "strong": bool, "mid": bool}}:
    # `llm_cache` overrides the class's cache default (a per-request pin
    # wins); `strong`/`mid` false bars the cascade from that tier. Unlisted =
    # every tier eligible.
    llm_policy_by_traffic_class: Annotated[str, JsonObject] = '{"bench": {"llm_cache": true}}'
    # JSON {name: url}: the rerank endpoints a request's `rerank`, a namespace's
    # `rerank_endpoint`, LOOM_CODE_SEARCH_RERANK_DEFAULT and
    # LOOM_KNOWLEDGE_SEARCH_RERANK_DEFAULT may name. Names are
    # short identifiers, URLs http(s). Loom posts candidate text only to these. A
    # secret: its URLs can carry a credential.
    rerank_endpoints_json: Annotated[str, JsonObject] = "{}"

    def rerank_endpoint_map(self) -> dict[str, str]:
        """The operator's rerank endpoints, name → URL."""
        raw = self.rerank_endpoints_json.strip()
        if not raw:
            return {}
        return dict(json_map_items(raw, "rerank_endpoints_json"))

    def gateway_required(self) -> bool:
        """Whether model calls may go only to the gateway's hosts."""
        return bool(self.gateway_required_by())

    def gateway_hosts(self) -> frozenset[str]:
        """The hosts of inference_gateway_hosts, as `url_host` spells them."""
        return ascii_host_set(self.inference_gateway_hosts)

    def off_gateway(self, url: str) -> str:
        """While the gateway is required, the host of a `url` it does not list
        ("(none)" for a URL with no host); "" when the send may go."""
        if not self.gateway_required():
            return ""
        host = url_host(url)
        return "" if host in self.gateway_hosts() else host or "(none)"

    def gateway_refusal(self, changes: Mapping[str, Any] | None = None) -> str:
        """Why these settings, with `changes` (a dashboard configuration) over
        them, would send a model call off the gateway; "" when none would."""
        why = self.gateway_required_by()
        if not why:
            return ""
        if not self.gateway_hosts():
            return f"{why} needs LOOM_INFERENCE_GATEWAY_HOSTS, the hosts model calls may go to"
        saved = dict(changes or {})
        v = self.model_dump(include=set(_GATEWAY_CHECKED)) | saved
        # Both API roots count, a provider unused included: a model named per
        # request may be either provider's.
        for field, what in (
            ("openai_base_url", "LOOM_OPENAI_BASE_URL"),
            ("anthropic_base_url", "LOOM_ANTHROPIC_BASE_URL"),
            ("embedding_base_url", "LOOM_EMBEDDING_BASE_URL"),
        ):
            host = self.off_gateway(v[field]) if v[field] else ""
            if host:
                if field in saved:
                    what = "the saved embedding base URL"
                return f"{why} needs {what} at a host in LOOM_INFERENCE_GATEWAY_HOSTS, not {host}"
        host = self.off_gateway(v["inference_token_url"]) if v["inference_token_url"] else ""
        if host:
            return (
                f"LOOM_INFERENCE_GATEWAY_HOSTS does not list {host}, the host of "
                "LOOM_INFERENCE_TOKEN_URL: the token exchange goes to the gateway too"
            )
        for name, url in self.rerank_endpoint_map().items():
            if self.off_gateway(url):
                # The entry, never its URL: it can carry a credential.
                return (
                    f"{why} refuses LOOM_RERANK_ENDPOINTS_JSON entry {name!r}: not a gateway host"
                )
        provider: str = v["embedding_provider"]
        if not provider:
            try:
                provider = inferred_provider(v["embedding_model"])
            except ValueError:
                provider = ""  # an unknown model is refused where the embedder is built
        if provider == "gemini":
            # The Gemini embedder calls Google's own API, never a gateway.
            which = (
                "the saved Gemini embedder"
                if "embedding_provider" in saved
                else "the Gemini embedder"
            )
            return f"{why} refuses {which}: embed through the gateway"
        return ""

    def gateway_required_by(self) -> str:
        """The setting that requires the gateway, for an error to name; "" when none does."""
        if self.inference_token_url:
            return "LOOM_INFERENCE_TOKEN_URL"
        if self.inference_workload_token_mode == "bearer":
            return "LOOM_INFERENCE_WORKLOAD_TOKEN_MODE"
        return "LOOM_INFERENCE_GATEWAY_REQUIRED" if self.inference_gateway_required else ""

    @model_validator(mode="after")
    def _check_gateway_hosts(self) -> ProviderSettings:
        """Every gateway host is a bare hostname, and with the gateway required
        every address Loom sends model calls to is one of them (the senders
        refuse any other too, on every redirect hop)."""
        refusal = host_list_refusal("LOOM_INFERENCE_GATEWAY_HOSTS", self.inference_gateway_hosts)
        if refusal:
            raise ValueError(refusal)
        refusal = self.gateway_refusal()
        if refusal:
            raise ValueError(refusal)
        return self

    @model_validator(mode="after")
    def _check_rerank_endpoints(self) -> ProviderSettings:
        """Every name is a short identifier and every URL http(s) with a host. The
        error names the entry, never the URL (it can carry a credential)."""
        for name, url in self.rerank_endpoint_map().items():
            if not RERANK_NAME_RE.fullmatch(name) or name == "none":
                raise ValueError(
                    f"LOOM_RERANK_ENDPOINTS_JSON: {name[:64]!r} is not a valid name "
                    "(letters, digits, '_' or '-', at most 64, not 'none')"
                )
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError(f"LOOM_RERANK_ENDPOINTS_JSON: {name!r} is not an http(s) URL")
        return self

    # Skip the rerank when the vector top hit's cosine is ≥ this (strong
    # matches rank reliably; the rerank costs ~500 ms). Fallback when the
    # adaptive gate has no probe.
    llm_rerank_skip_above: float = 0.62

    # Recovery worker: retries failed embeds and reconciles orphan
    # scope='shared' rows (the write path embeds inline).
    embedding_worker_enabled: bool = True
    # Seconds between recovery worker passes while there is work.
    embedding_worker_interval_seconds: int = 5
    # Sleep after an idle embedding recovery pass (nothing to embed);
    # `embedding_worker_interval_seconds` resumes once a pass writes a row.
    embedding_worker_idle_interval_seconds: int = 30
    # Seconds between orphan reconciliation's full memory × embedding anti-joins; other
    # passes check only memories of the last `embedding_worker_orphan_recent_seconds`.
    # 0 = full join every pass.
    embedding_worker_orphan_full_scan_seconds: int = 1800
    # Age window, seconds, of the memories a non-full orphan pass checks.
    embedding_worker_orphan_recent_seconds: int = 900
    # The table the LLM response cache reads and writes; an isolated server whose boot
    # migrations are off points this at its own scratch mirror.
    llm_response_cache_table: str = "loom.llm_response_cache"


# ===== RateLimit ==============================================================


class RateLimitSettings(BaseModel):
    """Rate limits: per-actor and per-traffic-class buckets, shared across web workers."""

    # Per-actor rate-limit bucket, keyed (namespace, token subject), as a
    # fraction of the namespace limit. 1.0 = no actor bucket; 0.6 leaves a
    # second credential budget while one sweeps.
    ratelimit_actor_fraction: float = 1.0
    # Share rate buckets across one server's web workers via a memory-mapped
    # file, so a keep-alive client pinned to one worker sees the full cap.
    # auto = when web_workers > 1; on = always; off = per-process buckets
    # divided by replicas × workers.
    ratelimit_shared: Literal["auto", "on", "off"] = "auto"
    # Bucket file; "" = one per server in the runtime dir (server/core/runtime_files.py).
    # The file is 0600 and never opened through a symlink. Set it for two servers
    # sharing one cap.
    ratelimit_shared_path: str = ""
    # Table slots (32 bytes each). Every process of a server must agree; a
    # mismatch falls back to per-process buckets and shows in /v1/health.
    ratelimit_shared_slots: int = Field(default=16384, ge=1)
    # JSON {class: fraction}: a traffic class carved into its own bucket of the
    # base cap × fraction, so a bench sweep never spends production's budget.
    # Class = token `traffic_class` claim, else namespace row, else request
    # hint, else interactive. Absent class = 1.0 (base bucket).
    ratelimit_class_fractions: Annotated[str, JsonObject] = '{"bench": 0.5}'
    # Requests one org may have in flight at once, deployment-wide. Each
    # process holds its share (divided by replicas × web workers, at least 1);
    # a call over it gets 429 RATE_LIMITED. 0 = no cap.
    tenant_max_concurrent_requests: int = Field(default=256, ge=0)


# ===== Runtime ================================================================


class RuntimeSettings(BaseModel):
    """Process shape, deployment, event stream and cost budgets."""

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
    # True: reads call no LLM (ask synthesis: 409), skip the spelling-fix and set-expansion
    # embeddings, and rerank only as named, since a reranker is a model call.
    reads_llm_free: bool = False

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
    # True marks this process as the deployment itself: its compose files set
    # LOOM_DEPLOYMENT=1, and every other process sets LOOM_INSTANCE_LABEL instead
    # (e.g. `iso:<worktree>:<port>`, `ci:integration`). A process with neither
    # refuses to start, so every ledger row is attributable.
    deployment: bool = False
    # At most one `budget.violated` audit row per op per this many seconds
    # (the row carries `suppressed_since_last`). 0 = a row per violation.
    budget_violation_audit_interval_s: float = 60.0
    # Violations per window that still write their own row before the rest are
    # folded into the next: a short burst stays visible, a storm costs at most
    # this many inserts a minute per (op, org, namespace).
    budget_violation_audit_burst: int = 5
    # The address the server hands to clients — the client setups a new token
    # comes with, `claude mcp add`, a bot's claim_url, OAuth metadata. Empty =
    # the request's own Host and scheme, which only a loopback bind may rely on:
    # a non-loopback bind_host refuses to start without it.
    public_base_url: str = ""
    # The address Loom is reachable on: `loom serve` and `make server` bind
    # it; in a container (which listens on all its interfaces) it is the
    # address the port is published on. Anything but loopback turns on the
    # startup checks: public_base_url set, and no auth-off, MCP open mode, dev
    # secret or weak root, gateway or end-user key.
    bind_host: str = "127.0.0.1"
    # Comma-separated peer addresses or CIDRs whose X-Forwarded-For/Proto
    # headers uvicorn applies; any other peer's are ignored. "*" trusts all.
    # Required behind a load balancer: at the default every client counts as the
    # balancer's address for the sign-in and sign-up limits.
    trusted_proxies: str = "127.0.0.1"
    # True lets a non-loopback bind start with auth off, MCP open mode, the dev
    # signing secret or a weak root, gateway or end-user key. Only for a port others
    # cannot reach.
    allow_insecure: bool = False

    def workers_mode(self) -> str:
        """`background_workers` as this process runs it: `auto` outside the
        deployment is `off`, so a branch server never runs the deployment's
        workers. `on` is the explicit opt-in there."""
        if self.background_workers == "auto" and not self.deployment:
            return "off"
        return self.background_workers

    # ---- outbound HTTP ----
    # Seconds the shared outbound HTTP clients (server/clients/_http.py: model and
    # embedding APIs, GitHub, issuer discovery, provider checks) wait on a call that sets
    # no timeout of its own. Read when a process builds the clients.
    http_client_timeout_s: float = Field(default=20.0, gt=0)
    # Seconds the shared outbound HTTP clients wait to open a connection.
    http_client_connect_timeout_s: float = Field(default=5.0, gt=0)
    # Connections each shared outbound HTTP client keeps open at most, so one
    # misbehaving downstream cannot exhaust the process's sockets.
    http_client_max_connections: int = Field(default=100, ge=1)
    # Idle connections each shared outbound HTTP client keeps for reuse.
    http_client_max_keepalive_connections: int = Field(default=20, ge=0)

    # ---- timeouts, budgets and caches ----
    # First wait, in seconds, after a worker's pass fails; it doubles per failure up to
    # worker_backoff_cap_s.
    worker_backoff_base_s: Annotated[float, LIVE] = Field(default=1.0, gt=0)
    # Longest wait, in seconds, between a failing worker's passes (the wait doubles from
    # worker_backoff_base_s).
    worker_backoff_cap_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # A pass with a failed leg on a worker whose interval is longer than this retries
    # after this long, doubling with each further such pass, up to the interval: a weekly
    # pass must not leave one namespace's transient failure standing for a week.
    worker_partial_retry_s: Annotated[float, LIVE] = Field(default=3600.0, gt=0)
    # Seconds one readiness check may take; a slower check fails.
    readiness_check_timeout_s: Annotated[float, LIVE] = Field(default=2.0, gt=0)


# ===== Storage ================================================================


class StorageSettings(BaseModel):
    """Storage: document originals, per-table retention and warm-tier moves."""

    # ---- document originals (loom.document_blobs) ----
    # The sweep that deletes originals no live document points at: those of
    # deleted and re-uploaded documents, and of documents that retention
    # (retention_documents_days, a namespace's retention_days) aged out. Their bucket
    # objects go with them; a walk of the bucket deletes any object left.
    document_blob_gc_enabled: bool = True
    # Pages of 1,000 bucket keys the sweep's walk reads a pass, from where the last
    # stopped: one walk takes a pass per this many thousand keys.
    document_blob_gc_object_pages: int = Field(default=20, ge=1)
    # Seconds between document-original GC sweeps.
    document_blob_gc_interval_seconds: int = 3600
    # Seconds an original must have gone unwritten before the sweep may remove
    # it. A write stores its original before the document row that names it, so
    # a younger original may belong to a write still in flight. Erasure ignores
    # it and deletes every original of its scope. With a bucket, the settings refuse
    # one under a minimum: a write's upload budget plus its row insert's retries
    # (906 s at the defaults; a much larger value leaves room for what the minimum
    # does not count), or an unbounded write timeout, read timeout or pool wait.
    document_blob_min_age_seconds: int = 3600
    # Where a new original's bytes go: `clickhouse` (the row's `data`) or `s3` (an
    # object in document_s3_bucket); an original is read from where its row says.
    document_object_store: Literal["clickhouse", "s3"] = "clickhouse"
    # The S3-compatible bucket originals are written to, read from and erased from;
    # required with document_object_store=s3 and while any object is left under the prefix.
    document_s3_bucket: str = ""
    # Key prefix inside document_s3_bucket; each object is
    # `<prefix>/<org>/<namespace>/<sha256>/<write id>`.
    document_s3_prefix: str = "loom/originals"
    # Endpoint of an S3-compatible store; empty is AWS S3 for document_s3_region.
    document_s3_endpoint_url: str = ""
    # Region of document_s3_bucket; empty takes the credential chain's region.
    document_s3_region: str = ""
    # Seconds a request to document_s3_bucket may take to connect; past it the request
    # fails as a store error (503).
    document_s3_connect_timeout_s: float = Field(default=5.0, gt=0)
    # Seconds a request to document_s3_bucket may take to answer (a streamed download,
    # per chunk); past it the request fails as a store error (503).
    document_s3_read_timeout_s: float = Field(default=30.0, gt=0)

    # Opt-in retention, applied as idempotent ALTER TABLE MODIFY TTL by
    # server/storage/retention_overrides.py: a retention_*_days setting deletes a table's
    # rows older than that many days.
    # ---- retention ----
    # Days `loom.audit_log` rows are kept; 0 = forever. A person's Undo of a
    # retirement holds by its record there: one aged out stops holding, and the
    # conflict resolver may judge and retire that pair again.
    retention_audit_log_days: int = 0
    # Days `loom.agent_events` rows are kept; 0 = forever.
    retention_agent_events_days: int = 0
    # Days `loom.memory` rows are kept; 0 = forever.
    retention_memory_days: int = 0
    # Days `loom.memory_embeddings` rows are kept; 0 = forever.
    retention_memory_embeddings_days: int = 0
    # Days `loom.conversations` rows are kept; 0 = forever.
    retention_conversations_days: int = 0
    # Days `loom.chunks` rows are kept; 0 = forever.
    retention_chunks_days: int = 0
    # Days `loom.documents` rows are kept; 0 = forever.
    retention_documents_days: int = 0
    # The supervisor re-applies table TTLs from settings + namespace rows on
    # this timer as metadata changes (materialize_ttl_after_modify = 0): rows
    # already past a new TTL leave at their part's next merge or a manual
    # MATERIALIZE TTL.
    retention_reconcile_enabled: bool = True
    # Seconds between TTL re-apply passes.
    retention_reconcile_interval_seconds: int = 300

    # Opt-in tiering, applied as idempotent ALTER TABLE MODIFY TTL by
    # server/storage/retention_overrides.py: a tiering_*_warm_days setting moves a table's
    # rows to tiering_warm_volume_name after that many days. It must be below the table's
    # retention_*_days when both are set, or the move is a no-op (logged WARN).
    # ---- storage tiering ----
    # Days before `loom.audit_log` rows move to the warm volume; 0 = never.
    tiering_audit_log_warm_days: int = 0
    # Days before `loom.agent_events` rows move to the warm volume; 0 = never.
    tiering_agent_events_warm_days: int = 0
    # Days before `loom.memory` rows move to the warm volume; 0 = never.
    tiering_memory_warm_days: int = 0
    # Days before `loom.memory_embeddings` rows move to the warm volume;
    # 0 = never.
    tiering_memory_embeddings_warm_days: int = 0
    # Days before `loom.conversations` rows move to the warm volume; 0 = never.
    tiering_conversations_warm_days: int = 0
    # Days before `loom.chunks` rows move to the warm volume; 0 = never.
    tiering_chunks_warm_days: int = 0
    # Days before `loom.documents` rows move to the warm volume; 0 = never.
    tiering_documents_warm_days: int = 0

    # Volume named in tiering TTL clauses; must match a volume in the
    # cluster's storage_policies.
    tiering_warm_volume_name: str = "warm"

    # ---- memory authority ----
    # Which store is the authority for live memory: clickhouse = ClickHouse alone;
    # postgres = Postgres holds current content, revisions and delivery jobs, and
    # ClickHouse serves search and history. Recorded at first boot; a replica
    # configured for the other authority refuses to start.
    storage_authority: Literal["clickhouse", "postgres"] = "clickhouse"
    # Postgres connection string (libpq DSN or postgres:// URL) for the postgres
    # authority. "" = none; required when storage_authority is postgres.
    pg_dsn: str = ""
    # Connections the Postgres pool keeps open per process.
    pg_pool_min_size: int = Field(default=2, ge=0)
    # Most connections the Postgres pool opens per process; a request waits for one.
    pg_pool_max_size: int = Field(default=20, ge=1)
    # Milliseconds a Postgres statement may run, and a request may wait for a pooled
    # connection, before it fails.
    pg_statement_timeout_ms: int = Field(default=5000, ge=100)
    # What an acknowledged write must survive: synchronous = a standby in another
    # failure domain has flushed it (boot refuses a server with no synchronous
    # standby); local = the primary's disk only (boot warns).
    pg_durability: Literal["synchronous", "local"] = "synchronous"
    # Passes a writer that reads its heads again makes when a key's head keeps changing
    # between its read and its commit. Past them a write request answers 503
    # ADMISSION_CONTENDED; memory.consolidate and a background writer leave the keys for
    # their next pass.
    pg_admission_rounds: int = Field(default=3, ge=1)
    # Milliseconds a memory commit or a `memory.redact` waits for a lock (its namespace's
    # generation, which an erasure of the scope holds while it runs) before answering a
    # retryable 503, so a long erasure does not hold every pooled connection it blocks; also
    # how long a read that judges a head once it settles (a write whose commit answer was
    # lost, a sensitivity relabel's close of its owed facts, by the sweep or the projection
    # repair) waits on each head's lock.
    pg_commit_lock_timeout_ms: int = Field(default=2000, ge=1)
    # Milliseconds a memory.set or memory.bulk_set keeps re-reaching its verdict while
    # writes commit in its namespace (each round adds only those writes) before answering
    # 503 ADMISSION_CONTENDED: the bound on how far a namespace's admissions may queue.
    pg_admission_deadline_ms: int = Field(default=3000, ge=100)
    # Revisions committed in a write's namespace during its check that its commit
    # reads to clear them; a longer gap sends the write back to its check.
    pg_admission_gap_rows: int = Field(default=200, ge=1)
    # Vectors of this process's recent writes kept to rule out a commit landing
    # in another write's check without reading ClickHouse.
    pg_admission_recent_vectors: int = Field(default=4096, ge=0)
    # Seconds before its lease ends that a publisher stops sending, for clock drift
    # and the time a send takes to leave the process; under Postgres, shorter than both
    # leases (`LOOM_PG_REQUEST_LEASE_SECONDS`, `LOOM_PG_DELIVERY_LEASE_SECONDS`).
    pg_lease_margin_seconds: float = Field(default=1.0, ge=0)
    # Revisions a waiting admission publishes itself for a writer that stopped before
    # publishing them; the delivery worker publishes the rest.
    pg_admission_help_rows: int = Field(default=20, ge=1)
    # Milliseconds an admission waits for earlier commits in its scope to reach
    # search before answering 503 PROJECTION_BEHIND; a diagnostic report waits as long,
    # then reads on and names `projection` in `skipped`.
    pg_projection_wait_ms: int = Field(default=2000, ge=0)
    # Seconds the writing request holds its revisions' delivery; past it, the delivery
    # worker or a waiting admission in the namespace publishes them instead.
    pg_request_lease_seconds: int = Field(default=5, ge=1)
    # Seconds the delivery worker holds a revision's delivery before another may take it;
    # also how long the conflict resolver sets aside a row whose flagged peer is not yet
    # in ClickHouse.
    pg_delivery_lease_seconds: int = Field(default=30, ge=5)
    # Background delivery of committed revisions a request did not publish.
    pg_delivery_enabled: bool = True
    # Seconds between delivery passes while there is work.
    pg_delivery_interval_seconds: int = 2
    # Seconds between delivery passes that found nothing.
    pg_delivery_idle_interval_seconds: int = 10
    # Revisions one delivery pass publishes.
    pg_delivery_batch_rows: int = Field(default=500, ge=1)
    # Memories one Postgres commit of a `namespace.restore` writes; each batch commits
    # within a request's lease (`pg_request_lease_seconds`).
    pg_restore_batch_rows: int = Field(default=500, ge=1)
    # Times a delivery is tried before it is parked: a search document it cannot build
    # is published as a stub for the embedding worker; a memory row ClickHouse keeps
    # refusing is left out of claims and of its namespace's watermark, and counted in
    # /v1/health?include=postgres (`postgres.parked_deliveries`). An audit row waiting
    # in the outbox (a row an erasure moved there, or one a claim deferred because the
    # holder it replaced may still insert it) is parked the same way
    # (`postgres.parked_audits`). An unavailable ClickHouse counts no attempt.
    pg_delivery_max_attempts: int = Field(default=10, ge=1)
    # Hours an erased memory's id is kept as deletion evidence, refusing a write built
    # from a head read before the erasure (longer than any request holds a head); a
    # `namespace.restore` into its namespace leaves the memory out meanwhile.
    pg_erasure_evidence_hours: int = Field(default=24, ge=1)

    # ---- timeouts, budgets and caches ----
    # Longest wait, in seconds, between retries of a failed dashboard-asset publish (the
    # wait doubles from storage_frontend_assets_retry_s).
    storage_frontend_assets_retry_max_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds before a failed dashboard-asset publish round is retried, doubling up to
    # `storage_frontend_assets_retry_max_s`.
    storage_frontend_assets_retry_s: Annotated[float, LIVE] = Field(default=2.0, gt=0)

    @model_validator(mode="after")
    def _check_object_store(self) -> StorageSettings:
        """An S3 store with no bucket would accept writes it cannot keep."""
        if self.document_object_store == "s3" and not self.document_s3_bucket:
            raise ValueError("LOOM_DOCUMENT_OBJECT_STORE=s3 needs LOOM_DOCUMENT_S3_BUCKET")
        if self.document_s3_bucket != self.document_s3_bucket.strip():
            raise ValueError("LOOM_DOCUMENT_S3_BUCKET has leading or trailing whitespace")
        if (
            self.document_s3_prefix.strip("/") != self.document_s3_prefix
            or not self.document_s3_prefix
        ):
            raise ValueError(
                "LOOM_DOCUMENT_S3_PREFIX must be non-empty, with no leading or trailing '/'"
            )
        return self

    @model_validator(mode="after")
    def _check_pg_lease_margin(self) -> StorageSettings:
        """A margin as long as a lease ends every lease before its first send: each write
        would answer PUBLICATION_PENDING and leave all publication to the worker."""
        if self.storage_authority == "postgres" and self.pg_lease_margin_seconds >= min(
            self.pg_request_lease_seconds, self.pg_delivery_lease_seconds
        ):
            raise ValueError(
                "LOOM_PG_LEASE_MARGIN_SECONDS must be smaller than LOOM_PG_REQUEST_LEASE_SECONDS "
                "and LOOM_PG_DELIVERY_LEASE_SECONDS"
            )
        return self


# ===== Task ===================================================================


class TaskSettings(BaseModel):
    """The task inbox workers and what run.complete does to a run's rows."""

    # ---- task inbox ----
    # Lease sweeper: return expired leases to todo, resume deferred tasks at
    # resume_at, escalate stale triage, and put right lease rows a race left out of
    # step with their task.
    task_lease_sweep_enabled: bool = True
    # Seconds between task-lease sweep passes while there is work.
    task_lease_sweep_interval_seconds: int = 60
    # Seconds between task-lease sweep passes that found nothing.
    task_lease_sweep_idle_interval_seconds: int = 300
    # Post one Standard Webhooks-signed notice per new event batch to each
    # inbox's webhook_url; the secret comes from the env var
    # task_inboxes.webhook_secret_ref names (`LOOM_WEBHOOK_SECRET_<ORG>__<NAME>`,
    # one of the inbox org's own).
    task_webhook_deliver_enabled: bool = True
    # Seconds between webhook delivery passes while notices are going out.
    task_webhook_deliver_interval_seconds: int = 5
    # Seconds between webhook delivery passes that found no new event.
    task_webhook_deliver_idle_interval_seconds: int = 30
    # Events one notice carries at most.
    task_webhook_batch_limit: Annotated[int, LIVE] = Field(default=200, ge=1)
    # Seconds between passes of the always-on sweep that closes the runs of
    # idle MCP sessions.
    mcp_session_run_sweep_interval_seconds: int = Field(default=60, ge=1)

    # ---- run completion ----
    # What run.complete does to the rows the run wrote when the call does not
    # say: keep = nothing; demote = scale confidence by the expiry demote
    # factor now; expire = expires_at now + memory_run_complete_expire_days
    # with on_expire=demote. Pinned, verified, promoted, locked, episodic and
    # reflection rows are always kept, and so is everything an MCP session's
    # implicit run wrote when the transport closes it.
    memory_run_complete_retain: Literal["keep", "demote", "expire"] = "keep"
    # Days until expiry for `memory_run_complete_retain = expire`.
    memory_run_complete_expire_days: int = 7


# ===== Tracker ================================================================


class TrackerSettings(BaseModel):
    """The tracker plane: records, duplicate checks, bisect."""

    # --- tracker record, read live -------------------------
    # Per-statement `max_execution_time` on the remote, seconds; the client's receive
    # timeout is this plus 5 s.
    tracker_live_timeout_s: float = 1.5
    # Caller-side budget per read, ms: past it the read is `tracker_unavailable` and
    # counts against the breaker.
    tracker_live_budget_ms: int = 300
    # Process cache TTL for live rows, seconds (upstream refreshes every 5–10 min); an
    # empty result is kept at most 60 s.
    tracker_live_cache_ttl_s: float = 300.0
    # Kill switch: off = every tracker read answers `tracker_unavailable`.
    tracker_live_serve: bool = True
    # Budget of the bisect candidate read, which scans a release range's diffs, ms: a
    # triage step, not an interactive one.
    tracker_live_bisect_budget_ms: int = 6000
    # Budget of the text-index reads (similar bugs, threads, changelog, PRs by
    # path, area escalations / incidents by term), ms; above the 300 ms interactive
    # budget.
    tracker_live_text_budget_ms: int = 900
    # Consecutive failures of a tracker source's credential that open its breaker.
    tracker_circuit_failure_threshold: int = 3
    # Seconds an open breaker stays open before one probe read (at least 1 s).
    tracker_circuit_cooldown_s: float = Field(default=120.0, ge=1.0)
    # --- tracker org and code-namespace bindings ------------------------------
    # The loom org the tracker namespaces belong to; empty (default) = none, so no
    # tracker namespace serves and self-service sources are refused until it is set.
    tracker_org: str = ""
    # Which tracker namespace a code namespace reads its facts from: glob keys over the
    # operator's repositories' namespaces → namespace, e.g. {"code-myrepo*": "tracker-myrepo"};
    # a code.repo.add_branch branch of one reads that repository's links. '' = no QA facts.
    tracker_code_namespace_bindings_json: Annotated[str, JsonObject] = ""
    # --- tracker sources and the record contract ------------------------------
    # Every tracker source and the namespaces that read them:
    # {"sources": {"<name>": {"host": "x.clickhouse.cloud", "port": 8443,
    #   "secure": true, "database": "loom_tracker"}},
    #  "namespaces": {"qa-<team>": {"source": "<name>", "repos": ["Org/repo"],
    #   "restricted": false}}}. A namespace naming an unknown source does not serve (one
    # error line, never a guess).
    tracker_sources_json: Annotated[str, JsonObject] = ""
    # Seconds tracker.source_check waits for a source's contract check.
    tracker_source_check_timeout_s: Annotated[float, LIVE] = Field(default=8.0, gt=0)
    # Seconds Loom waits to connect to a tracker source's ClickHouse. Read when a source's
    # client is built, which is kept per credential.
    tracker_source_connect_timeout_s: float = Field(default=5.0, gt=0)
    # The repositories whose numbers name a restricted namespace's escalations and core
    # incidents, as `Org/repo`: {"escalation": "Org/escalations", "incident":
    # "Org/incidents"}. `escalation_context` / `incident_context` read the record
    # `Org/repo#<number>`; a kind with no repository here reads no record by number.
    tracker_restricted_repos_json: Annotated[str, JsonObject] = ""
    # Credentials per tracker source, kept apart from `tracker_sources_json`:
    # {"<name>": {"user": "...", "password": "..."}}.
    tracker_credentials_json: Annotated[str, JsonObject] = ""
    # A source serves only while its schema meets the record contract
    # (docs/features/tracker/record-contract.md), checked through its own credential once per
    # this many seconds (at least 1 s); a failed check retries after 60 s.
    tracker_contract_ttl_s: float = Field(default=3600.0, ge=1.0)
    # --- self-service tracker sources -----------------------------------------
    # Self-service tracker sources: any signed-in org member registers
    # one (`tracker.source.add`); credential and schema are checked before any write.
    # Off = only the operator's map serves; loom.qa_sources rows are
    # ignored.
    tracker_self_service_enabled: bool = True
    # At most this many self-service sources an org may register; a further
    # `tracker.source.add` is refused.
    tracker_self_service_max_sources: int = 10
    # A registered host must resolve to a public address (no loopback, RFC 1918,
    # link-local, CGNAT 100.64/10 or multicast); True lets a dev stack point
    # at a local ClickHouse.
    tracker_self_service_allow_private_hosts: bool = False
    # How often a process re-reads loom.qa_sources (seconds); a write in this
    # process refreshes at once.
    tracker_sources_refresh_seconds: float = 60.0
    # --- similar items (tracker.similar) --------------------------------------
    # Whether tracker.similar's vector leg (over
    # loom.qa_item_embeddings, filled by the `tracker_embed` pass) is in the default
    # leg set; a caller naming `vector` in `legs` runs it regardless. The leg only adds
    # candidates, after the ranked legs (`tracker_similar_agreement_weight` aside).
    tracker_similar_vector_leg: bool = True
    # The agreement boost: a tracker.similar candidate found by a ranked leg AND the
    # vector leg gains the vector leg's RRF contribution times this weight (1.0 = counts
    # like a ranked leg, 0.5 = a tie-breaker) and the ranked set is re-sorted;
    # vector-only candidates stay appended.
    tracker_similar_agreement_weight: float = 1.0
    # The deepest vector-leg rank that may boost a ranked candidate; 0 = any rank.
    tracker_similar_agreement_max_rank: int = 5
    # Ms the vector leg waits for its query-time catch-up (embedding the
    # items changed since the tracker_embed pass's cursor), run beside the cosine scan. Past it the
    # call answers from the table (`query.fresh_pending = true`) and the embedding
    # finishes in the background for the next call.
    tracker_similar_catch_up_budget_ms: int = 300
    # --- the tracker_embed pass --------------------------------------------------
    # Every interval (seconds), for each configured tracker namespace, the issues and
    # PRs open or closed within `tracker_embed_closed_days` and changed since the last
    # pass are embedded (`title + "\n" + left(body, tracker_embed_body_chars)`,
    # `tracker_embed_batch` a call) into loom.qa_item_embeddings; an unchanged text
    # (sha256) is skipped, an item that left the window is tombstoned. Off = nothing is
    # written; the leg serves the table plus its query-time catch-up. The interval
    # bounds only that catch-up, never what a query can find.
    tracker_embed_enabled: bool = True
    # Seconds between `tracker_embed` passes.
    tracker_embed_interval_seconds: float = 1800.0
    # Seconds a tracker-embed pass may run before the supervisor abandons it.
    tracker_embed_max_pass_seconds: float = Field(default=900.0, gt=0)
    # The embedding window for PRs, days: a PR closed longer ago is not embedded
    # and one that leaves the window is tombstoned.
    tracker_embed_closed_days: int = 365
    # The window for ISSUES, days (3650 = every closed issue the record holds):
    # labelled duplicate targets are often closed long before the draft. PRs keep
    # `tracker_embed_closed_days`.
    tracker_embed_issue_closed_days: int = 3650
    # Body characters embedded per item. Must stay under the
    # embedder's 8,191-token input limit (a stack trace runs ~2 chars a token; one
    # oversized text fails the whole batch). A change needs a re-embed
    # (`loom tracker-embed-reset`, then the pass).
    tracker_embed_body_chars: int = 8000
    # Items sent to the embedder in one call (at least 1).
    tracker_embed_batch: int = Field(default=200, ge=1)
    # The nearest read's codes-shortlist over-fetch on
    # loom.qa_item_embeddings (x10's misses are BFloat16 rounding only).
    tracker_embed_fetch_multiplier: float = 10.0
    # Seconds the issue corpus's per-token document frequency (for
    # BM25F) is cached per process; the upstream index cannot count it inside a budget.
    tracker_similar_idf_ttl_s: float = 3600.0
    # --- an issue names the PRs that probably fixed it -----------------------
    # A daily pass ranks, for every issue opened in the last `tracker_links_issue_days`, the merged
    # trunk PRs of the `tracker_links_window_days` after it (from a week before) by BM25F over title
    # and body, the issue's stack frames in the PR's diff, the closing mention and the
    # component, and writes each issue's top `tracker_links_per_issue` into
    # loom.qa_link_candidates. Off = nothing is written; `tracker.links` serves the table.
    tracker_links_enabled: bool = True
    # Seconds between `tracker_links` passes.
    tracker_links_interval_seconds: float = 86400.0
    # Seconds a tracker-links pass may run before the supervisor abandons it.
    tracker_links_max_pass_seconds: float = Field(default=3600.0, gt=0)
    # Issues opened within this many days are ranked each pass.
    tracker_links_issue_days: int = 90
    # Days after an issue opened in which a merged trunk PR is a fix candidate.
    tracker_links_window_days: int = 90
    # The top candidates written per issue into loom.qa_link_candidates.
    tracker_links_per_issue: int = 20
    # Score = bm25_norm (the pair's BM25F as a share of the issue's best)
    # + weight_mention × (1 closing keyword, 0.5 bare mention) + 0.05 × component_match.
    # The stack-frames-in-diff features ride the row unscored.
    tracker_links_weight_mention: float = 1.5
    # A consumer (code.backport_gap's issue path, the bisect's fix candidate) takes the
    # top candidate as the fix only above this score AND when a closing keyword names
    # the issue. tracker.links serves every row with its score.
    tracker_links_min_score: float = 1.0
    # --- tracker.duplicate_check -------------------------------------------------
    # A draft's verdict: `duplicate_of` when the top candidate's calibrated confidence
    # reaches this, `related` when any candidate reaches the related threshold, else
    # `none` with no items. Both are probabilities from a logistic fit on labelled
    # duplicate pairs, so they survive a refit; the scale is compressed. Measured `none`
    # on 99% of negatives at 0.25.
    tracker_dupcheck_duplicate_threshold: float = 0.25
    # Calibrated confidence from which tracker.duplicate_check answers `related`
    # (below `tracker_dupcheck_duplicate_threshold`).
    tracker_dupcheck_related_threshold: float = 0.05
    # The fix-in-flight leg reads PRs from the last this many days.
    tracker_dupcheck_fix_path_days: int = 365
    # At most this many PRs the fix-in-flight leg reads per draft.
    tracker_dupcheck_fix_path_prs: int = 20
    # A leg whose read failed for a transient reason (a timeout, a connection
    # error, an upstream 5xx) is run once more inside the op budget; one that still
    # fails leaves the answer `epistemic: degraded` and `leg_status` names it with its
    # reason. The pause before that retry, ms (at most 200).
    tracker_dupcheck_leg_retry_delay_ms: int = Field(default=50, ge=0, le=200)
    # What the retry leaves of the op budget for the steps after the legs
    # (hydrate, fit, judge), ms.
    tracker_dupcheck_leg_retry_reserve_ms: int = 300
    # The shortest retry worth starting, ms: with less left the retry is skipped
    # and `leg_status` says `retry_skipped_budget`.
    tracker_dupcheck_leg_retry_min_ms: int = 200
    # How many tracker namespaces a duplicate check addressed to a code
    # namespace (or its bound tracker namespace) reads when no `namespaces` list is
    # given: every readable tracker record, the bound one first. 1 = the bound one only.
    tracker_dupcheck_fanout_namespaces: int = Field(default=4, ge=1)
    # Bounds the fitted duplicate threshold is clamped into, so one odd fit
    # cannot make every candidate (or none) a duplicate.
    tracker_dupcheck_duplicate_threshold_min: float = 0.15
    # Upper bound of the fitted duplicate threshold.
    tracker_dupcheck_duplicate_threshold_max: float = 0.5
    # The fit (coefficients, curve, holdout) is JSON in loom.code_index_state under the
    # tracker namespace, refitted by the tracker_links pass once older than
    # `tracker_dupcheck_refit_interval_s` by replaying every gold query at its filing
    # time. Off = the stored fit keeps serving;
    # with none stored the op answers `uncalibrated`.
    tracker_dupcheck_refit_enabled: bool = True
    # Seconds after which the tracker_links pass refits the duplicate-check calibration
    # (7 days).
    tracker_dupcheck_refit_interval_s: float = 604800.0
    # Non-duplicate pairs sampled (by hash) per duplicate-check calibration fit.
    tracker_dupcheck_fit_negatives: int = 400
    # The judge stage. On a `related` answer one model call reads
    # the draft against the top `tracker_dupcheck_judge_top_k` issue candidates' title +
    # body head (`tracker_dupcheck_judge_body_chars`) and may promote a same-defect one to
    # `duplicate` (never demotes; a recorded related_to never promotes).
    # `tracker_dupcheck_judge_model` "" = default client.
    # Benched on a labelled duplicate set.
    tracker_dupcheck_judge_enabled: bool = True
    # Issue candidates the judge reads (at least 1).
    tracker_dupcheck_judge_top_k: int = Field(default=5, ge=1)
    # Body characters per candidate the judge reads.
    tracker_dupcheck_judge_body_chars: int = 1500
    # The rerank stage. One model call reads the draft against a pool of
    # each leg's own head (`tracker_dupcheck_rerank_per_leg` rows a leg) plus the issues the tracker
    # graph links to the leading candidates; its same/related picks lead `nearest`, the
    # verdict stays the fit's. Fails open.
    tracker_dupcheck_rerank_enabled: bool = True
    # Rows each leg contributes to the rerank pool; also deepens the rows kept
    # per leg when it exceeds the default head.
    tracker_dupcheck_rerank_per_leg: int = Field(default=50, ge=1)
    # The candidates one rerank call reads (0 = dup_rerank.POOL_CAP,
    # 40); 200 lets a 50-per-leg pool through a fan-out's namespaces.
    tracker_dupcheck_rerank_pool_cap: int = 200
    # At most this many candidates the rerank pool holds for one namespace's
    # legs (after dedup, the legs taking turns).
    tracker_dupcheck_rerank_ns_cap: int = Field(default=60, ge=1)
    # A rerank `same` row at or above this is offered as a lead
    # ("nearest reported: … — may be related, check it"), never as the verdict.
    tracker_dupcheck_lead_min: float = Field(0.9, ge=0.0, le=1.0)
    # Body characters per candidate the rerank call reads.
    tracker_dupcheck_rerank_body_chars: int = 200
    # Seconds the rerank call may take; past it the rerank reports
    # error="timeout" and the verdict stands without it.
    tracker_dupcheck_rerank_timeout_s: float = Field(default=4.0, gt=0.0)
    # --- bisect without a stack frame ----------------------------------------
    # When no frame resolves to a file, code.bisect_candidates(issue_number) scans the
    # release window only for PRs whose diff carries one of this many of the issue's
    # rarest patterns (IDF over the issue corpus). 0 turns the leg off.
    tracker_bisect_scope_patterns: int = 6
    # The issue's component widens the scan to at most this many `src/` directories its
    # PRs touch, each with `tracker_bisect_component_min_lift` × the share every trunk PR
    # has (a hub directory says nothing); the aggregate is cached per component for
    # `tracker_bisect_component_ttl_s`.
    tracker_bisect_component_dirs: int = 8
    # A component directory counts only when its share of the component's PRs
    # is at least this multiple of its share of every trunk PR.
    tracker_bisect_component_min_lift: float = 1.5
    # Seconds the per-component directory aggregate is cached.
    tracker_bisect_component_ttl_s: float = 3600.0
    # --- a failing test is read against its own CI history -------------------
    # The FROM clause of the checks reads; '' = none. A tracker names
    # its own (`checks` table, or its source entry's `checks.from`); this is the
    # deployment-wide fallback. Name the table function, not a view over it: a view's
    # filter runs on the QA side, so a hot test's read hits play's 1 GB result cap.
    # ClickHouse's public CI (the `play` user is public) is
    # "remoteSecure('play.clickhouse.com', 'default', 'checks', 'play', '')".
    tracker_checks_from: str = ""
    # The repository whose CI `tracker_checks_from` is; '' = the namespace's only
    # repository.
    tracker_checks_repo: str = ""
    # Budget of the per-test history read, ms: play scans the whole window by its time
    # key, far past the 300 ms interactive budget.
    tracker_live_checks_budget_ms: int = 8000
    # Window of the test-signal history read, days.
    tracker_test_signal_window_days: int = 30
    # Churn window of the ledger leg, days (at least 1).
    tracker_test_signal_churn_days: int = Field(default=54, ge=1)
    # Runs of a test the history must hold before the test signal gives a verdict.
    tracker_test_signal_min_runs: int = 5
    # Status-change rate between consecutive runs of one PR/check sequence at
    # or above which a test reads as flaky.
    tracker_test_signal_flip_rate: float = 0.02
    # Floor of K, the other failing tests an hour needs to be a storm hour.
    tracker_test_signal_cofail_k: int = 100
    # K, the other failing tests an hour needs to be a storm hour, is at least this
    # multiple of the window's median failing tests per hour.
    tracker_test_signal_cofail_multiple: float = 5.0
    # A storm hour spans at least this many PRs; a test failing on this many
    # PRs in one hour is a shared cause by itself.
    tracker_test_signal_cofail_prs: int = 3
    # Share of the test's failing hours that must be storm hours for
    # `infra_candidate`.
    tracker_test_signal_cofail_share: float = 0.5
    # --- the per-test daily roll-up and the CI prior read from it -------------
    # The tracker_test_daily worker rolls the CI table into loom.qa_test_daily once per
    # day per test, so tracker.test_signal reads a key range here and the remote only
    # for hours no roll-up covers. Off = the tables stop filling; the op falls back to
    # the live read of the whole window.
    tracker_test_daily_enabled: bool = True
    # Pass interval, seconds; today (and yesterday inside the settle lag) is re-rolled
    # every pass as a partial day, so the live tail the op reads is at most one
    # interval plus the current hour.
    tracker_test_daily_interval_seconds: int = 1800
    # Seconds a tracker test-daily pass may run before the supervisor abandons it.
    tracker_test_daily_max_pass_seconds: float = Field(default=900.0, gt=0)
    # How far back a cold worker rolls, in days (the op's largest window is 90).
    tracker_test_daily_backfill_days: int = 30
    # Days one test-daily pass rolls up: the shared `play` user is metered at 100
    # statements of one shape per hour, and the op's live reads share that quota.
    tracker_test_daily_max_days_per_pass: int = 6
    # Seconds paused between remote statements of a pass; 0 = no pause.
    tracker_test_daily_pace_s: float = 5.0
    # Hours after a day ends before its roll is final: a check's row lands
    # when the check finishes, not when it starts.
    tracker_test_daily_lag_hours: float = 3.0
    # Budget of one roll-up statement, ms.
    tracker_test_daily_budget_ms: int = 60000
    # --- the static smell leg -------------------------------------------------
    # The static smell leg reads at most this many chunks of the test's home
    # file(s) from the code plane (flaky-test smell tokens per category, counted).
    tracker_test_signal_smell_chunks: int = 6

    # ---- timeouts, budgets and caches ----
    # Seconds one git call of the patch-history check may run.
    tracker_patch_line_timeout_s: Annotated[float, LIVE] = Field(default=3.0, gt=0)
    # Seconds a tracker source's status snapshot is reused.
    tracker_snapshot_ttl_s: Annotated[float, LIVE] = Field(default=60.0, gt=0)
    # Seconds the tracker source contract check may take when a listing reports it.
    tracker_contract_check_budget_s: Annotated[float, LIVE] = Field(default=3.0, gt=0)
    # Seconds a namespace's stored duplicate-check fit is cached.
    tracker_dupcheck_fit_ttl_s: Annotated[float, LIVE] = Field(default=300.0, gt=0)
    # Seconds the duplicate check's links and release reads may take together (measured
    # p50 about 0.3 s).
    tracker_dupcheck_links_budget_s: Annotated[float, LIVE] = Field(default=1.5, gt=0)
    # Seconds the duplicate check waits for its related-PR search, which starts with the
    # checks, once they finish.
    tracker_dupcheck_pr_wait_s: Annotated[float, LIVE] = Field(default=3.0, gt=0)
    # Seconds a duplicate-check answer is served again to the same caller for the same
    # question.
    tracker_dupcheck_recent_ttl_s: Annotated[float, LIVE] = Field(default=1800.0, gt=0)
    # Seconds a namespace's error-name to code map is cached.
    tracker_error_names_ttl_s: Annotated[float, LIVE] = Field(default=6 * 3600.0, gt=0)
    # Seconds the item-count read behind a tracker source's freshness may take.
    tracker_freshness_budget_s: Annotated[float, LIVE] = Field(default=2.0, gt=0)
    # Seconds the test-signal candidate read may take; it never holds up the answer.
    tracker_test_candidates_timeout_s: Annotated[float, LIVE] = Field(default=3.0, gt=0)


# ===== Operation latency budgets ===============================================
#
# Each operation's p99 latency target in milliseconds, keyed by op name: a request
# that runs longer emits a `budget.violated` event (`memory.budget_violations`). Every
# registered op has an entry (tests/unit/test_op_budgets.py). A deployment tunes them
# without editing this table: `latency_budget_overrides` (one op, in ms or "<n>x"),
# `latency_budget_overrides_by_consumer` (one consumer), `latency_budget_multiplier`
# (all of them), and `POST /v1/admin/budget.set` while Loom runs. Each batched or
# waited async insert an op awaits adds up to `ch_batched_insert_wait_max_ms`; the
# budgets assume its default. A budget is raised where one such wait is a large share
# of it ("+ ... wait" below names those waits); budgets of 400 ms or more absorb it. A wait
# the op's usual call does not make is priced as a shape in OP_SHAPE_BUDGETS_P99_MS. The
# restricted-access audit row's wait sits in the transport's admission gate, before
# the handler time a budget measures.

OP_BUDGETS_P99_MS: dict[str, int] = {
    "admin.users": 300,
    "agents.connected": 120,
    "audit.list": 200,
    "blacklist.add": 50,
    "blacklist.list": 80,
    "blacklist.remove": 80,
    "blacklist.sweep": 5000,
    "calibration.get": 60,
    # Cache hits are ~ms; a synthesis miss pays retrieval + one LLM call.
    "code.ask": 8000,
    # two bounded tracker reads + ≤ 9 ledger reads gathered, then ≤ 8 presence
    # fan-outs over ≤ 8 branch namespaces under a 1.2 s wall; `history` (on request)
    # adds git reads on the mirror under an 8 s wall — the nightly worker's shape,
    # not the default's
    "code.backport_gap": 1500,
    # one range read on loom's own table plus one stats read
    "code.backport_gaps": 500,
    # ≤12 symbol lookups gathered + two bounded tracker reads (300 ms each)
    "code.bisect_candidates": 1500,
    # window refs (≤4 cheap git reads + ≤2 state reads), then one blame
    # (≤400 lines, 5 s cap, cached 10 min) beside one rev-list and two
    # ledger reads, gathered; optional bounded tracker reads
    "code.blame_range": 6000,
    # Probes the live embedder over the whole corpus: slow by design.
    "code.calibrate": 30000,
    "code.callees": 400,
    "code.callers": 400,
    # A task briefing is one code.search plus four bounded reads —
    # 0.7-1.5 s cold; the PR and map paths stay ~0.2 s
    "code.context_pack": 2000,
    "code.context_packs": 300,
    # one ledger point read + one bounded `git diff` on the mirror, then
    # one gathered round: history chunks (cached), ≤4 docs greps, ≤30
    # file reads, two code_files reads, ≤3 census reads (cached), and
    # the wide-caller leg (functions, one edge read, ≤5 coverage greps)
    "code.conventions": 4000,
    "code.coverage": 1500,
    "code.coverage.unwatch": 500,
    # an inbox read, a state read, a few git calls and up to three writes
    "code.coverage.watch": 1500,
    "code.coverage_claim": 1200,
    # <=200 locations -> <=10 batched symbol-table reads
    "code.enclosing": 800,
    "code.examples": 500,
    # one keyed read over code_files (~10k rows per namespace)
    "code.files": 400,
    # one ledger point read beside two cheap git reads, then two bounded
    # tracker reads gathered (900 ms each, cached), then ≤ 10 is-ancestor calls
    "code.first_build": 2500,
    # Priced per mode (the handler notes the shape) with bench/code_grep_handler.py,
    # real calls through handle_code_grep in process, caches warm and cold (the
    # replica-restart worst case). `message` also ranks templates in Python.
    "code.grep": 600,
    "code.history": 300,
    # Two edge hops + test map + PR co-change.
    "code.impact": 1500,
    "code.index_errors": 400,
    "code.index_status": 200,
    "code.map": 300,
    "code.outcome": 150,
    # one symbol-table read served by the path bloom index + as_of
    "code.outline": 300,
    # the verify(reaches) BFS (depth<=4) plus one keyed read of the
    # guards of every hop and, on a miss, one namespace probe
    "code.path_guards": 1500,
    "code.pr_check": 600,
    "code.recent_commits": 800,
    # Fire-and-forget default returns immediately; wait=True is bounded
    # by its own timeout, not this budget.
    "code.refresh": 1000,
    "code.reindex": 500,
    "code.repo.add": 30000,
    "code.repo.add_branch": 30000,
    "code.repo.remote_branches": 30000,
    "code.repo.remove": 30000,
    "code.repo.set_access": 5000,
    "code.repo.set_source": 3000,
    "code.repos": 800,
    # one FINAL point read on the brief's sort-key prefix; a miss builds
    # the brief (one impact read seeding ≤ 20 bounded op reads, gathered)
    # and stores it — the build path is the ingest's, not the read's
    "code.review_brief": 8000,
    "code.scip_ingest": 2000,
    "code.scip_status": 150,
    # Priced per shape with bench/code_search_stage_latency.py (`live --handler`, the
    # whole op in process with the real embedder): a shape's budget is its sampled
    # max x ~1.6. The base covers the exact symbol route and every answer before the
    # legs; `hybrid` runs the legs without a model call, `hybrid_hyde` with one.
    "code.search": 400,
    # two regex greps over src/, one whole-file read (cached 10 min),
    # one excerpt read, one grep count — gathered
    "code.setting_facts": 1200,
    "code.symbol": 250,
    # symbol resolve + two loom reads gathered + items + up to three
    # bounded tracker reads (300 ms each, two gathered)
    "code.symbol_history": 1800,
    # Up to three edge hops + test map; same ceiling as code.impact.
    "code.test_gate": 1500,
    # up to ~30 prefix-scoped grep_files reads run together + the test map
    "code.test_home": 1500,
    # one keyed range read over code_test_facets per (kind, name) prefix
    "code.tests_for": 400,
    # Frame parse is local; one batched symbol resolve per frame set.
    "code.trace": 800,
    # calls/reaches run a bounded BFS (depth<=6, <=2000 nodes).
    "code.verify": 1500,
    # keyed reads: file rows + symbol spans + chunk spans per cited path
    # (batched), items by number, one name lookup per qualified name
    # (<=8 gathered); the optional mirror leg is capped at 20 calls / 5 s
    "code.verify_citations": 1500,
    "collection.create": 80,
    # It runs the purging erasure inline, as `erasure.request` does.
    "collection.delete": 2000,
    "collection.list": 80,
    "collection.move": 80,
    "conflicts.allowlist_list": 80,
    "conflicts.allowlist_remove": 80,
    "conflicts.list": 80,
    "conflicts.propose_revision": 120,
    # Fans ALTER UPDATE across all peers.
    "conflicts.resolve": 500,
    "consolidations.list": 200,
    "context.fetch": 150,
    "context.offload": 300,
    "context.pack": 600,
    # ~30 ms of writes + message and embedding-row waits.
    "conversation.append": 130,
    # Dominated by the LLM round-trip.
    "conversation.compact": 3500,
    "conversation.create": 30,
    # Three bulk ALTER UPDATEs, mutations_sync=2.
    "conversation.delete": 600,
    "conversation.get": 100,
    "conversation.list": 80,
    "conversation.list_saved_messages": 150,
    # + the embedding-row wait of a save that embeds the turn.
    "conversation.save_message": 300,
    "conversation.saved_status": 100,
    "conversation.search": 120,
    "conversation.search_saved_messages": 200,
    # ALTER UPDATE mutation.
    "conversation.set_logging": 200,
    "conversation.threads": 120,
    "conversation.unsave_message": 250,
    "conversation.update": 50,
    "cost.agent_summary": 250,
    "cost.budget_status": 120,
    "cost.record_turns": 150,
    "cost.summary": 200,
    "decision.chain": 500,
    "decision.outcome": 1500,
    "decision.precedents": 800,
    "decision.record": 1500,
    # Multi-table scan; not on hot path.
    "erasure.export": 2000,
    "erasure.list": 150,
    # Multiple ALTER UPDATEs are slow; tolerated for compliance.
    "erasure.request": 2000,
    "erasure.status": 25,
    # A point read, a sync insert when the scope holds a policy, the audit row, then two
    # point reads, or for scope org one point read and the org's namespace policies.
    "extraction_policy.clear": 100,
    # Two point reads, or for scope org one point read and the org's namespace policies.
    "extraction_policy.get": 50,
    # Cap check (a point read and a FINAL count), a sync insert and its audit row, then
    # the reads extraction_policy.get makes.
    "extraction_policy.set": 150,
    "facts.expire_by_adapter": 500,
    "facts.list": 80,
    # One frontier query per hop, at most max_hops of them.
    "facts.path": 250,
    "facts.related": 60,
    # Recursive CTE — heavier than a single hop.
    "facts.traverse": 200,
    "grants.approve": 80,
    "grants.create": 50,
    "grants.list": 80,
    "grants.pending_for_me": 150,
    "grants.revoke": 80,
    "grants.share_agent": 80,
    "grants.unshare_agent": 80,
    "inbox.list": 800,
    "inbox.policy_list": 800,
    "inbox.policy_set": 800,
    "inbox.principals": 1500,
    "inbox.register": 800,
    "insights.catalog": 20,
    "insights.query": 2000,
    "knowledge.add": 50,
    "knowledge.bulk_add": 2000,
    "knowledge.bulk_delete": 500,
    # The live document and the catalog rows, and with sample_rows the first rows of the
    # first 20 datasets: three reads, the last about 20 ms (a key range a dataset).
    "knowledge.dataset_get": 80,
    # A retype reads the stored artifact and types its rows again (up to 100,000 a
    # table, in batches); an approval of the inferred types is four reads and a write.
    "knowledge.dataset_approve": 30000,
    "knowledge.dedup_list": 400,
    "knowledge.dedup_resolve": 400,
    "knowledge.delete": 100,
    # The reads and writes in server/knowledge/app/restore.py: the document's rows by (org,
    # namespace, source_uri) (the chunk and supersession reads range over the namespace's key
    # prefix), the erasure ledger twice (every namespace list of the org's last 90 days is
    # read and parsed, so their count is not bounded), the source's claim, the bucket's two
    # locates, a few lightweight DELETEs and row inserts.
    "knowledge.restore": 200,
    # The configured extractors' capabilities, each cached a minute; a cold read waits at
    # most the listing's 5 s bound, and a service it could not read is not waited for
    # again for a minute.
    "knowledge.formats": 5500,
    "knowledge.get": 30,
    "knowledge.ingest": 60,
    "knowledge.ingest_cancel": 60,
    "knowledge.ingest_status": 40,
    # Thirteen reads: eight of the documents, chunks and retired ingestions (one a join), one
    # of the extractions' coverage, three of the knowledge.ingest jobs (each a GROUP BY
    # source over the namespace's job rows, which reads its cancels, joined to its document
    # rows) and one of the decision rows; then namespace.audit in process (sample_limit 0)
    # for its memory findings.
    "knowledge.inspect": 4000,
    "knowledge.list": 150,
    "knowledge.outline": 80,
    # Each source's live document and each dataset's catalog row read twice (before and
    # after), then one scan of up to four snapshots, held to the caller-shaped ceilings.
    "knowledge.query": 1500,
    "knowledge.reads": 500,
    "knowledge.search": 250,
    # add and rotate: a probe bounded by knowledge_source_probe_timeout_s, up to 5 s
    # waiting for the namespace's lease, a few reads and inserts; check: the probe
    # alone, writing nothing.
    "knowledge.source.add": 30000,
    "knowledge.source.check": 30000,
    "knowledge.source.inspect": 400,
    "knowledge.source.list": 300,
    "knowledge.source.remove": 400,
    "knowledge.source.rotate": 30000,
    "knowledge.source.sync_now": 300,
    # One LLM call per window plus the reduce; bounded by max_windows.
    "knowledge.synthesize": 60000,
    # A read and an insert; an ingested document's edit also takes the source's claim
    # (its lease statements, waiting up to 10 s for another write), reads the document
    # again and its datasets' catalog (FINAL), and inserts their rows at the new version.
    "knowledge.update_metadata": 400,
    "knowledge.upsert": 100,
    "learnings.list": 80,
    "learnings.quality": 120,
    "learnings.review": 150,
    "memory.agents_list": 80,
    # One JSONExtract query over loom.memory FINAL; budget covers large group-by
    # cardinality.
    "memory.aggregate": 250,
    "memory.apply": 3000,
    # Retrieval (memory.search's own budget) plus at most one LLM synthesis
    # call; a retrieval_only / abstained call pays no LLM latency.
    "memory.ask": 8000,
    "memory.assign_category": 60,
    # One audit_log page of the namespace's auto_supersede records, as audit.list reads.
    "memory.auto_retirements": 200,
    "memory.backlinks": 200,
    "memory.brief": 4000,
    "memory.bulk_get": 80,
    # One CH insert + N embeds; the audit-row wait runs beside the graph legs.
    "memory.bulk_set": 300,
    # 6 ALTER UPDATEs against bounded scopes.
    "memory.clear": 600,
    "memory.consolidate": 15000,
    "memory.copy_to": 400,
    "memory.cross_promote": 400,
    # Two keyed reads + one wide mirror read over ≤ `limit` keys; the re-emit
    # is one insert.
    "memory.dedup_collapse": 2000,
    # GROUP BY scan + per-cluster canonical lookup, bounded by `limit`.
    "memory.dedup_list": 300,
    # 1-2 ALTER UPDATEs; mutations_sync=2.
    "memory.dedup_resolve": 400,
    "memory.delete": 30,
    # memory.delete's work plus two committed check inserts and the read after the
    # tombstone: about 2.6x memory.delete's p99 on one server.
    "memory.delete_version": 80,
    "memory.demonstrate": 400,
    "memory.demote": 100,
    "memory.digests": 120,
    "memory.evidence": 150,
    "memory.export_evidence": 800,
    "memory.exposure_summary": 300,
    # Status read, the system.mutations lookup, the model-call switch and one
    # asynchronous update of the observed failed versions.
    "memory.extraction_retry_mine": 150,
    "memory.extraction_status": 80,
    # One read of at most 20 batches by id.
    "memory.extraction_status_mine": 80,
    # One FINAL scan filtered by array predicates;
    # it sits on the PreToolUse path, so the hook bounds it at 3 s anyway.
    "memory.for_file": 150,
    "memory.get": 25,
    "memory.get_by_id": 50,
    "memory.health": 400,
    "memory.history": 80,
    # Up to 200 items in one embed batch, each conflict-checked, then the insert, as for
    # memory.import_curated: a whole import, not a point write.
    "memory.import": 30000,
    # ~50 sections × per-section memory.set; embed-on-write dominates.
    "memory.import_curated": 30000,
    # one embed_many over ≤256 chunks + two synchronous inserts
    "memory.import_evidence": 30000,
    # Eight reads per view (two of them the citation read, by sort key), the ledger and
    # the namespace's retention, plus the guide, the categories, the access check and,
    # for an org admin, the extraction policy: about 27 statements.
    "memory.inspect": 3000,
    # Bounded by namespace size.
    "memory.keys": 2000,
    # Bounded queries: shared point-lookup, audit by request_id, learnings_queue
    # match, and for a bot's rule its supporter count (one candidate's rows).
    "memory.lineage": 150,
    "memory.link": 60,
    # Covers both recency (ORDER BY memory_key) and the wider score path.
    "memory.list": 150,
    "memory.lock": 40,
    # One memory.set (its budget is 200) plus the redirect version, its mirror
    # restamp and the chunk tombstones.
    "memory.move": 400,
    "memory.outcome": 200,
    "memory.outcome_run": 500,
    "memory.outline": 250,
    "memory.owner_report": 800,
    # memory.set's write path (handle_memory_set).
    "memory.pin": 200,
    "memory.promote": 100,
    "memory.proposal_decide": 800,
    "memory.proposals": 400,
    "memory.provenance": 100,
    "memory.recall": 400,
    "memory.record.conflicts": 150,
    "memory.record.history": 150,
    "memory.record.query": 150,
    "memory.record.write": 150,
    # ALTER UPDATE mutation; not on hot path.
    "memory.redact": 500,
    "memory.reindex": 5000,
    "memory.reject": 150,
    # Two point-lookups + one bounded arrayIntersect scan (entities bloom index).
    "memory.related": 120,
    "memory.resolve_orphan_duplicates": 5000,
    "memory.restore": 80,
    # Covers semantic / hybrid / all_mine fan-out (auto_touch is async, no
    # contribution). Reranker hop is EXCLUDED — the operator endpoint owns its
    # own latency. Rerank usage surfaces as `loom.search.assist{kind="rerank"}`
    # (read off `retrieval_work.inline_rerank_used` / `external_rerank_used`);
    # there is no `loom.rerank_ms` metric.
    "memory.search": 200,
    "memory.search_trace": 200,
    "memory.search_trace_log": 200,
    # Version inserts + conflict/dedup reads + audit, outbox and graph-insert waits
    # (the graph legs run concurrently).
    "memory.set": 200,
    # LLM extraction (~7K-token prompt) + bulk_set (~200 ms) + ~4 batched-insert waits.
    "memory.set_from_messages": 2700,
    # Profile + pinned + score-ordered recall (+ optional knowledge.search),
    # each identity-scoped point-lookups.
    "memory.stack": 200,
    "memory.statuses": 80,
    "memory.sweep_expired": 2000,
    "memory.timeline": 120,
    "memory.touch": 40,
    "memory.unlock": 40,
    "memory.unpin": 40,
    # + the audit-row wait each revision makes.
    "memory.update": 170,
    "memory.update_by_id": 130,
    "memory.verify": 80,
    "memory_schema.approve": 120,
    "memory_schema.list": 120,
    "memory_schema.propose": 120,
    "memory_schema.register": 120,
    # One bounded ledger scan on the (org, namespace, agent)
    # sort key, a handful of FINAL-scoped counts over the namespace, three of
    # the audit's shape checks, and two adoption reads — cached an hour.
    "mentor.brief": 3000,
    "mentor.guide": 20,
    # Three summary reads, ten FINAL-scoped aggregates over
    # the namespace and one Python scan of up to 20k (key, clipped value)
    # rows for the hand-rolled index check. Namespace-sized; the ledger
    # reads are bounded by window_seconds.
    "namespace.audit": 3000,
    # The audit, then three reads of the namespace's rows (agents, plain tags, key
    # prefixes), the categories, guide and policy, two reads for the extraction draft
    # (its writers, and which cited rows it wrote), one of the drafted tags' spellings,
    # and three per category draft (`max_category_proposals`, default 5). Then the
    # document drafts: four document reads (counts, writers, the sample, the per-format
    # coverage) and the per-source job read, one artifact read per sampled ingested
    # document (`namespace_tune_document_sample`, default 50), the sample (at most 8 Mi
    # characters of text and artifact in all) cut in process up to six times, and, when a
    # format fails, every extractor's capabilities read at once (5 s in all, cached for a
    # minute).
    "namespace.tune": 8000,
    "namespace.browse": 120,
    "namespace.cancel_request": 80,
    "namespace.delete": 60,
    "namespace.get": 30,
    "namespace.guide_get": 40,
    "namespace.guide_history": 60,
    # A claim on the guide (two KeeperMap writes), one point read, one insert.
    "namespace.guide_set": 250,
    # Nine concurrent namespace-sized aggregates, the widest a GROUP BY over live memory;
    # a collection's members leg reads the cached tree first.
    "namespace.inventory": 800,
    "namespace.join": 80,
    "namespace.leave": 80,
    "namespace.list": 80,
    "namespace.list_categories": 80,
    "namespace.list_mine": 120,
    "namespace.list_ontology": 60,
    "namespace.mark_seen": 50,
    "namespace.register": 30,
    "namespace.register_category": 400,
    "namespace.register_ontology": 80,
    "namespace.restore": 3000,
    # Bounded by max_keys; one FULL OUTER JOIN + batched inserts.
    "namespace.rollback": 10000,
    "namespace.search_categories": 150,
    # Bounded by namespace size.
    "namespace.snapshot": 2000,
    "namespace.summary": 300,
    "namespace.taxonomy": 120,
    # Ten FINAL-scoped aggregates plus two identity joins; namespace-sized,
    # not cluster-sized.
    "namespace.validate": 1500,
    "observe.crystallize": 2000,
    # Hot path: single insert, no LLM/embedding.
    "observe.record": 150,
    "ops.capabilities": 100,
    "ops.health": 500,
    "ops.projection_failures.list": 300,
    "ops.projection_failures.resolve": 500,
    "ops.projection_failures.retry": 3000,
    "org.model_calls": 50,
    "org.set_model_calls": 100,
    "policy.check": 500,
    "policy.decisions": 300,
    "policy.delete": 400,
    "policy.list": 200,
    "policy.set": 400,
    # memory.set's write path (handle_memory_set).
    "procedure.add": 200,
    "procedure.evolve": 200,
    "procedure.fork": 400,
    "procedure.list": 100,
    "procedure.locate": 150,
    "procedure.match": 150,
    "procedure.record_run": 150,
    "procedure.revise": 300,
    "profile.activity": 80,
    # memory.get, then memory.set's write path (handle_memory_set).
    "profile.append": 225,
    "profile.render": 60,
    # memory.get, then memory.set's write path (handle_memory_set).
    "profile.replace": 225,
    # memory.set's write path (handle_memory_set).
    "profile.set": 200,
    # The schema, the profile row and one user's digest aggregate.
    "profile.structured": 60,
    # A point read, then one synchronous insert.
    "profile_schema.clear": 100,
    # One point read by (org, namespace).
    "profile_schema.get": 50,
    # One synchronous insert.
    "profile_schema.set": 100,
    "run.begin": 150,
    "run.complete": 800,
    "run.get": 300,
    "run.grade": 800,
    "run.list": 200,
    # The tracker leg is bounded at 3 s; the memory legs run beside it.
    "search.query": 3500,
    "session.brief_delete": 200,
    "session.brief_get": 200,
    "session.brief_list": 200,
    # The deterministic builder and an agent-written overlay are one read and
    # one insert; the `model` shape is up to `session_brief_max_windows`
    # sequential model calls (about 47 s for four haiku windows).
    "session.brief_save": 800,
    "task.accept": 1500,
    "task.block": 1500,
    "task.cancel": 1500,
    "task.claim": 1500,
    "task.complete": 1500,
    "task.counts": 800,
    "task.create": 1500,
    "task.defer": 1500,
    "task.fail": 1500,
    "task.get": 500,
    "task.heartbeat": 1000,
    "task.history": 800,
    "task.list": 800,
    "task.progress": 1000,
    # A long poll holds up to MAX_WAIT_SECONDS (30 s, models_task.py) by design; the
    # budget is that plus 3 s (test_op_budgets.py ties the two).
    "task.pull": 33000,
    "task.reassign": 2000,
    "task.reopen": 1500,
    "task.resume": 1500,
    "task.unblock": 1500,
    "task.update": 1500,
    # A long poll holds up to MAX_WAIT_SECONDS (30 s, models_task.py) by design; the
    # budget is that plus 3 s (test_op_budgets.py ties the two).
    "task.updates": 33000,
    "task.wont_do": 1500,
    "tracker.answer": 1200,
    "tracker.binding": 50,
    "tracker.component": 300,
    "tracker.contract": 50,
    "tracker.duplicate_check": 2000,
    "tracker.flaky": 300,
    "tracker.item": 300,
    "tracker.links": 300,
    "tracker.release": 300,
    "tracker.similar": 600,
    "tracker.source.add": 30000,
    "tracker.source.check": 10000,
    "tracker.source.inspect": 30000,
    "tracker.source.list": 4000,
    "tracker.source.remove": 30000,
    "tracker.source.rotate": 30000,
    "tracker.source.set_mapping": 30000,
    "tracker.test_signal": 8000,
    "ui.vitals": 50,
    # The namespace's events and an attempts scan, 62 ms at 1M events.
    "webhook.deliveries": 400,
    # One registry read of the namespace.
    "webhook.list": 200,
    # The URL's name lookup, then a registry read and one insert.
    "webhook.register": 800,
    # A registry read of the namespace and one insert.
    "webhook.remove": 400,
    "working_memory.append": 80,
    "working_memory.delete": 40,
    "working_memory.get": 40,
    "working_memory.render": 60,
    "working_memory.replace": 80,
    "working_memory.set": 40,
}

# Budgets for the request shapes an op prices separately: a handler that notes a
# shape (`server.observability.budget_shape`) is held to that shape's budget.
OP_SHAPE_BUDGETS_P99_MS: dict[str, dict[str, int]] = {
    "code.grep": {
        "files": 600,
        "message": 2000,
    },
    "code.search": {
        "hybrid": 1500,
        "hybrid_hyde": 6000,
    },
    # A lookup ranked by the change at hand (near_paths / pr_number) reads the
    # PR's files and the candidates' neighbourhoods; the compact uses block adds a
    # mention leg. Each is priced from its served p99.
    "code.symbol": {
        "ranked": 3000,
        "uses_brief": 1500,
    },
    # The batched-insert shapes below are the base + the default
    # ch_batched_insert_wait_max_ms (50 ms), not a measured p99.
    # "audited": a high-sensitivity row was read, + its audit-row wait.
    # "webhook": a webhook took the event (announce sent outbox rows), + its outbox-row wait.
    # "undo": memory.restore's Undo, + its audit-record wait.
    # "rewrite": deduplication dropped the saved turn, + the rewritten row's wait.
    "conversation.save_message": {"rewrite": 350},
    "memory.bulk_get": {"audited": 130},
    "memory.bulk_set": {"webhook": 350},
    "memory.delete": {"webhook": 80},
    "memory.delete_version": {"webhook": 130},
    "memory.evidence": {"audited": 200},
    "memory.get": {"audited": 75},
    "memory.get_by_id": {"audited": 100},
    "memory.history": {"audited": 130},
    "memory.restore": {"undo": 130, "webhook": 130},
    "memory.update": {"webhook": 220},
    "memory.update_by_id": {"webhook": 180},
    "session.brief_save": {
        "model": 90000,
    },
}


# ===== LLM output budgets ======================================================
#
# The most tokens each LLM call may write, keyed by what the call does (its
# `llm_stage` where it has one, the series the cost ledger reports spend under).
# A call with a fixed cap reads it through `server.settings.budgets.llm_max_tokens`;
# `llm_max_tokens_overrides` changes one while Loom runs. A test fails on a literal
# `max_tokens` at a call site and on a key no call uses. Caps that are settings of
# their own keep their names (`memory_ask_synth_max_tokens` and its ceiling,
# `code_search_query_max_tokens`, `code_search_hyde_max_tokens`,
# `memory_search_identifier_grounding_max_tokens`), and a cap that scales with its
# input stays a formula at its call.

LLM_MAX_TOKENS: dict[str, int] = {
    # code.ask: the synthesized answer; part of its cached answers' version, so a change
    # marks them stale.
    "code_answer_synth": 900,
    # The ceiling a truncated extraction's one retry doubles its budget up to: bounds a
    # pathological transcript while clearing the long-session case.
    "extract_truncation_retry": 8192,
    # session.brief: one window's revision (about 600-1,500 tokens); a cut-off answer is
    # salvaged key by key.
    "session_brief_revision": 3000,
    # The session summary: one to three dated sentences.
    "session_summary": 160,
    # memory.ask: the judge of an answer against its evidence.
    "ask_consistency": 200,
    # memory.ask: the split of a question into sub-questions.
    "ask_decompose": 200,
    # memory.ask deep mode: the pick of memories to open.
    "ask_deep_select": 120,
    # memory.ask: the per-memory check of a question's premise.
    "ask_premise": 200,
    # code.ask: the identifiers a question names, proposed for lookup.
    "code_answer_identifiers": 60,
    # code.bisect_candidates: the rerank over the fused top candidates.
    "code_bisect_rerank": 700,
    # code.ask: the concept leg's expansion of the question into code vocabulary.
    "code_query_expand": 160,
    # the code-summary worker: one file's summary.
    "code_summary": 200,
    # consolidation: the merge of one cluster of memories.
    "consolidate_merge": 256,
    # conversation.compact: the summary of a thread's oldest turns.
    "conversation_compact": 600,
    # loom decisions extract: one window's decisions as JSON (a whole document's, up to
    # memory_decision_extract_window_chars), each claim with its quotes.
    "decision_extract": 12000,
    # loom decisions extract: the support check's verdict on each claim of one window and
    # on each stored decision.
    "decision_support": 8000,
    # the entity extractor: one memory's entities.
    "entity_extract": 160,
    # memory.search HyDE: the hypothetical answer embedded in place of the query.
    "hyde": 128,
    # knowledge.synthesize with datasets: the knowledge.query plan proposed as JSON.
    "knowledge_synthesize_plan": 1200,
    # observe: one transition pair's semantic memory.
    "observe_extract.event": 300,
    # observe: one session's procedure and hint.
    "observe_extract.notes": 900,
    # the period digest: one period's summary.
    "period_digest": 420,
    # profile consolidation: one identity's folded profile.
    "profile_fold": 320,
    # structured profile: the object around its properties, added to answer_tokens(props).
    "structured_profile": 32,
    # the reachability probe a provider configuration change runs.
    "provider_validation": 128,
    # the retrieval judge: one result's relevance grade.
    "retrieval_judge": 120,
    # memory.search: a derived-answer query's aggregate intent.
    "search_derived_intent": 160,
    # memory.search: the judge of whether the excerpts answer the query.
    "search_judge": 128,
    # memory.search: the (subject, verb, object) triples of a query.
    "search_query_triples": 128,
    # memory.search: a verb's synonym set.
    "search_verb_synonyms": 96,
    # session outcomes: the contrast between good and bad sessions.
    "session_contrast": 400,
    # session outcomes: one session's outcome label.
    "session_judge": 160,
    # the conversation titler: one thread's title.
    "title": 30,
    # tracker.duplicate_check: the rerank of the candidate pool.
    "tracker_dup_rerank": 90,
    # typed extraction: which schema fields the source supports (and its one retry).
    "typed_extract_fields": 256,
    # typed extraction: whether the record is present (and its one retry).
    "typed_extract_object": 128,
}
