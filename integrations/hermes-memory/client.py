"""Bounded HTTP calls to the released Loom memory API."""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from agent.secret_scope import get_secret
from hermes_cli.urllib_security import open_credentialed_url

MAX_RESPONSE_BYTES = 65_536
TIMEOUT_SECONDS = 5.0
ENV_FIELDS = {
    "base_url": "HERMES_LOOM_BASE_URL",
    "api_key": "HERMES_LOOM_API_KEY",
    "org": "HERMES_LOOM_ORG",
    "namespace": "HERMES_LOOM_NAMESPACE",
    "user_id": "HERMES_LOOM_USER_ID",
    "agent": "HERMES_LOOM_AGENT",
}


class LoomError(Exception):
    """A refusal or an uncertain HTTP outcome, without credential-bearing text."""

    def __init__(self, code: str, *, status: int | None = None, uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.status = status
        self.uncertain = uncertain

    def result(self) -> dict[str, Any]:
        result: dict[str, Any] = {"error": self.code, "retryable": False}
        if self.status is not None:
            result["http_status"] = self.status
        if self.uncertain:
            result["write_outcome"] = "unknown; verify in Loom before retrying"
        return result


def _base_url(raw: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
        host = parsed.hostname or ""
    except ValueError:
        raise ValueError("Invalid HERMES_LOOM_BASE_URL") from None
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if (
        not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
        or port == 0
        or any(char.isspace() or ord(char) < 32 for char in raw)
    ):
        raise ValueError("HERMES_LOOM_BASE_URL must be an HTTPS origin or loopback HTTP origin")
    return raw.rstrip("/")


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str = field(repr=False)
    org: str
    namespace: str
    user_id: str
    agent: str

    @classmethod
    def from_secrets(cls) -> Config:
        values = {key: (get_secret(env) or "").strip() for key, env in ENV_FIELDS.items()}
        values["agent"] = values["agent"] or "hermes"
        for key, value in values.items():
            if (
                not value
                or len(value) > 4096
                or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError(f"Set a non-empty {ENV_FIELDS[key]} without whitespace")
        for key in ("base_url", "api_key"):
            if not values[key].isascii():
                raise ValueError(f"Set an ASCII {ENV_FIELDS[key]}; use an IDNA host in URLs")
        for key in ("org", "namespace", "user_id", "agent"):
            if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,200}", values[key]) or values[key] == "-":
                raise ValueError(f"Set a named {ENV_FIELDS[key]}")
        values["base_url"] = _base_url(values["base_url"])
        return cls(**values)

    def identity(self) -> dict[str, str]:
        # Point facts keep the same key across native session switches.
        return {
            "org": self.org,
            "namespace": self.namespace,
            "agent": self.agent,
            "user_id": self.user_id,
            "session_id": "-",
        }


def mirrored(value: Any) -> bool:
    """Recognize the server's mirrored-source mark anywhere in its JSON reply."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("mirrored") is True:
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


class Client:
    def __init__(self, config: Config):
        self.config = config

    def post(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        writing = operation == "memory.set"
        request = urllib.request.Request(
            f"{self.config.base_url}/v1/{operation}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with open_credentialed_url(request, timeout=TIMEOUT_SECONDS) as response:
                if response.geturl() != request.full_url:
                    raise LoomError("REDIRECT_REFUSED", uncertain=writing)
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            try:
                with exc:
                    raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except (OSError, urllib.error.URLError, http.client.HTTPException):
                raw = b""  # Keep the known status when its optional diagnostic body cannot be read.
            raise self._http_error(exc.code, raw, writing=writing) from None
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            raise LoomError("CONNECTION_FAILED", uncertain=writing) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise LoomError("RESPONSE_TOO_LARGE", uncertain=writing)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise LoomError("INVALID_RESPONSE", uncertain=writing) from None
        if not isinstance(body, dict):
            raise LoomError("INVALID_RESPONSE", uncertain=writing)
        return body

    @staticmethod
    def _http_error(status: int, raw: bytes, *, writing: bool) -> LoomError:
        code = f"HTTP_{status}"
        if len(raw) <= MAX_RESPONSE_BYTES:
            try:
                body = json.loads(raw)
                detail = body.get("error", body.get("detail", {}))
                candidate = detail.get("code", "") if isinstance(detail, dict) else detail
                if isinstance(candidate, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", candidate):
                    code = candidate
            except (ValueError, UnicodeError, AttributeError, RecursionError):
                pass  # Error bodies are optional; never reflect server messages or credentials.
        return LoomError(code, status=status, uncertain=writing and status >= 500)
