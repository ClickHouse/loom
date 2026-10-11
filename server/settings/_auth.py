"""Authentication, tokens and identity providers."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from server.settings._env import DEV_JWT_SECRET, kid_map_items


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
    # The one namespace auth-off mode serves (SPA identity, /me bootstrap,
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
        analytics split in `/me`.

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

    # OAuth 2.1 authorization server for MCP one-click connect: /.well-known
    # discovery + /oauth/{register,authorize,token}. Issues normal loom JWTs,
    # subject-aligned to the signed-in user. Opt-in: a new authz surface.
    # `oauth_issuer` = public base URL for discovery/redirects ("" = per request).
    oauth_enabled: bool = False
    # Public base URL used in OAuth discovery and redirects; empty derives it from each request.
    # Required when `oauth_login_provider` is "oidc" (the IdP callback URL is built from it).
    oauth_issuer: str = ""
    # Human sign-in for /oauth/authorize: "password" (built-in form) or "oidc"
    # (redirect to an external IdP; loom maps the verified email to an account
    # and still mints its own token). "oidc" requires all three oidc_* values.
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

    @property
    def oidc_jit_email_domains_set(self) -> set[str]:
        return {
            s.strip().lower() for s in (self.oidc_jit_email_domains or "").split(",") if s.strip()
        }

    # JWT signing secret; required when auth is on. The default is the
    # checked-in DEV secret anyone can mint with: a deployment with auth on
    # refuses to start with it, except the `make dev` stack (`loom doctor`
    # warns everywhere else). Rotation: `jwt_secrets_json` = kid → secret map,
    # `jwt_active_kid` = signing kid; old tokens verify by their header kid.
    # Empty map = {jwt_active_kid: jwt_secret}. Stored credentials are sealed
    # with jwt_secret even when the map holds the signing keys.
    jwt_secret: str = DEV_JWT_SECRET
    # JSON (or base64 JSON) map kid → secret (PEM private key under RS256) for rotation;
    # empty = the single entry {`jwt_active_kid`: `jwt_secret`}.
    jwt_secrets_json: str = ""
    # Kid that signs new tokens; it must be a key of the secret map (checked at load).
    jwt_active_kid: str = "default"

    def jwt_secret_is_dev_default(self) -> bool:
        """Whether the *signing* secret (the active kid's entry, or
        ``jwt_secret`` when no key map is set) is the checked-in dev default —
        the value anyone who can read this repo can mint tokens with
        (`loom mint-token` warns, `loom doctor` grades WARN)."""
        try:
            active = self.jwt_secret_map().get(self.jwt_active_kid, self.jwt_secret)
        except ValueError:
            active = self.jwt_secret
        return active == DEV_JWT_SECRET

    def jwt_sealing_secret_is_dev_default(self) -> bool:
        """Whether ``jwt_secret``, the key stored credentials are sealed with,
        is the checked-in dev default. With a key map set it no longer signs,
        so the signing check above cannot see it."""
        return self.jwt_secret == DEV_JWT_SECRET

    # The `iss` Loom stamps on every token it mints and the `aud` every
    # verified token must carry; a token naming another audience is refused.
    jwt_issuer: str = "loom"
    # The `aud` claim stamped on minted tokens and required on every verified token.
    jwt_audience: str = "loom-platform"
    # Extra accepted `iss` values, for a gateway minting Loom-shaped JWTs with a
    # key in `jwt_public_keys_json`. Loom's own tokens (iss = jwt_issuer) pass.
    jwt_trusted_issuers: str = ""

    # "HS256" (default) or "RS256": under RS256 the secret-map entries are PEM
    # private keys and verifiers use the public keys (JWKS endpoint), so the
    # public map must hold the signing kid (checked at load).
    jwt_algorithm: Literal["HS256", "RS256"] = "HS256"
    # JSON (or base64 JSON) map kid → PEM public key: this deployment's own
    # keys under RS256, and a gateway's keys whose RS256 tokens it accepts.
    jwt_public_keys_json: str = ""
    # Clock-skew tolerance on iat/exp, seconds; small enough to give replay no
    # useful post-exp window.
    jwt_leeway_seconds: int = 30

    # Seconds the `loom.revoked_tokens` snapshot is cached; verify checks both
    # jti and sub against it, so a revoke takes effect within this TTL.
    jwt_revocation_cache_ttl_seconds: float = 30.0
    # Oldest revocation snapshot a process still trusts, seconds. Past it
    # (refreshes keep failing) every bearer is refused with a retryable 503.
    jwt_revocation_max_stale_seconds: float = 600.0

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

    # When set, GET /me redirects (302) here instead of serving the SPA (Loom
    # behind an aggregator). Deployment-specific; never hardcode.
    dashboard_redirect_url: str = ""

    # Comma-separated subjects allowed to send `X-Loom-On-Behalf-Of` (a trusted
    # aggregator passing each end-user's identity under one service token).
    # Empty disables the header.
    trusted_callers: str = ""

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

    def jwt_secret_map(self) -> dict[str, str]:
        """Resolve the kid → secret/private-key map. Always returns at least one
        entry. Accepts raw JSON or base64-encoded JSON."""
        raw = self.jwt_secrets_json.strip()
        if raw:
            return dict(kid_map_items(raw, "jwt_secrets_json", non_empty=True))
        return {self.jwt_active_kid: self.jwt_secret}

    @model_validator(mode="after")
    def _check_signing_keys(self) -> AuthSettings:
        """The signing kid must be in the secret map (or no token can be
        minted), and under RS256 in the public map too (or every token Loom
        mints is one nothing here accepts). Malformed key JSON fails here, not
        on the first request."""
        if self.jwt_active_kid not in self.jwt_secret_map():
            raise ValueError(
                f"LOOM_JWT_ACTIVE_KID {self.jwt_active_kid!r} is not a kid in LOOM_JWT_SECRETS_JSON"
            )
        public = self.jwt_public_key_map()
        if self.jwt_algorithm == "RS256" and self.jwt_active_kid not in public:
            raise ValueError(
                f"LOOM_JWT_ALGORITHM=RS256 requires a public key for the signing kid "
                f"{self.jwt_active_kid!r} in LOOM_JWT_PUBLIC_KEYS_JSON"
            )
        return self

    def jwt_public_key_map(self) -> dict[str, str]:
        """For RS256 verifiers: kid → PEM public key. Empty when none set."""
        raw = self.jwt_public_keys_json.strip()
        if not raw:
            return {}
        return dict(kid_map_items(raw, "jwt_public_keys_json", non_empty=False))

    # Kill switch for restricted namespaces. True: served to a principal whose
    # token names the namespace AND who holds a live direct grant. False: every
    # read is refused while ingest continues.
    restricted_serve: bool = True
    # Commit the `namespace.restricted_access` audit row every admitted read of a
    # restricted namespace writes before answering (inline) instead of beside
    # the request. Off by default: the awaited insert adds seconds.
    restricted_audit_inline: bool = False
    # Comma-separated origin list. "*" allows all (acceptable when
    # auth_required=True). Empty disables CORS entirely.
    cors_origins: str = ""
    # Request-body cap, bytes, enforced by ASGI middleware before any parser
    # (413 PAYLOAD_TOO_LARGE); uvicorn has no limit of its own. 0 disables.
    max_request_bytes: int = 32 * 1024 * 1024

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
