"""Service-account JWT mint + verify (HS256 shared-secret, RS256 via JWKS).

`exp` is optional: tokens are indefinite unless the minter passes
`ttl_seconds`. Revoke every token of a key with `loom signing-key retire`;
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
from server.core.principals import RESERVED_SUBJECT, is_reserved_subject
from server.settings import EndUserIssuer, settings


class TokenError(Exception):
    """Raised when a token is malformed, expired, or signed with the wrong key.
    `reason` is the bounded label `loom.auth.refusals` counts it under."""

    reason = "invalid"

    def __init__(self, message: str = "", *, reason: str = "") -> None:
        super().__init__(message)
        if reason:
            self.reason = reason


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
) -> None:
    """Refuse to start a deployment (`LOOM_DEPLOYMENT=1`) that requires auth
    while its root secret (`LOOM_JWT_SECRET`) is the checked-in dev default:
    anyone who has read the repository can mint tokens it accepts and open the
    credentials and signing keys it seals. The local compose stack
    (`LOOM_INSTANCE_LABEL=deploy:dev`) is the explicit exception; processes
    that are not the deployment (tests, isolated servers, CLIs) are not
    refused, and `loom doctor` / `/v1/health` still flag them."""
    if not (deployment and auth_required and dev_secret):
        return
    if instance_label.strip() == DEV_PROFILE_LABEL:
        return
    raise DevSigningSecretRefused(
        "refusing to start: auth is required and LOOM_JWT_SECRET is the checked-in dev "
        "default, which anyone can mint accepted tokens with. Set it to a private value; "
        f"the local dev stack (LOOM_INSTANCE_LABEL={DEV_PROFILE_LABEL}) is the only "
        "deployment exempt."
    )


class AuthUnavailable(TokenError):
    """The token may be valid, but this process cannot judge it yet: a
    retryable 503 AUTH_UNAVAILABLE, never a 401."""

    reason = "unavailable"


class RevocationUnavailable(AuthUnavailable):
    """This process has not loaded the revocation list, or its copy is older
    than `jwt_revocation_max_stale_seconds`, so it cannot tell whether the
    token was revoked. The list loads on the next refresh."""


class KeyringUnavailable(AuthUnavailable):
    """This process has not read the signing keyring yet, or not within
    `jwt_revocation_max_stale_seconds`, so a retired kid may not be known."""


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
    # Bearer's real subject under X-Loom-On-Behalf-Of (`subject` holds the one it acts
    # for); the gate refuses a revoked subject it acts for (`authz.check_acted_for`).
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
    # of every ancestor token, root first, and the sign-in session of a
    # third-party MCP client that minted it; revoking any of them kills this
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
    # `sid` claim: the sign-in session (server/auth/oauth.py) an hour-long
    # access token belongs to. Revoking it ends every token of the session;
    # '' = a token minted outside a sign-in session.
    session_id: str = ""
    # `fps` claim: the session is Loom's own sign-in (`loom login`, the
    # dashboard), whose children may outlive it (`delegation._child_ttl`);
    # False for a session a third-party client holds.
    first_party: bool = False
    # The agent an end-user token names (`agent:<id>`, signed by its issuer),
    # whose grants on a collection the request also holds; '' otherwise.
    agent_principal: str = ""
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
    read_scope: str | None = None,
    read_user_id: str | None = None,
    traffic_class: str | None = None,
    consumer: str | None = None,
    jti: str | None = None,
    session_id: str | None = None,
    first_party: bool = False,
    parents: list[str] | None = None,
    parent_subjects: list[str] | None = None,
    parent_iats: list[int] | None = None,
    creator: str | None = None,
    issued_at: int | None = None,
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
    => no `exp` => indefinite token. The signing kid goes in the header so
    verifiers pick the matching key during signing-key rotation.
    `parents` / `parent_subjects` / `parent_iats` / `creator` are the delegation claims
    `delegation.delegate` computes; a token minted from another token gets
    them from there. `issued_at` (unix seconds; default now) is the `iat`:
    a path that read the revocations before minting passes the time it took
    before that read, so a revocation that landed after it still postdates
    the token.
    """
    if "\x00" in subject:
        # Reserved: revocation keys an operator's cutoffs by a NUL a subject never has.
        raise ValueError("a token subject may not contain a NUL character")
    if is_reserved_subject(subject):
        # Only an end-user issuer's tokens carry these.
        raise ValueError(f"{RESERVED_SUBJECT}: {subject!r}")
    now = int(time.time()) if issued_at is None else issued_at
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
    if session_id:
        payload["sid"] = session_id
        if first_party:
            payload["fps"] = True
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
        return jwt.encode(payload, secret, algorithm="HS256", headers={"kid": "override"})

    from server.auth import keyring

    key = keyring.signing_key()
    return jwt.encode(payload, key.secret, algorithm=key.algorithm, headers={"kid": key.kid})


def verify_token(token: str, *, secret: str | None = None) -> AuthInfo:
    """Verify a JWT and return AuthInfo. Raises TokenError on failure.

    The header `kid` and `alg` pick the key (`server.auth.keyring`): a keyring
    key, the root secret (kid `default`, HS256), or a gateway's RS256 public
    key. A token whose kid has no key under its algorithm is refused.
    """
    if secret is None:
        from server.auth import keyring

        end_user = _end_user_key(token)
        if end_user is not None:
            return _verify_end_user(token, *end_user)
        if not keyring.loaded():
            raise KeyringUnavailable("the signing keyring is not loaded yet; retry shortly")
        if keyring.stale():
            raise KeyringUnavailable("the signing keyring is stale; retry shortly")
        try:
            unverified_header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise TokenError(f"invalid token header: {e}") from e
        algorithm = unverified_header.get("alg")
        token_kid = unverified_header.get("kid", keyring.ROOT_KID)
        if algorithm not in keyring.ALGORITHMS or not isinstance(token_kid, str):
            raise TokenError(f"token alg {algorithm!r} is not one of {keyring.ALGORITHMS}")
        candidates = keyring.verification_keys(token_kid, algorithm)
        if not candidates:
            raise TokenError(f"token kid {token_kid!r} has no {algorithm} key")
    else:
        algorithm = "HS256"
        candidates = [secret]

    trusted = settings.trusted_issuers()

    try:
        decoded = _decode(token, candidates, algorithm)
    except jwt.ExpiredSignatureError as e:
        raise TokenError(f"token expired: {e}", reason="expired") from e
    except jwt.PyJWTError as e:  # InvalidKeyError is not an InvalidTokenError
        raise TokenError(f"invalid token: {e}") from e
    # PyJWT accepts a numeric string for these; delegation and revocation read them
    # as numbers, so a string `exp` would mint an unbounded child. JSON numbers only.
    for claim in ("exp", "iat"):
        value = decoded.get(claim)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise TokenError(f"claim {claim!r} must be a number")
    if not isinstance(decoded.get("iss"), str):
        raise TokenError("claim 'iss' must be a string")
    if decoded.get("iss") not in trusted:
        # The trusted list is deployment config: logged, not told to the caller.
        logging.getLogger(__name__).warning(
            "untrusted issuer %r; allowed: %s", decoded.get("iss"), sorted(trusted)
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
    if "\x00" in str(decoded["sub"]):
        raise TokenError("claim 'sub' may not contain a NUL character")
    if is_reserved_subject(str(decoded["sub"])):
        raise TokenError(f"claim 'sub': {RESERVED_SUBJECT}")
    exp = decoded.get("exp")

    return AuthInfo(
        subject=str(decoded["sub"]),
        org=org,
        namespaces=tuple(_str_list(decoded, "namespaces")),
        write_namespaces=tuple(_str_list(decoded, "ns_write")),
        ns_roles=tuple((str(k), str(v)) for k, v in ns_roles_claim.items()),
        roles=tuple(roles),
        is_service_account=_bool_claim(decoded, "sa"),
        read_scope=_read_scope_claim(decoded),
        read_user_id=_optional_str_claim(decoded, "read_user_id"),
        token_id=token_id_of(token, decoded.get("jti")),
        traffic_class=_traffic_class_claim(decoded.get("traffic_class")),
        consumer=_consumer_claim(decoded.get("consumer")),
        parents=tuple(_str_list(decoded, "parents")),
        parent_subjects=tuple(_str_list(decoded, "parent_subjects")),
        parent_iats=tuple(_parent_iats(decoded)),
        creator=_creator_claim(decoded),
        session_id=_optional_str_claim(decoded, "sid"),
        first_party=_bool_claim(decoded, "fps"),
        issued_at=_iat_of(decoded),
        expires_at=int(exp) if exp is not None else None,
    )


class EndUserKeysUnavailable(AuthUnavailable):
    """An end-user issuer's JWKS has not been fetched in this process yet."""


def _end_user_key(token: str) -> tuple[str, EndUserIssuer, Any] | None:
    """(issuer, its spec, public key) when `token` names an end-user issuer
    (its `iss`, checked against the signature after), else None. The key is the
    issuer's own of the token's `kid`: from its `keys`, or its last fetched JWKS."""
    from server.auth import end_user_keys

    issuers = settings.end_user_issuers()
    if not issuers:
        return None
    try:
        iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError as e:
        raise TokenError(f"invalid token: {e}") from e
    if not isinstance(iss, str) or iss not in issuers:
        return None
    spec = issuers[iss]
    found = spec.keys.get(kid) if isinstance(kid, str) else None
    if found is None and isinstance(kid, str):
        found = end_user_keys.key(iss, kid)
    if found is None:
        if spec.jwks_url and not end_user_keys.loaded(iss):
            raise EndUserKeysUnavailable(f"the keys of {iss!r} are not fetched yet; retry shortly")
        raise TokenError(f"token kid {kid!r} is not a key of {iss!r}")
    return iss, spec, found


# Claims an end-user token may not carry: its identity is the user, nothing more.
_NOT_FOR_END_USERS = (
    "namespaces",
    "ns_write",
    "ns_roles",
    "roles",
    "sa",
    "read_scope",
    "read_user_id",
    "creator",
    "parents",
)


def _verify_end_user(token: str, issuer: str, spec: EndUserIssuer, public_key: Any) -> AuthInfo:
    """A short-lived token an end-user issuer signed for one of its users: one
    org, `sub: user:<id>`, and the agent the request runs inside when there is
    one (`agent: agent:<id>`, signed, since an agent's grants widen the
    request). It runs as that user alone: an org member with no claim, whose
    reads are pinned to the user's rows (`own_user`) and who reaches what
    grants give the user, the user's groups and the agent."""
    from server.core.principals import AGENT, USER, end_user_revocation_subject, principal_id

    try:
        decoded = _decode(token, [public_key], "RS256")
    except jwt.ExpiredSignatureError as e:
        raise TokenError(f"token expired: {e}", reason="expired") from e
    except jwt.PyJWTError as e:
        raise TokenError(f"invalid token: {e}") from e
    if decoded.get("iss") != issuer:
        raise TokenError(f"an end-user key of {issuer!r} signed a token of another issuer")
    user = principal_id(str(decoded.get("sub") or ""), USER)
    org = decoded.get("org")
    agent = decoded.get("agent") or ""
    exp, iat = decoded.get("exp"), decoded.get("iat")
    if not user:
        raise TokenError("an end-user token names one user: sub user:<id>")
    if not isinstance(org, str) or org in ("", "*"):
        raise TokenError("an end-user token names one org")
    if not spec.signs_for(org):
        raise TokenError(f"{issuer!r} does not sign for the org {org!r}")
    if not isinstance(decoded.get("jti"), str) or not decoded["jti"]:
        raise TokenError("an end-user token carries a jti, so one token can be revoked")
    if not isinstance(agent, str) or (agent and not principal_id(agent, AGENT)):
        raise TokenError("an end-user token's agent is agent:<id>")
    if not isinstance(exp, int | float) or isinstance(exp, bool) or not isinstance(iat, int):
        raise TokenError("an end-user token carries a numeric iat and exp")
    if exp - iat > settings.jwt_end_user_max_ttl_seconds:
        raise TokenError(
            f"an end-user token lives at most {settings.jwt_end_user_max_ttl_seconds} s"
        )
    carried = [c for c in _NOT_FOR_END_USERS if c in decoded]
    if carried:
        raise TokenError(f"an end-user token carries no {', '.join(carried)} claim")
    # `user:<id>` names one person per org, so a subject revocation of an end
    # user names its org (`loom revoke-token --subject <org>/user:<id>`): one
    # tenant's never reaches the same id at another.
    _check_not_revoked({**decoded, "sub": end_user_revocation_subject(org, USER + user)})
    return AuthInfo(
        subject=USER + user,
        org=org,
        namespaces=(),
        # A member of the org with no claim: an org-level read it may make, a
        # namespace only through a grant.
        roles=(Role.VIEWER,),
        is_service_account=False,
        impersonated_by=issuer,
        read_scope="own_user",
        read_user_id=user,
        agent_principal=agent,
        token_id=token_id_of(token, decoded.get("jti")),
        consumer=_consumer_claim(decoded.get("consumer")),
        issued_at=iat,
        expires_at=int(exp),
    )


def _decode(token: str, keys: list[Any], algorithm: str) -> dict[str, Any]:
    """The claims under the first of `keys` the signature matches. `issuer=` is
    checked by the caller (PyJWT takes one string, not a set); `exp` is not
    required (indefinite tokens) but enforced when present."""
    error = jwt.InvalidSignatureError("no key")
    for key in keys:
        try:
            return jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=settings.jwt_audience,
                options={"require": ["iat", "sub", "iss", "aud"]},
                leeway=settings.jwt_leeway_seconds,  # clock-skew tolerance
            )
        except jwt.InvalidSignatureError as e:
            error = e
    raise error


def _check_not_revoked(decoded: dict[str, Any]) -> None:
    """Refuse a token revoked by `jti` (one token), by `sid` (its sign-in
    session), by `sub` (every token of a principal) or by `sub` with an
    issued-before cutoff (tokens minted before it die, a re-mint after it
    stays valid). A delegated token also
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
        raise TokenError(f"token jti {jti!r} is revoked", reason="revoked")
    sid = decoded.get("sid")
    if sid and is_revoked(str(sid)):
        raise TokenError("the sign-in session of this token has ended", reason="revoked")
    issued_at = _iat_of(decoded)
    sub_for_revoke = decoded.get("sub")
    if sub_for_revoke and is_revoked(str(sub_for_revoke), issued_at=issued_at):
        raise TokenError(f"token sub {sub_for_revoke!r} is revoked", reason="revoked")
    if any(is_revoked(parent) for parent in _str_list(decoded, "parents")):
        raise TokenError("a parent of this token is revoked", reason="revoked")
    parent_iats = _parent_iats(decoded)
    creator = _creator_claim(decoded)
    oldest = min([*parent_iats, issued_at]) if issued_at is not None else None
    if creator and is_revoked(creator, issued_at=oldest):
        raise TokenError(f"token creator {creator!r} is revoked", reason="revoked")
    chain = zip(_str_list(decoded, "parent_subjects"), parent_iats, strict=True)
    if any(is_revoked(sub, issued_at=iat) for sub, iat in chain):
        raise TokenError("a parent of this token is revoked", reason="revoked")


def _bool_claim(decoded: dict[str, Any], claim: str) -> bool:
    value = decoded.get(claim, False)
    if not isinstance(value, bool):
        raise TokenError(f"claim {claim!r} must be a boolean")  # "false" is truthy
    return value


def _optional_str_claim(decoded: dict[str, Any], claim: str) -> str:
    value = decoded.get(claim)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TokenError(f"claim {claim!r} must be a string")  # 0 / false read as unset
    return value


def _read_scope_claim(decoded: dict[str, Any]) -> str:
    """'' (namespace-wide) when absent; otherwise exactly one of READ_SCOPES. Any
    other value is refused: read as '' it would widen the token to the namespace."""
    value = _optional_str_claim(decoded, "read_scope")
    if value and value not in READ_SCOPES:
        raise TokenError(f"claim 'read_scope' must be one of {', '.join(READ_SCOPES)}")
    return value


def _creator_claim(decoded: dict[str, Any]) -> str:
    creator = decoded.get("creator") or ""
    if not isinstance(creator, str):
        raise TokenError("claim 'creator' must be a string")
    return creator


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
    """The token's `iat` as unix seconds (verify_token requires it and refuses a
    non-number); None only for a claims dict that carries none."""
    raw = decoded.get("iat")
    return int(raw) if raw is not None else None


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
