"""Service-account JWT mint + verify (HS256 shared-secret, RS256 via JWKS).

`exp` is optional: tokens are indefinite unless the minter passes
`ttl_seconds`. Revoke all in-flight tokens by rotating LOOM_JWT_ACTIVE_KID;
revoke one through `loom revoke-token` (server/auth/revocation.py): by its
`jti` (every token minted here carries one), by its `sub`, or by `sub` with an
issued-before cutoff that spares a re-mint.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

import jwt

from server.auth.authz import Role, grant_role
from server.settings import settings


class TokenError(Exception):
    """Raised when a token is malformed, expired, or signed with the wrong key."""


class DevSigningSecretRefused(RuntimeError):
    """A deployment with auth on is signing with the checked-in dev secret."""


# The instance label the local compose stack (`make dev`) sets: the one
# deployment profile allowed to sign with the checked-in dev secret.
DEV_PROFILE_LABEL = "deploy:dev"


def require_real_signing_secret(
    *,
    deployment: bool,
    instance_label: str,
    auth_required: bool,
    dev_secret: bool,
    dev_sealing_secret: bool,
) -> None:
    """Refuse to start a deployment (`LOOM_DEPLOYMENT=1`) that requires auth
    while its signing secret is the checked-in dev default: anyone who has read
    the repository can mint tokens it accepts. Also while the sealing secret
    (`LOOM_JWT_SECRET`, which seals stored credentials even when
    `LOOM_JWT_SECRETS_JSON` signs) is: anyone with the repository and a read of
    the tables opens them. The local compose stack
    (`LOOM_INSTANCE_LABEL=deploy:dev`) is the explicit exception; processes
    that are not the deployment (tests, isolated servers, CLIs) are not
    refused, and `loom doctor` / `/v1/health` still flag them."""
    if not (deployment and auth_required and (dev_secret or dev_sealing_secret)):
        return
    if instance_label.strip() == DEV_PROFILE_LABEL:
        return
    if not dev_secret:
        raise DevSigningSecretRefused(
            "refusing to start: LOOM_JWT_SECRET is the checked-in dev default. Tokens are "
            "signed with LOOM_JWT_SECRETS_JSON, but stored credentials (repository tokens, "
            "tracker-source passwords, provider keys) are sealed with LOOM_JWT_SECRET, so "
            "anyone who reads the tables can open them. Set LOOM_JWT_SECRET to a private "
            "value; credentials sealed with the old one must be entered again."
        )
    raise DevSigningSecretRefused(
        "refusing to start: auth is required and the JWT signing secret is the checked-in "
        "dev default, which anyone can mint accepted tokens with. Set LOOM_JWT_SECRET (or "
        "LOOM_JWT_SECRETS_JSON + LOOM_JWT_ACTIVE_KID) to a private value; the local dev "
        f"stack (LOOM_INSTANCE_LABEL={DEV_PROFILE_LABEL}) is the only deployment exempt."
    )


class RevocationUnavailable(TokenError):
    """The token may be valid, but this process has not loaded the
    revocation list, or its copy is older than
    `jwt_revocation_max_stale_seconds`, so it cannot tell whether the token
    was revoked. Retryable: the list loads on the next refresh."""


@dataclass(frozen=True)
class AuthInfo:
    """The authenticated principal. Passed through to handlers."""

    subject: str  # effective subject (post-impersonation if any)
    org: str
    namespaces: tuple[str, ...]  # readable namespaces (the `namespaces` claim)
    roles: tuple[Role, ...]
    is_service_account: bool
    # `ns_write` claim. Empty => writable wherever readable. Set => the only
    # scope new rows may be written to, so read-down inheritance is safe (a
    # child token reads its parent's namespace but cannot write into it).
    write_namespaces: tuple[str, ...] = ()
    # `ns_roles` claim: per-namespace effective role (namespace -> role name),
    # capped at the minter's live grant at mint time. When set, it is the
    # AUTHORITATIVE per-namespace role — a flat `roles` does not apply to
    # every namespace in the claim (that would let a reader token write).
    # Empty => flat token: fall back to flat `roles` + grant lookup.
    # Priority/order lives in `namespaces` (a list), not here.
    ns_roles: tuple[tuple[str, str], ...] = ()
    # Bearer's real subject under X-Loom-On-Behalf-Of impersonation; forensic
    # trail only (`subject` already holds the impersonated identity).
    impersonated_by: str = ""
    # `read_scope` claim: the widest read this token may make inside a
    # namespace it holds — "" / "namespace" (every row: membership is the read
    # boundary), "own_user" (rows of the user_id the request names), or
    # "own_session" (rows of the session_id the request names). A request may
    # narrow further, never widen (server/identity/domain/read_scope.py).
    read_scope: str = ""
    # `read_user_id` claim: pins the token to ONE end user. Reads are filtered
    # to that user_id whatever the body says; a body naming another user is
    # refused. For the one-token-many-users agent shape, mint one per user.
    read_user_id: str = ""
    # The credential's id for the exposure and audit ledgers, so "which
    # credential read these keys" has an answer finer than `actor`: the `jti`
    # when the token carries one (every token `mint_token` issues does), else
    # sha256(token)[:16] for a token without one or from another issuer.
    # Revocation keys on jti/sub in loom.revoked_tokens, so this id is also
    # the revocation handle.
    token_id: str = ""
    # `consumer` claim: the client this credential speaks for,
    # bound at mint time. Overrides the request's own `consumer` field the way
    # the `traffic_class` claim overrides the request's hint (the claim ranks
    # first); rows it stamps read `consumer_source = claimed`. '' = the
    # request's field, else the subject class, and the row reads self-reported.
    consumer: str = ""
    # `traffic_class` claim: the operator's override of the
    # class every request under this token runs as — production |
    # interactive | bench. '' = the namespace row (then the request's hint,
    # then interactive) decides.
    traffic_class: str = ""
    # Delegation chain (server/auth/delegation.py). `parents`: the token ids
    # of every ancestor token, root first; revoking any of them kills this
    # token. `creator`: the root principal whose live grants and account this
    # token is rechecked against on every request. '' = not a delegated token.
    # `parent_subjects`: the ancestors' `sub`s, in the same order, so revoking
    # an ancestor by subject (a service account's revoke) kills this token too.
    # `parent_iats`: each ancestor's `iat`, aligned with `parent_subjects`, so
    # an issued-before cutoff on an ancestor is judged by that ancestor's age.
    parents: tuple[str, ...] = ()
    parent_subjects: tuple[str, ...] = ()
    parent_iats: tuple[int, ...] = ()
    creator: str = ""
    # `iat` as unix seconds; None for an identity no token backs (dev mode).
    issued_at: int | None = None
    # `exp` as unix seconds; None for a token without one.
    expires_at: int | None = None

    def has_role(self, required: Role) -> bool:
        return any(r.value >= required.value for r in self.roles)

    def has_ns_roles(self) -> bool:
        """True when this token carries an authoritative per-namespace role map
        (a "scoped" token). False for flat/wildcard tokens."""
        return bool(self.ns_roles)

    def effective_role(self, org: str, namespace: str) -> Role | None:
        """Per-namespace role from the `ns_roles` claim, or None when the token
        carries no map for this namespace. Authoritative when present — the
        token's role on `namespace`, already capped to the minter's grant. An
        entry naming anything but a grant role (org_admin, platform_admin, a
        typo) confers nothing: a per-namespace role is at most admin."""
        if self.org != "*" and self.org != org:
            return None
        for ns, role_name in self.ns_roles:
            if ns == namespace:
                return grant_role(role_name)
        return None

    def best_ns_role(self) -> Role | None:
        """The highest role the `ns_roles` claim grants anywhere, or None for a token
        without the map. A deployment-scoped op is gated on this: the op reads no
        namespace, so any namespace's role is the principal's standing."""
        best: Role | None = None
        for _ns, role_name in self.ns_roles:
            r = grant_role(role_name)
            if r is not None and (best is None or r.value > best.value):
                best = r
        return best

    def can_access(self, org: str, namespace: str) -> bool:
        # `org="*"` / `namespaces=["*"]` are opt-in cross-org wildcards.
        if self.org != "*" and self.org != org:
            return False
        return namespace in self.namespaces or "*" in self.namespaces

    def can_write(self, org: str, namespace: str) -> bool:
        """Whether this token may write *new* rows tagged with `namespace`.

        No `ns_write` claim => writable wherever readable. With it, writes are
        restricted to that scope (read does not imply write).
        """
        if not self.write_namespaces:
            return self.can_access(org, namespace)
        if self.org != "*" and self.org != org:
            return False
        return namespace in self.write_namespaces or "*" in self.write_namespaces


# The `read_scope` claim's values, widest first.
READ_SCOPES: tuple[str, ...] = ("namespace", "own_user", "own_session")


def validate_read_scope(value: str) -> str:
    v = (value or "").strip()
    if v not in READ_SCOPES:
        raise ValueError(f"read_scope must be one of {', '.join(READ_SCOPES)}; got {value!r}")
    return v


def mint_token(
    subject: str,
    org: str,
    namespaces: list[str],
    roles: list[str],
    *,
    write_namespaces: list[str] | None = None,
    ns_roles: dict[str, str] | None = None,
    ttl_seconds: int | None = None,
    is_service_account: bool = True,
    secret: str | None = None,
    kid: str | None = None,
    read_scope: str | None = None,
    read_user_id: str | None = None,
    traffic_class: str | None = None,
    consumer: str | None = None,
    jti: str | None = None,
    parents: list[str] | None = None,
    parent_subjects: list[str] | None = None,
    parent_iats: list[int] | None = None,
    creator: str | None = None,
) -> str:
    """Mint a JWT for service accounts, user onboarding, and tests.

    Every token carries a `jti`: `jti` when given —
    the CLI and the admin endpoint generate one with `new_jti()` first so
    they can report it beside the token — else a fresh one here. It is the
    credential's id in the ledgers (`AuthInfo.token_id`) and the handle
    `loom revoke-token --jti` retires; it is not a secret.

    `namespaces` is the readable set (its ORDER is preserved verbatim and is
    the read priority for scoped tokens). `write_namespaces` (the `ns_write`
    claim), if given, restricts writes to that subset. `ns_roles`, if given,
    is the authoritative per-namespace role map (namespace -> role name),
    already capped to the minter's grant; when present it governs
    authorization per namespace instead of the flat `roles`. No `ttl_seconds`
    => no `exp` => indefinite token. The active kid goes in the header so
    verifiers pick the matching secret during signing-key rotation.
    `parents` / `parent_subjects` / `parent_iats` / `creator` are the delegation claims
    `delegation.delegate` computes; a token minted from another token gets
    them from there.
    """
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": subject,
        "org": org,
        "namespaces": namespaces,
        "roles": roles,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "jti": jti or new_jti(),
        "sa": is_service_account,
    }
    if write_namespaces is not None:
        payload["ns_write"] = write_namespaces
    if ns_roles is not None:
        payload["ns_roles"] = ns_roles
    if read_scope:
        payload["read_scope"] = validate_read_scope(read_scope)
    if read_user_id:
        payload["read_user_id"] = str(read_user_id)
    if traffic_class:
        from server.observability.traffic_class import TRAFFIC_CLASSES, normalize_class

        tc = normalize_class(traffic_class)
        if not tc:
            raise TokenError(
                f"traffic_class must be one of {TRAFFIC_CLASSES}, got {traffic_class!r}"
            )
        payload["traffic_class"] = tc
    if consumer:
        from server.observability.consumer import claim_consumer

        c = claim_consumer(consumer)
        if not c:
            raise TokenError(f"consumer must be a printable name, got {consumer!r}")
        payload["consumer"] = c
    if parents:
        payload["parents"] = list(parents)
    if parent_subjects:
        payload["parent_subjects"] = list(parent_subjects)
    if parent_iats:
        payload["parent_iats"] = list(parent_iats)
    if creator:
        payload["creator"] = creator
    if ttl_seconds is not None:
        payload["exp"] = now + ttl_seconds
    if secret is not None:
        # Raw secret (e.g. test): label kid 'override' so verifiers skip lookup.
        return jwt.encode(payload, secret, algorithm="HS256", headers={"kid": kid or "override"})

    secret_map = settings.jwt_secret_map()
    active_kid = kid or settings.jwt_active_kid
    if active_kid not in secret_map:
        raise TokenError(f"active kid {active_kid!r} not in secret map; check settings")
    algorithm = settings.jwt_algorithm
    return jwt.encode(
        payload,
        secret_map[active_kid],
        algorithm=algorithm,
        headers={"kid": active_kid},
    )


def verify_token(token: str, *, secret: str | None = None) -> AuthInfo:
    """Verify a JWT and return AuthInfo. Raises TokenError on failure.

    The header `kid` picks the key (default `settings.jwt_active_kid`) from the
    map the header `alg` names, and only configured algorithms are accepted:
    HS256 when Loom signs with shared secrets, RS256 when it signs with RSA
    keys or trusts a gateway's public keys. An RS256 deployment's secret map
    holds private keys, so it never verifies an HS256 token.
    """
    if secret is None:
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise TokenError(f"invalid token header: {e}") from e
        algorithm = unverified_header.get("alg")
        accepted = _accepted_algorithms()
        if algorithm not in accepted:
            raise TokenError(f"token alg {algorithm!r} not in accepted set {sorted(accepted)}")
        token_kid = unverified_header.get("kid", settings.jwt_active_kid)
        keys = settings.jwt_public_key_map() if algorithm == "RS256" else settings.jwt_secret_map()
        if not isinstance(token_kid, str) or token_kid not in keys:
            raise TokenError(f"token kid {token_kid!r} not in the {algorithm} key map")
        secret = keys[token_kid]
    else:
        algorithm = settings.jwt_algorithm

    # Loom's own issuer plus the federated issuers in the
    # `jwt_trusted_issuers` CSV.
    trusted_issuers = {settings.jwt_issuer}
    for extra in settings.jwt_trusted_issuers.split(","):
        extra = extra.strip()
        if extra:
            trusted_issuers.add(extra)

    try:
        # `issuer=` is checked manually below: PyJWT only supports a single
        # string, not a set. `exp` is not required (indefinite tokens) but
        # still enforced when present.
        decoded = jwt.decode(
            token,
            secret,
            algorithms=[algorithm],
            audience=settings.jwt_audience,
            options={"require": ["iat", "sub", "iss", "aud"]},
            leeway=settings.jwt_leeway_seconds,  # clock-skew tolerance
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError(f"token expired: {e}") from e
    except jwt.PyJWTError as e:  # InvalidKeyError is not an InvalidTokenError
        raise TokenError(f"invalid token: {e}") from e
    if decoded.get("iss") not in trusted_issuers:
        # The trusted list is deployment config: logged, not told to the caller.
        logging.getLogger(__name__).warning(
            "untrusted issuer %r; allowed: %s", decoded.get("iss"), sorted(trusted_issuers)
        )
        raise TokenError(f"untrusted issuer {decoded.get('iss')!r}")

    _check_not_revoked(decoded)

    roles: list[Role] = []
    for name in _str_list(decoded, "roles"):
        try:
            roles.append(Role[name.upper()])
        except KeyError as e:
            raise TokenError(f"unknown role in token: {name!r}") from e

    ns_roles_claim = decoded.get("ns_roles") or {}
    if not isinstance(ns_roles_claim, dict):
        raise TokenError("claim 'ns_roles' must be an object")
    org = decoded.get("org", "")
    if not isinstance(org, str):
        raise TokenError("claim 'org' must be a string")
    exp = decoded.get("exp")

    return AuthInfo(
        subject=str(decoded["sub"]),
        org=org,
        namespaces=tuple(_str_list(decoded, "namespaces")),
        write_namespaces=tuple(_str_list(decoded, "ns_write")),
        ns_roles=tuple((str(k), str(v)) for k, v in ns_roles_claim.items()),
        roles=tuple(roles),
        is_service_account=bool(decoded.get("sa", False)),
        read_scope=str(decoded.get("read_scope") or ""),
        read_user_id=str(decoded.get("read_user_id") or ""),
        token_id=token_id_of(token, decoded.get("jti")),
        traffic_class=_traffic_class_claim(decoded.get("traffic_class")),
        consumer=_consumer_claim(decoded.get("consumer")),
        parents=tuple(_str_list(decoded, "parents")),
        parent_subjects=tuple(_str_list(decoded, "parent_subjects")),
        parent_iats=tuple(_parent_iats(decoded)),
        creator=_creator_claim(decoded),
        issued_at=_iat_of(decoded),
        expires_at=int(exp) if isinstance(exp, (int, float)) else None,
    )


def _check_not_revoked(decoded: dict[str, Any]) -> None:
    """Refuse a token revoked by `jti` (one token), by `sub` (every token of
    a principal) or by `sub` with an issued-before cutoff (tokens minted
    before it die, a re-mint after it stays valid). A delegated token also
    dies with any ancestor token (`parents`), any ancestor's `sub`
    (`parent_subjects`, each judged by its own `parent_iats` entry: a child
    minted after the cutoff from an older parent must die with it) and its
    creator's `sub` (judged by the root's `iat`, the oldest in the chain).
    `iat` is required, so the cutoff always has an anchor."""
    from server.auth.revocation import is_revoked, loaded, snapshot_age_seconds

    if not loaded():
        raise RevocationUnavailable("token revocation list not loaded yet; retry shortly")
    if snapshot_age_seconds() > settings.jwt_revocation_max_stale_seconds:
        raise RevocationUnavailable("token revocation list is stale; retry shortly")
    jti = decoded.get("jti")
    if jti and is_revoked(str(jti)):
        raise TokenError(f"token jti {jti!r} is revoked")
    issued_at = _iat_of(decoded)
    sub_for_revoke = decoded.get("sub")
    if sub_for_revoke and is_revoked(str(sub_for_revoke), issued_at=issued_at):
        raise TokenError(f"token sub {sub_for_revoke!r} is revoked")
    if any(is_revoked(parent) for parent in _str_list(decoded, "parents")):
        raise TokenError("a parent of this token is revoked")
    parent_iats = _parent_iats(decoded)
    creator = _creator_claim(decoded)
    oldest = min([*parent_iats, issued_at]) if issued_at is not None else None
    if creator and is_revoked(creator, issued_at=oldest):
        raise TokenError(f"token creator {creator!r} is revoked")
    chain = zip(_str_list(decoded, "parent_subjects"), parent_iats, strict=True)
    if any(is_revoked(sub, issued_at=iat) for sub, iat in chain):
        raise TokenError("a parent of this token is revoked")


def _creator_claim(decoded: dict[str, Any]) -> str:
    creator = decoded.get("creator") or ""
    if not isinstance(creator, str):
        raise TokenError("claim 'creator' must be a string")
    return creator


def _accepted_algorithms() -> frozenset[str]:
    """The algorithms this deployment's keys can verify: its own signing
    algorithm, plus RS256 when a gateway's public keys are configured."""
    accepted = {settings.jwt_algorithm}
    if settings.jwt_public_key_map():
        accepted.add("RS256")
    return frozenset(accepted & {"HS256", "RS256"})


def _str_list(decoded: dict[str, Any], claim: str) -> list[str]:
    """A list-of-strings claim; absent is empty. Any other shape is refused —
    iterating a string claim would yield one "namespace" per character."""
    value = decoded.get(claim) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TokenError(f"claim {claim!r} must be a list of strings")
    return value


def _parent_iats(decoded: dict[str, Any]) -> list[int]:
    """The `parent_iats` claim, one int per `parent_subjects` entry. A missing
    or misaligned list is refused: without it an ancestor's cutoff would be
    judged by this token's (younger) `iat` and miss."""
    value = decoded.get("parent_iats") or []
    subjects = _str_list(decoded, "parent_subjects")
    if (
        not isinstance(value, list)
        or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)
        or len(value) != len(subjects)
    ):
        raise TokenError("claim 'parent_iats' must list one int per parent subject")
    return value


def token_id_of(token: str, jti: Any) -> str:
    """The credential id the ledgers stamp: the `jti` when the
    token carries one (every `mint_token` output does), else the
    first 16 hex characters of sha256 over the encoded token — stable for the
    credential's lifetime, never the token."""
    if jti:
        return str(jti)
    import hashlib

    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:16]


def new_jti() -> str:
    """A fresh token id: 128 random bits as 32 hex characters. Unguessable so
    a revocation row cannot be pre-planted for a token yet to be minted, and
    plain hex so it survives the ledgers' String columns and a shell."""
    return secrets.token_hex(16)


def _iat_of(decoded: dict[str, Any]) -> int | None:
    """The token's `iat` as unix seconds, None when it is not a number (the
    `require` list above already refused a token without one)."""
    raw = decoded.get("iat")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _consumer_claim(raw: Any) -> str:
    """The `consumer` claim normalised the way a request's field is; an
    unusable value is '' (the request's field decides) rather than refused."""
    from server.observability.consumer import claim_consumer

    return claim_consumer(raw)


def _traffic_class_claim(raw: Any) -> str:
    """The `traffic_class` claim normalised; an unknown value is ignored
    ('' — the namespace decides) rather than refused, so a token minted by
    a newer build still verifies here."""
    from server.observability.traffic_class import normalize_class

    return normalize_class(raw)
