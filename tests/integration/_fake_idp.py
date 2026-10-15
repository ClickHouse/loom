"""A fake OIDC identity provider over real HTTP, for the tests that run Loom's
sign-in against one: discovery, an authorize endpoint that signs in `email`
without a form, the token endpoint (authorization code with PKCE, refresh
with rotation), JWKS and revocation.

Set `email` to choose who signs in next, and `deactivate(email)` to make the
IdP refuse that user's refresh tokens, as a real one does for a user disabled
there.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import jwt as pyjwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from jwt.algorithms import RSAAlgorithm

from tests.integration._eventually import wait_until
from tests.integration._ports import free_port

CLIENT_ID = "loom-oidc-client"
CLIENT_SECRET = "oidc-client-secret"
_KID = "fake-idp-key"


def _s256(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )


class FakeIdP:
    def __init__(self, email: str) -> None:
        self.email = email
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.deactivated: set[str] = set()
        self.revoked: list[str] = []
        self._codes: dict[str, dict[str, str]] = {}
        self._refresh: dict[str, str] = {}  # refresh token -> email
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def deactivate(self, email: str) -> None:
        self.deactivated.add(email)

    def _id_token(self, email: str, nonce: str) -> str:
        now = int(time.time())
        return pyjwt.encode(
            {
                "iss": self.url,
                "aud": CLIENT_ID,
                "iat": now,
                "exp": now + 300,
                "sub": "idp-" + hashlib.sha256(email.encode()).hexdigest()[:12],
                "email": email,
                "email_verified": True,
                "nonce": nonce,
            },
            self._key,
            algorithm="RS256",
            headers={"kid": _KID},
        )

    def _new_refresh(self, email: str) -> str:
        token = secrets.token_urlsafe(16)
        self._refresh[token] = email
        return token

    def app(self) -> FastAPI:
        app = FastAPI()
        jwk = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update(kid=_KID, use="sig", alg="RS256")

        @app.get("/.well-known/openid-configuration")
        async def discovery() -> JSONResponse:
            return JSONResponse(
                {
                    "issuer": self.url,
                    "authorization_endpoint": f"{self.url}/authorize",
                    "token_endpoint": f"{self.url}/token",
                    "revocation_endpoint": f"{self.url}/revoke",
                    "jwks_uri": f"{self.url}/jwks",
                    "scopes_supported": ["openid", "email", "profile", "offline_access"],
                }
            )

        @app.get("/jwks")
        async def jwks() -> JSONResponse:
            return JSONResponse({"keys": [jwk]})

        @app.get("/authorize")
        async def authorize(request: Request) -> RedirectResponse:
            q = request.query_params
            code = secrets.token_urlsafe(16)
            self._codes[code] = {
                "nonce": q.get("nonce", ""),
                "challenge": q.get("code_challenge", ""),
                "scope": q.get("scope", ""),
                "email": self.email,
            }
            params = {"code": code, "state": q.get("state", ""), "iss": self.url}
            return RedirectResponse(
                url=f"{q['redirect_uri']}?{urllib.parse.urlencode(params)}", status_code=302
            )

        @app.post("/token")
        async def token(request: Request) -> JSONResponse:
            form = {
                k: v[0] for k, v in urllib.parse.parse_qs((await request.body()).decode()).items()
            }
            if (form.get("client_id"), form.get("client_secret")) != (CLIENT_ID, CLIENT_SECRET):
                return JSONResponse({"error": "invalid_client"}, status_code=401)
            if form.get("grant_type") == "refresh_token":
                email = self._refresh.pop(form.get("refresh_token", ""), None)
                if email is None or email in self.deactivated:
                    return JSONResponse({"error": "invalid_grant"}, status_code=400)
                return JSONResponse(
                    {"access_token": "a", "refresh_token": self._new_refresh(email)}
                )
            grant = self._codes.pop(form.get("code", ""), None)
            if grant is None or _s256(form.get("code_verifier", "")) != grant["challenge"]:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            out: dict[str, Any] = {
                "access_token": "idp-access",
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": self._id_token(grant["email"], grant["nonce"]),
            }
            if "offline_access" in grant["scope"].split():
                out["refresh_token"] = self._new_refresh(grant["email"])
            return JSONResponse(out)

        @app.post("/revoke")
        async def revoke(request: Request) -> Response:
            form = urllib.parse.parse_qs((await request.body()).decode())
            token = form.get("token", [""])[0]
            self._refresh.pop(token, None)
            self.revoked.append(token)
            return Response(status_code=200)

        return app

    @contextmanager
    def running(self) -> Iterator[FakeIdP]:
        server = uvicorn.Server(
            uvicorn.Config(self.app(), host="127.0.0.1", port=self.port, log_level="warning")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            wait_until(
                lambda: httpx.get(f"{self.url}/jwks", timeout=1.0).status_code == 200,
                max_wait=10,
                interval=0.2,
                description="fake IdP",
            )
            yield self
        finally:
            server.should_exit = True
            thread.join(timeout=5)
