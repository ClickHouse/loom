"""Fixed-provider OAuth wire contracts for document connections.

This boundary neither authorizes a Loom subject nor persists credentials. Its caller
owns browser state, account/source generations, credential sealing and revocation
ordering. A returned provider identity is metadata, never namespace authority.
No request retries or follows redirects, including token rotation and revocation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx

from server.clients._http import get_client
from server.clients._vendor import read_capped
from server.clients.notion import API_VERSION
from server.settings import settings

Kind = Literal["gdrive", "notion", "linear"]
_now = time.monotonic
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
_SCOPES = {"gdrive": frozenset({_DRIVE_SCOPE, "openid", "email"}), "linear": frozenset({"read"})}
_AUTHORIZE = {
    "gdrive": "https://accounts.google.com/o/oauth2/v2/auth",
    "notion": "https://api.notion.com/v1/oauth/authorize",
    "linear": "https://linear.app/oauth/authorize",
}
_TOKEN = {
    "gdrive": "https://oauth2.googleapis.com/token",
    "notion": "https://api.notion.com/v1/oauth/token",
    "linear": "https://api.linear.app/oauth/token",
}
_REVOKE = {
    "gdrive": "https://oauth2.googleapis.com/revoke",
    "notion": "https://api.notion.com/v1/oauth/revoke",
    "linear": "https://api.linear.app/oauth/revoke",
}


class OAuthError(RuntimeError):
    """Fixed failure codes only; provider bodies, URLs and requests are not retained."""


@dataclass(frozen=True, repr=False)
class Tokens:
    access_token: str
    refresh_token: str
    scopes: tuple[str, ...]
    expires_in: int | None

    def __repr__(self) -> str:
        return "Tokens(<redacted>)"


@dataclass(frozen=True, repr=False)
class AccountGrant:
    tokens: Tokens
    account_id: str
    display_email: str | None
    workspace_id: str | None = None

    def __repr__(self) -> str:
        return "AccountGrant(<redacted>)"


def _text(value: Any, *, maximum: int = 8192) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise OAuthError("invalid_response")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise OAuthError("invalid_response")
    return value


def _email(value: Any) -> str | None:
    return None if value is None else _text(value, maximum=512)


def _credential(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,8192}", value):
        raise OAuthError("invalid_response")
    return value


def _verifier(value: str | None) -> str:
    if value is None or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", value):
        raise OAuthError("invalid_pkce")
    return value


def _scopes(kind: Kind, value: Any) -> tuple[str, ...]:
    if kind == "notion":
        # An opaque introspection scope cannot prove registration capabilities.
        return ()
    if isinstance(value, str):
        values = value.split()
    elif isinstance(value, (tuple, list)) and all(isinstance(v, str) for v in value):
        values = list(value)
    else:
        raise OAuthError("scope_refused")
    actual = frozenset("email" if v == _EMAIL_SCOPE and kind == "gdrive" else v for v in values)
    if actual != _SCOPES[kind]:
        raise OAuthError("scope_refused")
    return tuple(sorted(actual))


@dataclass(frozen=True, repr=False)
class OAuthClient:
    kind: Kind
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    public_base_url: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.kind not in _AUTHORIZE or not self.client_id or not self.client_secret:
            raise OAuthError("not_configured")
        if any(c.isspace() or ord(c) < 32 for c in self.public_base_url):
            raise OAuthError("invalid_public_base")
        try:
            url = urlsplit(self.public_base_url)
            port = url.port
            valid = (
                url.scheme in {"https", "http"}
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and (port is None or 0 < port <= 65535)
                and (url.scheme == "https" or url.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        except ValueError:
            valid = False
        if not valid:
            raise OAuthError("invalid_public_base")

    def __repr__(self) -> str:
        return "OAuthClient(<redacted>)"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/v1/connect/{self.kind}/callback"

    def authorization_url(self, state: str, *, verifier: str | None = None) -> str:
        """The caller supplies its persisted browser-bound state and S256 verifier."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", state):
            raise OAuthError("invalid_state")
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": state,
        }
        if self.kind == "notion":
            if verifier is not None:
                raise OAuthError("unsupported_pkce")
            params["owner"] = "user"
        else:
            challenge = hashlib.sha256(_verifier(verifier).encode("ascii")).digest()
            params.update(
                code_challenge=base64.urlsafe_b64encode(challenge).rstrip(b"=").decode("ascii"),
                code_challenge_method="S256",
                scope=" ".join(sorted(_SCOPES[self.kind])),
            )
            if self.kind == "gdrive":
                params.update(
                    access_type="offline", include_granted_scopes="false", prompt="consent"
                )
            else:
                params["actor"] = "user"
        return f"{_AUTHORIZE[self.kind]}?{urlencode(params)}"

    async def _request(
        self, method: str, url: str, *, deadline: float, empty: bool = False, **kw: Any
    ) -> dict[str, Any]:
        remaining = deadline - _now()
        if not math.isfinite(remaining) or remaining <= 0:
            raise OAuthError("deadline")
        remaining = min(remaining, settings.knowledge_connected_oauth_timeout_s)
        try:
            async with asyncio.timeout(remaining):
                async with get_client().stream(
                    method, url, timeout=remaining, follow_redirects=False, **kw
                ) as response:
                    if response.status_code != 200:
                        # Do not read provider error bodies, which can echo credentials.
                        raise OAuthError("provider_refused")
                    if empty:
                        return {}
                    raw = await read_capped(
                        response, settings.knowledge_connected_oauth_response_max_bytes
                    )
        except (httpx.RequestError, TimeoutError):
            raise OAuthError("unavailable") from None
        if raw is None:
            raise OAuthError("response_too_large")
        try:
            answer = json.loads(raw)
        except (ValueError, RecursionError):
            raise OAuthError("invalid_response") from None
        if not isinstance(answer, dict) or answer.get("error") or answer.get("errors"):
            raise OAuthError("invalid_response")
        return answer

    def _token_request(self, fields: dict[str, str]) -> dict[str, Any]:
        if self.kind == "notion":
            return {
                "json": fields,
                "auth": httpx.BasicAuth(self.client_id, self.client_secret),
                "headers": {"Notion-Version": API_VERSION},
            }
        return {
            "data": {**fields, "client_id": self.client_id, "client_secret": self.client_secret}
        }

    def _tokens(
        self,
        answer: dict[str, Any],
        *,
        previous_refresh: str | None = None,
        previous_scopes: tuple[str, ...] | None = None,
    ) -> Tokens:
        access = _credential(answer.get("access_token"))
        token_type = answer.get("token_type")
        if not (token_type is None and self.kind == "notion") and (
            not isinstance(token_type, str) or token_type.lower() != "bearer"
        ):
            raise OAuthError("invalid_response")
        refresh = answer.get("refresh_token")
        if self.kind == "gdrive" and refresh is None:
            refresh = previous_refresh
        refresh = _credential(refresh)
        scopes = _scopes(self.kind, answer.get("scope", previous_scopes))
        expiry = answer.get("expires_in")
        if not (expiry is None and self.kind == "notion") and (
            type(expiry) is not int or expiry <= 0
        ):
            raise OAuthError("invalid_response")
        return Tokens(access, refresh, scopes, expiry)

    async def exchange(
        self, code: str, *, verifier: str | None = None, deadline: float
    ) -> AccountGrant:
        """Exchange once, then resolve provider identity under the same deadline."""
        fields = {
            "grant_type": "authorization_code",
            "code": _text(code),
            "redirect_uri": self.redirect_uri,
        }
        if self.kind != "notion":
            fields["code_verifier"] = _verifier(verifier)
        elif verifier is not None:
            raise OAuthError("unsupported_pkce")
        answer = await self._request(
            "POST", _TOKEN[self.kind], deadline=deadline, **self._token_request(fields)
        )
        tokens = self._tokens(answer)
        if self.kind == "notion":
            owner = answer.get("owner")
            user = (
                owner.get("user")
                if isinstance(owner, dict) and owner.get("type") == "user"
                else None
            )
            if not isinstance(user, dict):
                raise OAuthError("invalid_response")
            person = user.get("person")
            email = person.get("email") if isinstance(person, dict) else None
            return AccountGrant(
                tokens,
                _text(user.get("id"), maximum=1024),
                _email(email),
                _text(answer.get("workspace_id"), maximum=1024),
            )
        headers = {"Authorization": f"Bearer {tokens.access_token}"}
        if self.kind == "gdrive":
            user = await self._request(
                "GET",
                "https://openidconnect.googleapis.com/v1/userinfo",
                deadline=deadline,
                headers=headers,
            )
            return AccountGrant(
                tokens, _text(user.get("sub"), maximum=1024), _email(user.get("email"))
            )
        identity = await self._request(
            "POST",
            "https://api.linear.app/graphql",
            deadline=deadline,
            headers=headers,
            json={"query": "query { viewer { id name email } }"},
        )
        data = identity.get("data")
        user = data.get("viewer") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            raise OAuthError("invalid_response")
        return AccountGrant(tokens, _text(user.get("id"), maximum=1024), _email(user.get("email")))

    async def refresh(
        self, refresh_token: str, *, scopes: tuple[str, ...], deadline: float
    ) -> Tokens:
        """No retry: the caller serializes and commits this account generation."""
        scopes = _scopes(self.kind, scopes)
        fields = {"grant_type": "refresh_token", "refresh_token": _credential(refresh_token)}
        answer = await self._request(
            "POST", _TOKEN[self.kind], deadline=deadline, **self._token_request(fields)
        )
        return self._tokens(answer, previous_refresh=refresh_token, previous_scopes=scopes)

    async def revoke(self, token: str, *, refresh: bool, deadline: float) -> None:
        """Call only after local inactivation; no claim of whole-grant revocation."""
        token = _credential(token)
        if self.kind == "notion":
            if refresh:
                raise OAuthError("unsupported_refresh_revocation")
            kw = self._token_request({"token": token})
        else:
            fields = {"token": token}
            if self.kind == "linear":
                fields["token_type_hint"] = "refresh_token" if refresh else "access_token"
            kw = {"data": fields}
        await self._request("POST", _REVOKE[self.kind], deadline=deadline, empty=True, **kw)
