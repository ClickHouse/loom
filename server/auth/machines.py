"""Bots and CI jobs: machine identities that never sign in at an identity
provider and never hold a long-lived secret.

An operator registers one (`loom bot create`) with the namespaces its data
belongs in and how it proves itself at /oauth/token; each proof gets a
service-account token for `sa:<name>`: an hour, or the identity's own maximum
(`max_access_token_s`, at most a day) when shorter, unless the request asks for
another lifetime (`expires_in`) up to that maximum:

- a key: the bot signs a short assertion with its private key, the
  client-credentials grant with `private_key_jwt` (RFC 7523);
- a workload trust: a CI job hands over the OIDC token its platform issued
  for this run, accepted only from exactly the registered issuer, for its
  audience, with every listed claim matching (RFC 8693 token exchange).

Disabling one (`loom bot disable`) refuses every later proof and revokes the
tokens it already has. One registered `--platform-admin` names no namespace:
each of its tokens holds the platform_admin role, which spans every org.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
import jwt

from server.auth.keyring import MIN_RSA_BITS
from server.core.ch import get_client, own_write_reads
from server.core.versions import next_version

log = logging.getLogger(__name__)

TABLE = "loom.machine_identities"
TOKEN_TTL = 3600
# not a setting: the ceiling an identity's own maximum may name, a product limit
MAX_TOKEN_TTL = 86400
# not a setting: a shorter one would run out before its first use (verify's leeway)
MIN_TOKEN_TTL = 60
_PLATFORM_ADMIN_MIGRATION = 76  # schemas/migrations/076_machine_platform_admin.sql
ACCESS_TOKEN_MIGRATION = 77  # schemas/migrations/077_machine_access_token_ttl.sql
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
SUBJECT_TOKEN_TYPES = frozenset(
    {"urn:ietf:params:oauth:token-type:jwt", "urn:ietf:params:oauth:token-type:id_token"}
)
KEY_ALGORITHMS = frozenset({"ES256", "RS256", "EdDSA"})
_WORKLOAD_ALGORITHMS = ["RS256", "ES256"]
_ASSERTION_MAX_S = 300  # an assertion is made for one request, not kept
_LEEWAY_S = 30


class Machine(TypedDict):
    name: str
    org: str
    ns_roles: list[tuple[str, str]]
    public_keys: str
    issuer: str
    audience: str
    claims: dict[str, str]
    # A trust that tells agents apart (server/auth/agents.py); '' for one bot.
    agent_claim: str
    # Where the issuer publishes its keys; '' = its OIDC discovery says.
    jwks_uri: str
    # The longest a workload token may live to be taken, and how many agents
    # the trust may make.
    max_token_s: int
    max_agents: int
    # The longest token a proof may be issued when the request asks for one
    # (`expires_in`); without one it gets an hour, or this when shorter.
    max_access_token_s: int
    # Its tokens carry the platform_admin role, which spans every org; such a
    # bot names no namespace and tells no agents apart (`account_admin.bot_from`).
    platform_admin: bool


async def platform_admin_tokens_ready(ch: Any) -> bool:
    """Older replicas accept admin JWTs without protecting human sign-in changes."""
    return await fleet_at(ch, _PLATFORM_ADMIN_MIGRATION)


async def access_token_maximum_ready(ch: Any) -> bool:
    """Older replicas issue an hour whatever the maximum, and reset it when they save."""
    return await fleet_at(ch, ACCESS_TOKEN_MIGRATION)


async def fleet_at(ch: Any, migration: int) -> bool:
    """Every live replica, in every deployment, runs `migration` or later."""
    from server.core import instances

    try:
        live = await instances.read_live(ch, all_deployments=True)
        return bool(live) and all(int(row["schema_version"] or 0) >= migration for row in live)
    except Exception as e:  # noqa: BLE001 — an unread fleet refuses what it gates
        log.warning("fleet check for migration %d failed: %s", migration, type(e).__name__)
        return False


class Refused(Exception):
    """A proof Loom does not accept; `error` is the OAuth error code."""

    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(description or error)
        self.error, self.description = error, description


def subject(name: str) -> str:
    return f"sa:{name}"


async def get(name: str, *, active_only: bool = True, ch: Any = None) -> Machine | None:
    """The machine identity `name` (only an active one, unless `active_only` is
    off for an operator finishing a disable), read as of every write that
    landed: a replica a moment behind must not mint for a bot just disabled."""
    active = " AND active = 1" if active_only else ""
    with own_write_reads():
        row = await (ch or get_client()).query_one(
            f"SELECT * FROM {TABLE} FINAL WHERE name = {{name:String}}{active}",
            params={"name": name},
        )
    return _machine(row) if row is not None else None


def _machine(row: dict[str, Any]) -> Machine:
    return {
        "name": row["name"],
        "org": row["org"],
        "ns_roles": [(str(ns), str(role)) for ns, role in row["ns_roles"]],
        "public_keys": row["public_keys"],
        "issuer": row["issuer"],
        "audience": row["audience"],
        "claims": dict(row["claims"]),
        "agent_claim": row["agent_claim"],
        "jwks_uri": row["jwks_uri"],
        "max_token_s": int(row["max_token_s"]),
        "max_agents": int(row["max_agents"]),
        "max_access_token_s": int(row["max_access_token_s"]),
        "platform_admin": bool(row.get("platform_admin", 0)),  # a row of a build before it
    }


def asked_lifetime(raw: str | None) -> int | None:
    """Parse an optional lifetime in whole seconds, refusing invalid requests."""
    if raw is None or raw == "":
        return None
    refused = f"expires_in is a whole number of seconds, {MIN_TOKEN_TTL} or more"
    if not (raw.isascii() and raw.isdigit()):
        raise Refused("invalid_request", refused)
    digits = raw.lstrip("0")
    # Ten digits or more is past any maximum, and is cut to it: never converted
    # whole, as Python refuses a string of thousands of digits.
    value = MAX_TOKEN_TTL if len(digits) >= 10 else int(digits or "0")
    if value < MIN_TOKEN_TTL:
        raise Refused("invalid_request", refused)
    return value


def token_lifetime(m: Machine, asked: int | None) -> int:
    """Cap the requested or default lifetime at the bot's maximum (bot_from keeps it
    within a day)."""
    return min(TOKEN_TTL if asked is None else asked, m["max_access_token_s"])


async def live_roles(m: Machine) -> list[tuple[str, str]]:
    """The bot's (namespace, role) pairs on namespaces still active: one
    released or deleted since the bot was made grants it nothing."""
    active = await active_namespaces(m["org"], [ns for ns, _ in m["ns_roles"]])
    return [(ns, role) for ns, role in m["ns_roles"] if ns in active]


async def active_namespaces(org: str, names: list[str]) -> set[str]:
    """Those of `names` that are active namespaces of `org`."""
    if not names:
        return set()
    with own_write_reads():  # "register it, then the bot" may have used another replica
        rows = await get_client().query_all(
            "SELECT namespace FROM loom.namespaces FINAL WHERE org = {org:String} "
            "AND namespace IN {names:Array(String)} AND active",
            params={"org": org, "names": names},
        )
    return {r["namespace"] for r in rows}


async def naming(org: str, namespace: str, *, ch: Any = None) -> list[str]:
    """The active bots naming (org, namespace)."""
    ch = ch or get_client()
    with own_write_reads():
        rows = await ch.query_all(
            f"SELECT name FROM {TABLE} FINAL WHERE org = {{org:String}} AND active = 1 "
            "AND arrayExists(r -> r.1 = {ns:String}, ns_roles)",
            params={"org": org, "ns": namespace},
        )
    return [str(r["name"]) for r in rows]


async def forget_namespace(org: str, namespace: str, by: str, *, ch: Any = None) -> list[str]:
    """Drop (org, namespace) from every active bot naming it: it was released,
    and a next holder of the name is not who the bot was made for. Returns the
    bots changed."""
    ch = ch or get_client()
    from server.auth.grant_claims import bot_change

    changed = []
    for name in await naming(org, namespace, ch=ch):
        async with bot_change(name):  # read again under it: a disable or replace may land
            m = await get(name, ch=ch)
            if m is not None:
                m["ns_roles"] = [(ns, role) for ns, role in m["ns_roles"] if ns != namespace]
                await save(m, by=by, ch=ch)
                changed.append(m["name"])
    return changed


async def listed() -> list[dict[str, Any]]:
    with own_write_reads():  # a trust another replica saved a moment ago counts
        return await get_client().query_all(
            f"SELECT name, org, ns_roles, public_keys != '' AS has_key, issuer, audience, claims, "
            f"agent_claim, jwks_uri, max_token_s, max_agents, max_access_token_s, "
            f"platform_admin, active, updated_by, updated_at FROM {TABLE} FINAL "
            "ORDER BY name"
        )


async def save(m: Machine, *, by: str, active: bool = True, ch: Any = None) -> None:
    # Past the name's newest row, whichever replica's clock wrote it: under
    # FINAL a later change with a lower version would be lost.
    ch = ch or get_client()
    version = await next_version(ch, TABLE, {"name": m["name"]})
    await ch.insert(
        TABLE,
        [
            {
                **m,
                "platform_admin": int(m.get("platform_admin", False)),
                "active": int(active),
                "updated_by": by,
                "updated_at": datetime.now(UTC),
                "version": version,
            }
        ],
    )


async def verify_assertion(
    assertion: str, client_id: str, audiences: list[str]
) -> tuple[Machine, dict[str, Any]]:
    """The machine a client assertion proves, and its claims. The assertion
    names its machine (`iss` = `sub` = the name), is signed by one of its
    keys, is meant for this token endpoint and lives at most five minutes;
    the caller spends its `jti` once."""
    try:
        name = str(jwt.decode(assertion, options={"verify_signature": False}).get("iss") or "")
        kid = jwt.get_unverified_header(assertion).get("kid")
    except jwt.PyJWTError:
        raise Refused("invalid_client", "the client assertion is not a JWT") from None
    m = await get(name) if name and client_id in ("", name) else None
    if m is None or not m["public_keys"]:
        raise Refused("invalid_client")
    keys = [k for k in jwt.PyJWKSet.from_json(m["public_keys"]).keys if kid in (None, k.key_id)]
    for key in keys:
        if key.algorithm_name not in KEY_ALGORITHMS:
            continue
        try:
            claims: dict[str, Any] = jwt.decode(
                assertion,
                key.key,
                algorithms=[key.algorithm_name],
                audience=audiences,
                issuer=name,
                options={"require": ["exp", "iat", "jti", "iss", "sub", "aud"]},
                leeway=_LEEWAY_S,
            )
        except jwt.InvalidSignatureError:
            continue
        except jwt.PyJWTError as e:
            raise Refused("invalid_client", f"client assertion refused: {e}") from None
        if claims["sub"] != name:
            raise Refused("invalid_client", "the assertion's sub must be the client")
        if int(claims["exp"]) - int(claims["iat"]) > _ASSERTION_MAX_S:
            raise Refused("invalid_client", "the assertion may live at most 5 minutes")
        return m, claims
    raise Refused("invalid_client", "no registered key signed the assertion")


async def verify_workload(token: str, name: str) -> tuple[Machine, dict[str, Any]]:
    """The machine whose workload trust `token` satisfies, and its claims.
    Only the registered issuer's keys are ever fetched."""
    m = await get(name)
    if m is None or not m["issuer"] or not m["claims"]:
        raise Refused("invalid_client")
    if _unverified(token).get("iss") != m["issuer"]:
        raise Refused("invalid_grant", "the subject token is from another issuer")
    claims = await _signed(token, m)
    _meets(m, claims)
    return m, claims


async def verify_any_workload(token: str) -> tuple[Machine, dict[str, Any]]:
    """`verify_workload` for an exchange that names no client: the one active
    trust of the token's issuer whose audience and claims it meets (RFC 8693
    makes client_id optional; trusts of one issuer differ by their claims).
    The signature is checked once per place the trusts take keys from."""
    unverified = _unverified(token)
    aud = unverified.get("aud")
    # Read before the signature is: anything but strings is no audience.
    auds = {a for a in (aud if isinstance(aud, list) else [aud]) if isinstance(a, str)}
    with own_write_reads():
        rows = await get_client().query_all(
            f"SELECT * FROM {TABLE} FINAL WHERE issuer = {{iss:String}} AND active = 1 "
            "ORDER BY name",
            params={"iss": str(unverified.get("iss", ""))},
        )
    trusts = [m for m in map(_machine, rows) if m["claims"] and m["audience"] in auds]
    signed: dict[tuple[str, str], dict[str, Any] | Refused] = {}
    met: list[tuple[Machine, dict[str, Any]]] = []
    away: list[tuple[Machine, Refused]] = []  # trusts whose keys could not be had
    reasons: list[str] = []  # for the log: the presenter learns no trust's claims
    for m in trusts:
        source = (m["jwks_uri"], m["audience"])
        if source not in signed:
            try:
                signed[source] = await _signed(token, m)
            except Refused as e:
                signed[source] = e
        claims = signed[source]
        if isinstance(claims, Refused):
            if claims.error == "temporarily_unavailable":
                away.append((m, claims))
            reasons.append(f"{m['name']}: {claims}")
            continue
        try:
            _meets(m, claims)
            met.append((m, claims))
        except Refused as e:
            reasons.append(f"{m['name']}: {e}")
    if len(met) > 1:
        log.warning("workload token meets trusts %s", ", ".join(m["name"] for m, _ in met))
        raise Refused("invalid_request", "the subject token meets several trusts; name one")
    if met:
        # The claims are signed now: a trust whose keys are away matters only if
        # it would take them too, and then the answer must wait for its keys.
        for m, outage in away:
            if _could_meet(m, met[0][1]):
                raise outage
        return met[0]
    if away:
        raise away[0][1]
    log.info("workload token taken by no trust: %s", "; ".join(reasons) or "none of its issuer")
    raise Refused("invalid_grant", "no trust of this issuer takes the subject token")


def _could_meet(m: Machine, claims: dict[str, Any]) -> bool:
    try:
        _meets(m, claims)
    except Refused:
        return False
    return True


def _unverified(token: str) -> dict[str, Any]:
    try:
        claims: dict[str, Any] = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        raise Refused("invalid_grant", "the subject token is not a JWT") from None
    return claims


async def _signed(token: str, m: Machine) -> dict[str, Any]:
    """The token's claims, its signature checked with the keys `m` takes."""
    from server.auth import oidc

    try:
        jwks_uri = m["jwks_uri"] or await _jwks_uri(m["issuer"])
        key = await asyncio.to_thread(oidc.signing_key, jwks_uri, token)
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=_WORKLOAD_ALGORITHMS,
            audience=m["audience"],
            issuer=m["issuer"],
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            leeway=_LEEWAY_S,
        )
    except (httpx.HTTPError, jwt.PyJWKClientConnectionError, _Undiscovered) as e:
        # The answer is unauthenticated: the keys' URL and the network error are logged.
        log.warning(
            "workload token: the issuer's keys could not be read: %s: %s", type(e).__name__, e
        )
        raise Refused(
            "temporarily_unavailable", "the issuer's keys could not be read; retry"
        ) from e
    except (jwt.PyJWTError, ValueError, KeyError) as e:
        raise Refused("invalid_grant", f"subject token refused: {e}") from None
    return claims


def _meets(m: Machine, claims: dict[str, Any]) -> None:
    """Refused unless the verified claims meet the trust."""
    # A long-lived token is a credential to replay: the platform should not
    # issue one for this, and Loom does not take it.
    if int(claims["exp"]) - int(claims["iat"]) > m["max_token_s"]:
        raise Refused("invalid_grant", f"the subject token may live at most {m['max_token_s']} s")
    for claim, want in m["claims"].items():
        if not matches(str(claims.get(claim, "")), want):
            raise Refused("invalid_grant", f"claim {claim!r} does not match the trust")


def matches(value: str, want: str) -> bool:
    """A trust's claim condition: exact, or a prefix when it ends in `*`."""
    if want.endswith("*"):
        return value.startswith(want[:-1])
    return value == want


class _Undiscovered(Exception):
    """The issuer's discovery document could not be read, or does not name it."""


_discovered: dict[str, tuple[str, float]] = {}  # issuer -> (jwks_uri, read at)


async def _jwks_uri(issuer: str) -> str:
    from server.clients._http import get_client as http
    from server.settings import settings

    cached = _discovered.get(issuer)
    if cached and time.monotonic() - cached[1] < settings.auth_machine_discovery_ttl_s:
        return cached[0]
    try:
        r = await http().get(
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
            timeout=settings.identity_provider_timeout_s,
        )
        r.raise_for_status()
        doc = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise _Undiscovered(type(e).__name__) from e
    # A document that does not match is a trust registered wrong, not a fault to retry.
    if not isinstance(doc, dict) or doc.get("issuer") != issuer:
        raise Refused("invalid_client", "the trust's issuer names another in its discovery")
    jwks_uri = str(doc.get("jwks_uri", ""))
    if not jwks_uri.startswith("https://"):
        raise Refused("invalid_client", "the trust's issuer names no https jwks_uri")
    _discovered[issuer] = (jwks_uri, time.monotonic())
    return jwks_uri


# The members an RFC 7638 thumbprint hashes, per key type.
_THUMBPRINT_MEMBERS = {
    "EC": ("crv", "kty", "x", "y"),
    "RSA": ("e", "kty", "n"),
    "OKP": ("crv", "kty", "x"),
}


def public_jwks(public_pem: bytes) -> str:
    """A JWKS holding one public key (PEM: P-256, RSA of 2048 bits or more, or
    Ed25519), with its algorithm and its RFC 7638 thumbprint as `kid`."""
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm

    key = load_pem_public_key(public_pem)
    if isinstance(key, ec.EllipticCurvePublicKey) and key.curve.name == "secp256r1":
        jwk, alg = ECAlgorithm.to_jwk(key, as_dict=True), "ES256"
    elif isinstance(key, rsa.RSAPublicKey) and key.key_size >= MIN_RSA_BITS:
        jwk, alg = RSAAlgorithm.to_jwk(key, as_dict=True), "RS256"
    elif isinstance(key, ed25519.Ed25519PublicKey):
        jwk, alg = OKPAlgorithm.to_jwk(key, as_dict=True), "EdDSA"
    else:
        raise ValueError("use a P-256, Ed25519 or RSA (2048 bits or more) public key")
    members = _THUMBPRINT_MEMBERS[jwk["kty"]]
    canonical = json.dumps({m: jwk[m] for m in members}, separators=(",", ":"), sort_keys=True)
    kid = base64.urlsafe_b64encode(hashlib.sha256(canonical.encode()).digest()).rstrip(b"=")
    return json.dumps({"keys": [{**jwk, "alg": alg, "use": "sig", "kid": kid.decode()}]})


def generate_key() -> tuple[bytes, bytes]:
    """A new P-256 key pair: (private PEM, public PEM)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    return (
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ),
    )
