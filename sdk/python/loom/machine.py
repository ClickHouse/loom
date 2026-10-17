"""Tokens for a bot or a CI job registered with `loom bot create`: no `loom
login` and no long-lived secret. With LOOM_CLIENT_ID set, the SDK proves
either

- a key: it signs a short client assertion (private_key_jwt; needs
  `pip install clickhouse-loom[machine]`). The key is a PEM file
  (LOOM_CLIENT_KEY_FILE), the PEM itself (LOOM_CLIENT_KEY, as a secret
  manager injects it), or an AWS Secrets Manager secret holding it
  (LOOM_CLIENT_KEY_SECRET, an id or ARN; needs `clickhouse-loom[aws]`), read
  again for each token so a rotated key is picked up; or
- its CI job's own OIDC token: LOOM_WORKLOAD_TOKEN or LOOM_WORKLOAD_TOKEN_FILE,
  or asked of the CI platform when it offers one on request
  (ACTIONS_ID_TOKEN_REQUEST_URL), for LOOM_WORKLOAD_AUDIENCE,

and gets an hour-long token from /oauth/token, renewed before it runs out and
kept in memory only.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import httpx

from loom.credentials import _origin
from loom.errors import AuthInvalid, BackendUnavailable, LoomError

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
_RENEW_BEFORE_S = 300
_KEY_SOURCES = ("LOOM_CLIENT_KEY_FILE", "LOOM_CLIENT_KEY", "LOOM_CLIENT_KEY_SECRET")


def machine_login(server: str) -> MachineLogin | None:
    """The bot login the environment describes, or None."""
    client_id = os.environ.get("LOOM_CLIENT_ID", "").strip()
    if not client_id:
        return None
    given = [name for name in _KEY_SOURCES if os.environ.get(name, "").strip()]
    if len(given) > 1:
        raise AuthInvalid(f"bot {client_id!r}: set one of {', '.join(given)}, not several")
    return MachineLogin(
        server,
        client_id,
        key_source=(given[0], os.environ[given[0]].strip()) if given else None,
        audience=os.environ.get("LOOM_WORKLOAD_AUDIENCE", ""),
    )


class MachineLogin(httpx.Auth):
    """httpx auth that sends the bot's hour-long token, getting a new one
    before it expires and once more when the server answers 401."""

    def __init__(
        self,
        server: str,
        client_id: str,
        *,
        key_file: str = "",
        key_source: tuple[str, str] | None = None,
        audience: str = "",
    ) -> None:
        self.server = server.rstrip("/")
        self.profile = client_id  # how the transport names the credential
        # (env var, value): where the key comes from; key_file is the file one.
        self._key_source = key_source or (("LOOM_CLIENT_KEY_FILE", key_file) if key_file else None)
        self._audience = audience
        self._token, self._expires_at, self._renew_at, self._token_endpoint = "", 0.0, 0.0, ""
        self._lock = threading.Lock()

    def current_token(self) -> str:
        """The token in hand, got first if there is none: what the CLI reads
        the bot's org and namespaces from before its first request."""
        return self._token or self.token()

    def token(self, refused: str = "") -> str:
        with self._lock:
            if not self._token or self._token == refused or time.time() > self._renew_at:
                try:
                    self._token, self._expires_at = self._fetch()
                    # Five minutes before it runs out, or halfway for a token
                    # a short-lived CI proof capped.
                    life = self._expires_at - time.time()
                    self._renew_at = self._expires_at - min(_RENEW_BEFORE_S, life / 2)
                except (LoomError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
                    # The CI token ran out, Loom is unreachable or busy, a key
                    # file is mid-rotation, an answer is malformed: the token in
                    # hand is good until it runs out, and the next try waits a bit.
                    if self._token == refused or time.time() >= self._expires_at:
                        raise
                    self._renew_at = min(self._expires_at, time.time() + 30)
            return self._token

    def _fetch(self) -> tuple[str, float]:
        endpoint = f"{self.server}/oauth/token"
        if self._key_source:
            source, value = self._key_source
            form = {
                "grant_type": "client_credentials",
                "client_id": self.profile,
                "client_assertion_type": ASSERTION_TYPE,
                "client_assertion": self._assertion(
                    self._audience_of_assertion(), _key_pem(source, value), source
                ),
            }
        else:
            form = {
                "grant_type": TOKEN_EXCHANGE,
                "client_id": self.profile,
                "subject_token": _workload_token(self._audience),
                "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            }
        r = httpx.post(endpoint, data=form, timeout=30.0)
        try:
            body = r.json()
        except ValueError:
            body = {}  # a proxy's page, not Loom's answer
        if not isinstance(body, dict):
            body = {}
        if r.status_code == 429 or r.status_code >= 500:  # Keeper busy, the CI issuer away
            raise BackendUnavailable(f"bot {self.profile!r}: {r.status_code}; retry shortly")
        if r.status_code != 200 or "access_token" not in body:
            raise AuthInvalid(
                f"bot {self.profile!r}: {body.get('error', r.status_code)} "
                f"{body.get('error_description', '')}".strip()
            )
        return str(body["access_token"]), time.time() + float(body.get("expires_in", 3600))

    def _audience_of_assertion(self) -> str:
        """The token endpoint the server names itself by, which a bot reaching it
        by another (internal) URL must still sign for."""
        if not self._token_endpoint:
            r = httpx.get(f"{self.server}/.well-known/oauth-authorization-server", timeout=30.0)
            r.raise_for_status()
            self._token_endpoint = str(r.json()["token_endpoint"])
        return self._token_endpoint

    def _assertion(self, endpoint: str, pem: bytes, source: str) -> str:
        try:
            import jwt
            from cryptography.hazmat.primitives.asymmetric import ec, ed25519
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
        except ImportError as e:
            raise AuthInvalid("a bot key needs `pip install clickhouse-loom[machine]`") from e
        try:
            key = load_pem_private_key(pem, password=None)
        except ValueError as e:  # an env file's literal "\n"s, say
            raise AuthInvalid(f"{source}: not a PEM private key") from e
        alg = (
            "ES256"
            if isinstance(key, ec.EllipticCurvePrivateKey)
            else "EdDSA"
            if isinstance(key, ed25519.Ed25519PrivateKey)
            else "RS256"
        )
        now = int(time.time())
        claims = {
            "iss": self.profile,
            "sub": self.profile,
            "aud": endpoint,
            "iat": now,
            "exp": now + 120,
            "jti": uuid.uuid4().hex,
        }
        return jwt.encode(claims, key, algorithm=alg)  # type: ignore[arg-type]

    def _request_token(self, request: httpx.Request, refused: str = "") -> str:
        if _origin(str(request.url)) != _origin(self.server):
            raise AuthInvalid(
                f"refusing to send bot {self.profile!r}'s token to {request.url.host}"
            )
        return self.token(refused)

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        token = self._request_token(request)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._request_token(request, token)}"
            yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # The token request blocks; keep it off the loop.
        token = await asyncio.to_thread(self._request_token, request)
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            token = await asyncio.to_thread(self._request_token, request, token)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


def _key_pem(source: str, value: str) -> bytes:
    """The bot's private key, from where LOOM_CLIENT_KEY_* says."""
    if source == "LOOM_CLIENT_KEY_FILE":
        return Path(value).read_bytes()
    if source == "LOOM_CLIENT_KEY":
        return value.encode()
    try:
        import boto3
    except ImportError as e:
        raise AuthInvalid("LOOM_CLIENT_KEY_SECRET needs `pip install clickhouse-loom[aws]`") from e
    # An ARN names its region; a bare id takes the AWS SDK's default one.
    region = value.split(":")[3] if value.startswith("arn:") else None
    try:
        secret = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=value)
    except Exception as e:  # botocore's errors; the token in hand stays good
        raise BackendUnavailable(f"reading {value} from Secrets Manager: {e}") from e
    if "SecretString" not in secret:
        raise AuthInvalid(f"{value}: the key must be stored as text, not as binary")
    return str(secret["SecretString"]).encode()


def _workload_token(audience: str) -> str:
    """This CI job's OIDC token: given in the environment or a file (a platform
    that hands jobs one token, which must rotate it for jobs past an hour), or
    asked for anew."""
    given = os.environ.get("LOOM_WORKLOAD_TOKEN", "").strip()
    if given:
        return given
    path = os.environ.get("LOOM_WORKLOAD_TOKEN_FILE", "")
    if path:
        return Path(path).read_text().strip()
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    if not (url and bearer and audience):
        raise AuthInvalid(
            "LOOM_CLIENT_ID is set but there is no key (LOOM_CLIENT_KEY_FILE, LOOM_CLIENT_KEY "
            "or LOOM_CLIENT_KEY_SECRET) or CI token "
            "(LOOM_WORKLOAD_TOKEN, LOOM_WORKLOAD_TOKEN_FILE, or a CI job with "
            "LOOM_WORKLOAD_AUDIENCE)"
        )
    # The runner's URL carries its own query (an API version): the audience joins it.
    asked = httpx.URL(url).copy_merge_params({"audience": audience})
    r = httpx.get(str(asked), headers={"Authorization": f"Bearer {bearer}"}, timeout=30.0)
    r.raise_for_status()
    return str(r.json()["value"])
