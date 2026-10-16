"""Bots and CI jobs: machine identities that never sign in at an identity
provider and never hold a long-lived secret.

An operator registers one (`loom bot create`) with the namespaces its data
belongs in and how it proves itself at /oauth/token; each proof gets an
hour-long service-account token for `sa:<name>`:

- a key: the bot signs a short assertion with its private key, the
  client-credentials grant with `private_key_jwt` (RFC 7523);
- a workload trust: a CI job hands over the OIDC token its platform issued
  for this run, accepted only from exactly the registered issuer, for its
  audience, with every listed claim matching (RFC 8693 token exchange).

Disabling one (`loom bot disable`) refuses every later proof and revokes the
tokens it already has.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any, TypedDict

import httpx
import jwt

from server.core.ch import get_client

TABLE = "loom.machine_identities"
TOKEN_TTL = 3600
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


class Refused(Exception):
    """A proof Loom does not accept; `error` is the OAuth error code."""

    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(description or error)
        self.error, self.description = error, description


def subject(name: str) -> str:
    return f"sa:{name}"


async def get(name: str) -> Machine | None:
    """The active machine identity `name`, read as of every write that landed:
    a replica a moment behind must not mint for a bot just disabled."""
    from server.core.ch import own_write_reads

    with own_write_reads():
        row = await get_client().query_one(
            f"SELECT * FROM {TABLE} FINAL WHERE name = {{name:String}} AND active = 1",
            params={"name": name},
        )
    if row is None:
        return None
    return {
        "name": row["name"],
        "org": row["org"],
        "ns_roles": [(str(ns), str(role)) for ns, role in row["ns_roles"]],
        "public_keys": row["public_keys"],
        "issuer": row["issuer"],
        "audience": row["audience"],
        "claims": dict(row["claims"]),
    }


async def listed() -> list[dict[str, Any]]:
    return await get_client().query_all(
        f"SELECT name, org, ns_roles, public_keys != '' AS has_key, issuer, audience, claims, "
        f"active, updated_by, updated_at FROM {TABLE} FINAL ORDER BY name"
    )


async def save(m: Machine, *, by: str, active: bool = True) -> None:
    from server.core.ch import own_write_reads

    # Past the newest row of the name, whichever replica's clock wrote it: under
    # FINAL a later change with a lower version would be lost.
    with own_write_reads():
        newest = await get_client().query_one(
            f"SELECT max(version) AS v FROM {TABLE} WHERE name = {{name:String}}",
            params={"name": m["name"]},
        )
    version = max(time.time_ns(), int((newest or {}).get("v") or 0) + 1)
    await get_client().insert(
        TABLE,
        [
            {
                **m,
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
    try:
        iss = jwt.decode(token, options={"verify_signature": False}).get("iss")
    except jwt.PyJWTError:
        raise Refused("invalid_grant", "the subject token is not a JWT") from None
    if iss != m["issuer"]:
        raise Refused("invalid_grant", "the subject token is from another issuer")
    from server.auth import oidc

    try:
        jwks_uri = await _jwks_uri(m["issuer"])
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
        raise Refused("temporarily_unavailable", f"the issuer's keys: {e}") from e
    except (jwt.PyJWTError, ValueError, KeyError) as e:
        raise Refused("invalid_grant", f"subject token refused: {e}") from None
    for claim, want in m["claims"].items():
        if not matches(str(claims.get(claim, "")), want):
            raise Refused("invalid_grant", f"claim {claim!r} does not match the trust")
    return m, claims


def matches(value: str, want: str) -> bool:
    """A trust's claim condition: exact, or a prefix when it ends in `*`."""
    if want.endswith("*"):
        return value.startswith(want[:-1])
    return value == want


class _Undiscovered(Exception):
    """The issuer's discovery document could not be read, or does not name it."""


_DISCOVERY_TTL_S = 3600
_discovered: dict[str, tuple[str, float]] = {}  # issuer -> (jwks_uri, read at)


async def _jwks_uri(issuer: str) -> str:
    from server.clients._http import get_client as http

    cached = _discovered.get(issuer)
    if cached and time.monotonic() - cached[1] < _DISCOVERY_TTL_S:
        return cached[0]
    try:
        r = await http().get(f"{issuer.rstrip('/')}/.well-known/openid-configuration", timeout=10.0)
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
    elif isinstance(key, rsa.RSAPublicKey) and key.key_size >= 2048:
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
