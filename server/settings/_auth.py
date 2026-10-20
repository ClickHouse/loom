"""Authentication, tokens and identity providers."""

from __future__ import annotations

import functools
import itertools
import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from server.settings._env import DEV_JWT_SECRET, decode_json_or_b64, json_map_items


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
    keys: dict[str, str] = field(default_factory=dict)

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
    # External IdP issuer URL; its `/.well-known/openid-configuration` must name the same issuer.
    oidc_issuer: str = ""
    # Client id registered at the IdP; also the audience each id_token must carry.
    oidc_client_id: str = ""
    # Client secret sent to the IdP token endpoint when exchanging the sign-in code.
    oidc_client_secret: str = ""
    # JIT provisioning: a first OIDC sign-in with no account creates a personal
    # namespace in oidc_jit_org instead of being rejected. Off = fail closed.
    # oidc_jit_email_domains (comma-separated) restricts it; empty admits any
    # verified email the IdP authenticates.
    oidc_jit_provision: bool = False
    # Org that JIT-provisioned accounts are created in; required when `oidc_jit_provision` is on.
    oidc_jit_org: str = ""
    # Comma-separated email domains JIT provisioning admits; empty admits any verified email.
    oidc_jit_email_domains: str = ""
    # Bearer tokens (comma-separated, any one accepted) an identity provider presents to
    # deactivate and reactivate people over SCIM 2.0 (/scim/v2). Empty = off; needs auth on, oidc.
    scim_token: str = ""

    @property
    def oidc_jit_email_domains_set(self) -> set[str]:
        return {
            s.strip().lower() for s in (self.oidc_jit_email_domains or "").split(",") if s.strip()
        }

    # The root secret, and the one secret a deployment carries: it signs tokens
    # as kid `default` and seals every stored credential and every signing key
    # in loom.signing_keys (`loom signing-key rotate` adds those). Required when
    # auth is on. The default is the checked-in DEV secret anyone can mint
    # with: a deployment with auth on refuses to start with it, except the
    # `make dev` stack (`loom doctor` warns everywhere else).
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

    # The `iss` Loom stamps on every token it mints and the `aud` every
    # verified token must carry; a token naming another audience is refused.
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
    # tokens this deployment accepts. Loom's own keys live in loom.signing_keys.
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
    # change reaches other processes through the watermark below, and this
    # bounds one whose watermark row was never written.
    grant_cache_ttl_seconds: float = 10.0
    # Seconds between each process's reads of the per-org grant watermark
    # (loom.acl_versions): a revoke on one replica is enforced on every other
    # within about this long.
    acl_watermark_poll_seconds: float = Field(default=1.0, ge=0.1)
    # Oldest watermark a process still trusts, seconds. Past it (polls keep
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
    # The same for one agent of a trust with an agent claim, so one agent's
    # loop cannot spend the trust's. A trust without agents has only the per-trust limit.
    workload_exchanges_per_agent: int = Field(default=300, ge=1)

    # Browser self-signup: "off" (endpoint 404s) or "open"
    # (/v1/auth/self-signup mints JWTs scoped to the requested namespace).
    self_signup: Literal["off", "open"] = "off"
    # Default org for self-signed tokens.
    self_signup_org: str = "dev"

    # Identity mode; decides how callers authenticate.
    #   single-user    — no auth, one anonymous identity.
    #   trust-network  — self-signup, first-claim-wins.
    #   gateway        — an upstream proxy mints short-lived Loom JWTs with the
    #                    shared HS256 secret; Loom only validates.
    # Empty = inferred from auth_required + self_signup at boot.
    identity_provider: Literal["", "single-user", "trust-network", "gateway"] = ""

    # When set, a dashboard page load redirects (302) here instead of serving the SPA (Loom
    # behind an aggregator). Deployment-specific; never hardcode.
    dashboard_redirect_url: str = ""

    # Comma-separated subjects allowed to send `X-Loom-On-Behalf-Of` (a trusted
    # aggregator passing each end-user's identity under one service token).
    # Empty disables the header.
    trusted_callers: str = ""
    # The services that sign a short-lived token per end user (a chat app in
    # front of Loom): JSON (or base64 JSON) {issuer: {"orgs": [...], "jwks_url":
    # "https://…", "keys": {kid: RS256 public PEM}}}. `orgs` lists the orgs its
    # tokens may name (exact names, or `prefix*`); its keys come from `jwks_url`
    # (fetched every `jwt_end_user_jwks_refresh_seconds`) and/or `keys`. A key
    # here verifies only its own issuer's tokens, and only as an end user
    # (`sub: user:<id>`, one listed `org`, an optional `agent: agent:<id>`),
    # never any other token. Empty: no end-user tokens.
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
    # Commit the `namespace.restricted_access` audit row every admitted read of a
    # restricted namespace writes before answering (inline). True by default: a
    # row written beside the request is lost when the process is killed before it
    # lands. False trades that row for the seconds the awaited insert adds.
    restricted_audit_inline: bool = True
    # Comma-separated origin list. "*" allows all (acceptable when
    # auth_required=True). Empty disables CORS entirely.
    cors_origins: str = ""
    # Request-body cap, bytes, enforced by ASGI middleware before any parser
    # (413 PAYLOAD_TOO_LARGE); uvicorn has no limit of its own. 0 disables.
    max_request_bytes: int = 32 * 1024 * 1024
    # Strict-Transport-Security max-age, seconds, on every answer a route or the web
    # gate sends (browsers ignore it over plain HTTP). 0 sends no header.
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
