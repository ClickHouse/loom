"""The token-introspection routes outside the op registry: the JWKS an
RS256 verifier reads, the OIDC discovery document of the identity tokens Loom
signs for an inference gateway, and `/v1/whoami`, which answers what a bearer
token verifies as."""

from __future__ import annotations

import base64
from typing import Any

import jwt as pyjwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server.auth.bearer import auth_unavailable, bearer_token
from server.auth.jwt import AuthUnavailable, TokenError, verify_token
from server.observability.metrics import record_auth_refusal
from server.settings import settings

auth_router = APIRouter()


@auth_router.get("/v1/.well-known/openid-configuration")
async def v1_openid_configuration() -> JSONResponse:
    """The issuer of the identity tokens Loom signs for an inference gateway
    (`LOOM_INFERENCE_TOKEN_AUDIENCE`), and where its keys are; 404 when Loom
    signs none. It issues nothing to people: no authorization endpoint."""
    from server.llm import gateway

    if not (settings.inference_token_url and settings.inference_token_audience):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    iss = gateway.issuer()
    return JSONResponse(
        {
            "issuer": iss,
            "jwks_uri": f"{iss}/.well-known/jwks.json",
            "id_token_signing_alg_values_supported": ["RS256"],
            "subject_types_supported": ["public"],
            "response_types_supported": ["id_token"],
        },
        headers={"Cache-Control": "no-store"},
    )


@auth_router.get("/v1/.well-known/jwks.json")
async def v1_jwks() -> dict[str, object]:
    """JWKS (JSON Web Key Set) for RS256 verifiers.

    One JWK per RS256 key in the signing keyring (`loom signing-key rotate
    --algorithm RS256`), so external services verify Loom-issued tokens
    without holding any private material. Empty while every key is HS256.
    """
    from server.auth import keyring

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        return {"keys": [], "error": "cryptography library not installed"}

    def _b64(n: int) -> str:
        b = n.to_bytes((n.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    keys: list[dict[str, object]] = []
    for kid, pem in keyring.public_keys().items():
        try:
            pub = serialization.load_pem_public_key(pem.encode())
            # JWKS publishes only RSA signing keys (kty=RSA below); narrow so the
            # public_numbers().n/.e access is typed instead of union-attr-erroring.
            if not isinstance(pub, rsa.RSAPublicKey):
                continue
            nums = pub.public_numbers()
            keys.append(
                {
                    "kty": "RSA",
                    "kid": kid,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64(nums.n),
                    "e": _b64(nums.e),
                }
            )
        except Exception:  # noqa: BLE001 — one bad key must not drop the JWKS
            continue
    return {"keys": keys}


@auth_router.get("/v1/whoami")
async def whoami(request: Request) -> dict[str, object]:
    """Service-account self-introspection.

    When the request carries a valid bearer token, returns the verified
    claims (sub, org, namespaces, roles, kid, exp). When no token is
    present (auth_required=False dev mode), returns {"authenticated": false}.

    Useful for SDK clients to debug "which kid is signing my tokens?" and
    for ops to verify a rolled-out kid is being accepted.
    """
    # A refused token is counted in loom.auth.refusals; no token is a signed-out
    # caller asking, not a refusal.
    try:
        token = bearer_token(request.headers.get("Authorization"))
    except TokenError as e:
        record_auth_refusal(e, transport="http")
        return {"authenticated": False, "reason": str(e)}
    if token is None:
        return {"authenticated": False}
    try:
        info = verify_token(token)
    except TokenError as e:
        record_auth_refusal(e, transport="http")
        if not isinstance(e, AuthUnavailable):
            # 200 + authenticated=false (not an error) so the SPA drops a stale
            # token and shows login rather than erroring the welcome screen.
            return {"authenticated": False, "reason": str(e)}
        # Transient: the token may be fine. authenticated=false would make the SPA
        # drop it, so answer 503 and let the client retry.
        raise auth_unavailable(e) from e

    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.PyJWTError:
        header = {}

    # exp/iat surfaced without re-verifying (verify_token already passed)
    try:
        unverified_claims = pyjwt.decode(
            token,
            options={"verify_signature": False},
        )
    except pyjwt.PyJWTError:
        unverified_claims = {}

    return {
        "authenticated": True,
        "subject": info.subject,
        "org": info.org,
        "namespaces": list(info.namespaces),
        "roles": [r.name for r in info.roles],
        "is_service_account": info.is_service_account,
        "kid": header.get("kid"),
        "exp": unverified_claims.get("exp"),
        "iat": unverified_claims.get("iat"),
    }


def attach(app: Any) -> None:
    app.include_router(auth_router)
