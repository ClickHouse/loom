"""Serial same-origin cell administration without ambient server credentials."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
import ssl
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from server.cli import cell_each_state as history

if TYPE_CHECKING:
    import httpx

    from server.cli.cell_each import Command

_ACTION_LIMIT = 2 * 1024 * 1024  # not a setting: the server contract fixes the maximum result size
_CONTEXT_KEYS = {"version", "expected_cell", "expected_deployment_id", "expected_bot"}
_ACTION_CONTEXT = _CONTEXT_KEYS | {"run_id", "attempt", "command_digest"}
_LIMITS = {"action_timeout_s": 30, "max_body_bytes": 65536, "max_response_bytes": _ACTION_LIMIT}
_THUMBPRINT_MEMBERS = {
    "EC": ("crv", "kty", "x", "y"),
    "RSA": ("e", "kty", "n"),
    "OKP": ("crv", "kty", "x"),
}
_REFUSAL_CODES = {
    "automation_required",
    "security_unavailable",
    "short_token_required",
    "cell_unavailable",
    "cell_identity_changed",
    "automation_unprovisioned",
    "automation_identity_changed",
    "automation_key_changed",
    "automation_revoked",
    "invalid_arguments",
    "action_unavailable",
}


class Refused(ValueError):
    """A locally diagnosed input, identity or preflight failure."""


def _refuse() -> Refused:
    return Refused("Cell administration refused")


def _encoded(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _refuse() from None


def _object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=history._pairs,
            parse_constant=history._constant,
            parse_float=history._float,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise _refuse() from None
    if type(value) is not dict:
        raise _refuse()
    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        if type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise _refuse() from None
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is dict:
            pending.extend(item.keys())
            pending.extend(item.values())
    return value


def _version(value: Any) -> bool:
    return type(value) is int and value == 1


def _origin(value: str) -> str:
    if type(value) is not str or len(value) > 512 or not value.isascii():
        raise _refuse()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise _refuse() from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or value != f"https://{parsed.netloc}"
        or parsed.netloc != parsed.netloc.lower()
        or parsed.netloc.endswith(":")
        or port == 443
    ):
        raise _refuse()
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or any(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) is None
            for part in host.split(".")
        ):
            raise _refuse() from None
    return value


def _private_path(value: Any) -> Path:
    if type(value) is not str or not value or len(value.encode()) > 4096 or "\0" in value:
        raise _refuse()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise _refuse()
    return path


def _manifest(path: Path) -> dict[str, Any]:
    value = _object(history.read_private(path, 1024 * 1024))
    if value.keys() not in (
        {"version", "router_token_file", "keys"},
        {"version", "router_token_file", "keys", "ca_file"},
    ):
        raise _refuse()
    if (
        not _version(value["version"])
        or type(value["keys"]) is not list
        or not 1 <= len(value["keys"]) <= 4096
    ):
        raise _refuse()
    value["router_token_file"] = _private_path(value["router_token_file"])
    if "ca_file" in value:
        value["ca_file"] = _private_path(value["ca_file"])
    keys: dict[tuple[str, str], Path] = {}
    for row in value["keys"]:
        if type(row) is not dict or row.keys() != {"cell", "deployment_id", "key_file"}:
            raise _refuse()
        if not history._text(row["cell"], 32) or not history._text(row["deployment_id"], 256):
            raise _refuse()
        identity = (row["cell"], row["deployment_id"])
        if identity in keys:
            raise _refuse()
        keys[identity] = _private_path(row["key_file"])
    value["keys"] = keys
    return value


def _router_token(manifest: dict[str, Any]) -> str:
    try:
        token = history.read_private(manifest["router_token_file"], 4096).decode("ascii").strip()
    except UnicodeError:
        raise _refuse() from None
    if re.fullmatch(r"lco_[0-9a-f]{128}", token) is None:
        raise _refuse()
    return token


def _tls(manifest: dict[str, Any]) -> ssl.SSLContext:
    if "ca_file" not in manifest:
        return ssl.create_default_context()
    try:
        return ssl.create_default_context(
            cadata=history.read_private(manifest["ca_file"], 1024 * 1024).decode("ascii")
        )
    except (ValueError, UnicodeError, ssl.SSLError):
        raise _refuse() from None


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limit: int,
    deadline: float,
    bearer: str = "",
    body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
    action: bool = False,
) -> tuple[int, dict[str, Any]]:
    remaining = min(35.0 if action else 15.0, deadline - time.monotonic())
    if remaining <= 0:
        raise _refuse()
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    # This serial client never carries one origin's cookies to another request.
    client.cookies.clear()
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    arguments: dict[str, Any] = {"headers": headers, "timeout": remaining}
    if body is not None:
        encoded = _encoded(body)
        if len(encoded) > 65536:
            raise _refuse()
        arguments["content"] = encoded
        headers["Content-Type"] = "application/json"
    if form is not None:
        arguments["data"] = form
    async with asyncio.timeout(remaining):
        # A local deadline stops admission, not a remote handler already given work.
        async with client.stream(method, url, **arguments) as response:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise _refuse()
            length = response.headers.get("content-length")
            if length is not None and (
                not length.isascii()
                or not length.isdecimal()
                or len(length) > 12
                or int(length) > limit
            ):
                raise _refuse()
            raw = bytearray()
            async for chunk in response.aiter_raw(chunk_size=16384):
                if len(raw) + len(chunk) > limit:
                    raise _refuse()
                raw.extend(chunk)
            return response.status_code, _object(bytes(raw))


def _target(value: dict[str, Any]) -> dict[str, Any]:
    history._target(value)
    if re.fullmatch(r"[a-z0-9-]{1,32}", value["cell"]) is None:
        raise _refuse()
    _origin(value["origin"])
    return value


async def _census(
    client: httpx.AsyncClient, router: str, token: str, deadline: float
) -> list[dict[str, Any]]:
    status, answer = await _request(
        client,
        "GET",
        router + "/cells/v1/operator/cells",
        bearer=token,
        limit=4 * 1024 * 1024,
        deadline=deadline,
    )
    if status != 200 or answer.keys() != {"version", "cells"} or not _version(answer["version"]):
        raise _refuse()
    rows = answer["cells"]
    if type(rows) is not list or not 1 <= len(rows) <= 4096:
        raise _refuse()
    cells: set[str] = set()
    deployments: set[str] = set()
    origins: set[str] = set()
    for row in rows:
        _target(row)
        if row["cell"] in cells or row["deployment_id"] in deployments or row["origin"] in origins:
            raise _refuse()
        cells.add(row["cell"])
        deployments.add(row["deployment_id"])
        origins.add(row["origin"])
    return sorted(rows, key=lambda row: row["cell"])


async def _point(
    client: httpx.AsyncClient, router: str, token: str, target: dict[str, Any], deadline: float
) -> None:
    endpoint = (
        router + "/cells/v1/operator/cells/" + quote(target["cell"], safe="") + "/administration"
    )
    status, answer = await _request(
        client, "GET", endpoint, bearer=token, limit=256 * 1024, deadline=deadline
    )
    if (
        status != 200
        or answer.keys() != {"version", "cell"}
        or not _version(answer["version"])
        or _target(answer["cell"]) != target
    ):
        raise _refuse()


def _assertion(pem: bytes, target: dict[str, Any], endpoint: str) -> str:
    automation, recovery = target["automation"], target["recovery"]
    if automation["thumbprint"] == recovery["thumbprint"] or automation["name"] == recovery["name"]:
        raise _refuse()
    return bot_assertion(
        pem, name=automation["name"], thumbprint=automation["thumbprint"], endpoint=endpoint
    )


def bot_assertion(pem: bytes, *, name: str, thumbprint: str, endpoint: str) -> str:
    """Sign for one explicitly selected bot after checking its public key identity."""
    import jwt
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from jwt.algorithms import ECAlgorithm, OKPAlgorithm, RSAAlgorithm

    if (
        not history._text(name, 256)
        or type(thumbprint) is not str
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", thumbprint) is None
        or type(endpoint) is not str
        or not endpoint.endswith("/oauth/token")
    ):
        raise _refuse()
    _origin(endpoint[: -len("/oauth/token")])
    try:
        key = load_pem_private_key(pem, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise _refuse() from None
    if isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1":
        algorithm, jwk = "ES256", ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    elif isinstance(key, ed25519.Ed25519PrivateKey):
        algorithm, jwk = "EdDSA", OKPAlgorithm.to_jwk(key.public_key(), as_dict=True)
    elif isinstance(key, rsa.RSAPrivateKey) and key.key_size >= 2048:
        algorithm, jwk = "RS256", RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    else:
        raise _refuse()
    members = {name: jwk[name] for name in _THUMBPRINT_MEMBERS[jwk["kty"]]}
    actual_thumbprint = (
        base64.urlsafe_b64encode(hashlib.sha256(_encoded(members)).digest()).rstrip(b"=").decode()
    )
    if actual_thumbprint != thumbprint:
        raise _refuse()
    now = int(time.time())
    claims = {
        "iss": name,
        "sub": name,
        "aud": endpoint,
        "iat": now,
        "exp": now + 120,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": thumbprint})


def _access(answer: dict[str, Any], target: dict[str, Any], issued: float) -> tuple[str, float]:
    return bot_access(answer, target["automation"]["name"], issued)


def bot_access(answer: dict[str, Any], name: str, issued: float) -> tuple[str, float]:
    """Bound the normal token response for an explicitly selected bot."""
    import jwt

    if (
        not history._text(name, 256)
        or not {"access_token", "token_type", "expires_in"} <= answer.keys()
    ):
        raise _refuse()
    token, lifetime = answer["access_token"], answer["expires_in"]
    if (
        type(token) is not str
        or not 1 <= len(token) <= 16384
        or any(ord(char) < 33 or ord(char) > 126 for char in token)
        or answer["token_type"] != "Bearer"
        or type(lifetime) is not int
        or not 30 <= lifetime <= 60
    ):
        raise _refuse()
    try:
        claims = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except jwt.PyJWTError:
        raise _refuse() from None
    # TLS and the server's normal verification supply authority, never this unverified view.
    expires, starts = claims.get("exp"), claims.get("iat")
    if (
        type(expires) is not int
        or type(starts) is not int
        or not 0 < expires - starts <= 60
        or not 30 <= expires - time.time() <= 60
        or claims.get("sub") != "sa:" + name
    ):
        raise _refuse()
    end = min(issued + lifetime, time.monotonic() + expires - time.time())
    if end - time.monotonic() < 30:
        raise _refuse()
    return token, end


async def _token(
    client: httpx.AsyncClient, target: dict[str, Any], key_file: Path, deadline: float
) -> tuple[str, float]:
    origin = target["origin"]
    status, metadata = await _request(
        client,
        "GET",
        origin + "/.well-known/oauth-authorization-server",
        limit=16384,
        deadline=deadline,
    )
    endpoint = origin + "/oauth/token"
    if (
        status != 200
        or metadata.get("token_endpoint") != endpoint
        or metadata.get("issuer", origin) != origin
    ):
        raise _refuse()
    pem = history.read_private(key_file, 65536)
    assertion = _assertion(pem, target, endpoint)
    issued = time.monotonic()
    status, answer = await _request(
        client,
        "POST",
        endpoint,
        limit=16384,
        deadline=deadline,
        form={
            "grant_type": "client_credentials",
            "client_id": target["automation"]["name"],
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
            "expires_in": "60",
        },
    )
    if status != 200:
        raise _refuse()
    return _access(answer, target, issued)


def _context(target: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    value = {
        "version": 1,
        "expected_cell": target["cell"],
        "expected_deployment_id": target["deployment_id"],
        "expected_bot": target["automation"]["name"],
    }
    if state is not None:
        value.update(
            run_id=state["run_id"],
            attempt=state["outcomes"][target["cell"]]["attempt"],
            command_digest=state["command"]["digest"],
        )
    return value


def _same_context(actual: Any, expected: dict[str, Any]) -> bool:
    return (
        type(actual) is dict
        and actual.keys() == expected.keys()
        and all(
            type(actual[key]) is type(value) and actual[key] == value
            for key, value in expected.items()
        )
    )


async def _capability(
    client: httpx.AsyncClient, target: dict[str, Any], token: str, action: str, deadline: float
) -> None:
    context = _context(target)
    status, answer = await _request(
        client,
        "POST",
        target["origin"] + "/v1/admin/cell.capabilities",
        bearer=token,
        body={"context": context},
        limit=8192,
        deadline=deadline,
    )
    if (
        status != 200
        or answer.keys() != {"version", "context", "actions", "limits"}
        or not _version(answer["version"])
        or not _same_context(answer["context"], context)
        or answer["limits"] != _LIMITS
    ):
        raise _refuse()
    actions = answer["actions"]
    if (
        type(actions) is not list
        or len(actions) > 12
        or any(type(item) is not str or item not in history._ACTIONS for item in actions)
        or len(set(actions)) != len(actions)
        or action not in actions
    ):
        raise _refuse()


def _completion(
    answer: dict[str, Any], target: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    required = {
        "version",
        "status",
        "context",
        "action",
        "started_audit",
        "completed_audit",
        "result",
    }
    if (
        answer.keys() != required
        or not _version(answer["version"])
        or answer["status"] != "completed"
        or not _same_context(answer["context"], _context(target, state))
        or answer["action"] != state["command"]["action"]
    ):
        raise _refuse()
    for key in ("started_audit", "completed_audit"):
        if type(answer[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", answer[key]) is None:
            raise _refuse()
    _result(answer["action"], answer["result"], state["command"]["arguments"])
    value = {
        key: answer[key]
        for key in ("version", "context", "action", "started_audit", "completed_audit")
    }
    value["result_digest"] = hashlib.sha256(_encoded(answer["result"])).hexdigest()
    if len(_encoded(value)) > 2048:
        raise _refuse()
    return value


def _setting(row: Any) -> None:
    keys = {"name", "env", "value", "default", "source", "live", "description"}
    if type(row) is not dict or row.keys() != keys or len(_encoded(row)) > 16384:
        raise _refuse()
    if (
        not history._text(row["name"], 256)
        or not history._text(row["env"], 512)
        or type(row["description"]) is not str
        or row["live"] is not True
        or type(row["source"]) is not str
        or row["source"]
        not in {"override", "dashboard", "env", "env_file", "settings_file", "inferred", "default"}
    ):
        raise _refuse()


def _settings_result(action: str, value: dict[str, Any], arguments: dict[str, Any]) -> None:
    if action == "settings_set":
        if value.keys() != {"ok", "setting"} or value["ok"] is not True:
            raise _refuse()
        _setting(value["setting"])
        if value["setting"]["name"] != arguments["name"]:
            raise _refuse()
        return
    field = "settings" if action == "settings_list" else "changes"
    maximum = 4096 if field == "settings" else arguments["limit"]
    if value.keys() != {field} or type(value[field]) is not list or len(value[field]) > maximum:
        raise _refuse()
    if field == "changes" and len(_encoded(value)) > 512 * 1024:
        raise _refuse()
    names: set[str] = set()
    for row in value[field]:
        if field == "settings":
            _setting(row)
            if not row["name"].startswith(arguments["prefix"]) or row["name"] in names:
                raise _refuse()
            names.add(row["name"])
        else:
            keys = {"ts", "actor", "name", "value", "previous", "previous_source", "cleared"}
            if (
                type(row) is not dict
                or row.keys() != keys
                or len(_encoded(row)) > 16384
                or row["name"] != arguments["name"]
                or not history._text(row["actor"], 512)
                or not history._text(row["ts"], 64)
                or type(row["cleared"]) is not bool
                or (
                    row["previous_source"] is not None
                    and not history._text(row["previous_source"], 32)
                )
            ):
                raise _refuse()


def _rate_result(value: dict[str, Any], arguments: dict[str, Any]) -> None:
    keys = {"ok", "scope", "key", "op_class", "capacity", "rate", "cleared"}
    if (
        value.keys() != keys
        or value["ok"] is not True
        or value["scope"] != "namespace"
        or value["key"] != arguments["namespace"]
        or value["op_class"] != arguments["op_class"]
    ):
        raise _refuse()
    if arguments["clear"]:
        if (
            value["cleared"] is not True
            or value["capacity"] is not None
            or value["rate"] is not None
        ):
            raise _refuse()
    elif value["cleared"] is not None or any(
        type(value[key]) not in {int, float} or value[key] != arguments[key]
        for key in ("capacity", "rate")
    ):
        raise _refuse()


def _budget_result(value: dict[str, Any], arguments: dict[str, Any]) -> None:
    keys = {"ok", "op", "budget_ms", "cleared", "consumer", "budget"}
    if (
        value.keys() != keys
        or value["ok"] is not True
        or value["op"] != arguments["op"]
        or value["consumer"] != arguments["consumer"]
    ):
        raise _refuse()
    if arguments["clear"]:
        if (
            value["cleared"] is not True
            or value["budget_ms"] is not None
            or value["budget"] is not None
        ):
            raise _refuse()
        return
    expected = arguments["budget"] if arguments["budget"] is not None else arguments["budget_ms"]
    if (
        value["cleared"] is not None
        or value["budget_ms"] != arguments["budget_ms"]
        or type(value["budget_ms"]) is not type(arguments["budget_ms"])
        or value["budget"] != (expected if arguments["consumer"] is not None else None)
        or type(value["budget"])
        is not type(expected if arguments["consumer"] is not None else None)
    ):
        raise _refuse()


def _timestamp(value: Any) -> None:
    if not history._text(value, 64) or "T" not in value:
        raise _refuse()
    try:
        datetime.fromisoformat(value)
    except ValueError:
        raise _refuse() from None


def _signing_result(action: str, value: dict[str, Any], arguments: dict[str, Any]) -> None:
    if action == "signing_keys_retire":
        if (
            value.keys() != {"kid", "retired"}
            or value["kid"] != arguments["kid"]
            or type(value["retired"]) is not bool
        ):
            raise _refuse()
        return
    if action == "signing_keys_rotate":
        if (
            value.keys() != {"kid", "algorithm", "activate_at"}
            or not history._text(value["kid"], 256)
            or value["algorithm"] != arguments["algorithm"]
        ):
            raise _refuse()
        _timestamp(value["activate_at"])
        return
    if value.keys() != {"keys"} or type(value["keys"]) is not list or len(value["keys"]) > 128:
        raise _refuse()
    kids: set[str] = set()
    for row in value["keys"]:
        if (
            type(row) is not dict
            or row.keys() != {"kid", "algorithm", "created_at", "activate_at", "retired_at"}
            or not history._text(row["kid"], 256)
            or row["kid"] in kids
            or type(row["algorithm"]) is not str
            or row["algorithm"] not in {"HS256", "RS256"}
            or len(_encoded(row)) > 16384
        ):
            raise _refuse()
        kids.add(row["kid"])
        _timestamp(row["created_at"])
        _timestamp(row["activate_at"])
        if row["retired_at"] is not None:
            _timestamp(row["retired_at"])


def _policy_text(value: Any, maximum: int, *, empty: bool = False) -> bool:
    return type(value) is str and (empty or bool(value)) and len(value.encode("utf-8")) <= maximum


def _blacklist_result(action: str, value: dict[str, Any], arguments: dict[str, Any]) -> None:
    if action == "blacklist_global_add":
        if (
            value.keys() != {"pattern_id", "created_at"}
            or value["pattern_id"] != arguments["pattern_id"]
        ):
            raise _refuse()
        _timestamp(value["created_at"])
        return
    if (
        value.keys() != {"rows", "truncated"}
        or type(value["rows"]) is not list
        or len(value["rows"]) > arguments["limit"]
        or type(value["truncated"]) is not bool
    ):
        raise _refuse()
    identities: set[str] = set()
    for row in value["rows"]:
        if (
            type(row) is not dict
            or row.keys()
            != {
                "pattern_id",
                "pattern",
                "pattern_type",
                "reason",
                "created_by",
                "hit_count",
                "last_hit_at",
                "created_at",
            }
            or not history._uuid(row["pattern_id"])
            or row["pattern_id"] in identities
            or not _policy_text(row["pattern"], 32768)
            or type(row["pattern_type"]) is not str
            or row["pattern_type"] not in {"substring", "regex"}
            or not _policy_text(row["reason"], 32768, empty=True)
            or not _policy_text(row["created_by"], 32768, empty=True)
            or type(row["hit_count"]) is not int
            or not 0 <= row["hit_count"] <= 2**64 - 1
        ):
            raise _refuse()
        identities.add(row["pattern_id"])
        _timestamp(row["created_at"])
        if row["last_hit_at"] is not None:
            _timestamp(row["last_hit_at"])


def _subject_result(value: dict[str, Any], arguments: dict[str, Any]) -> None:
    if (
        value.keys()
        != {"subject", "recorded", "already_revoked", "outright", "cutoff", "sessions", "tokens"}
        or value["subject"] != arguments["subject"].strip()
        or value["recorded"] is not True
        or type(value["already_revoked"]) is not bool
        or value["outright"] is not True
        or value["cutoff"] is not None
        or type(value["sessions"]) is not int
        or value["sessions"] < 0
        or type(value["tokens"]) is not int
        or value["tokens"] < 0
    ):
        raise _refuse()


def _result(action: str, value: Any, arguments: dict[str, Any]) -> None:
    if type(value) is not dict or len(_encoded(value)) > _ACTION_LIMIT:
        raise _refuse()
    if action in {"settings_list", "settings_set", "settings_history"}:
        _settings_result(action, value, arguments)
    elif action == "ratelimit_namespace_set":
        _rate_result(value, arguments)
    elif action == "budget_set":
        _budget_result(value, arguments)
    elif action in {"signing_keys_list", "signing_keys_rotate", "signing_keys_retire"}:
        _signing_result(action, value, arguments)
    elif action in {"blacklist_global_add", "blacklist_global_list"}:
        _blacklist_result(action, value, arguments)
    elif action == "subject_revoke":
        _subject_result(value, arguments)
    else:
        # An unimplemented adapter cannot become completed through an arbitrary result.
        raise _refuse()


def _no_action(answer: dict[str, Any], target: dict[str, Any], state: dict[str, Any]) -> str | None:
    required = {"version", "status", "phase", "context", "action", "code"}
    if (
        answer.keys() == required
        and _version(answer["version"])
        and answer["status"] == "refused"
        and _same_context(answer["context"], _context(target, state))
        and answer["action"] == state["command"]["action"]
        and answer["phase"] == "before_business"
        and type(answer["code"]) is str
        and answer["code"] in _REFUSAL_CODES
    ):
        return answer["code"].upper()
    return None


def _adopt(
    state: dict[str, Any] | None,
    command: Command | None,
    targets: list[dict[str, Any]],
    router: str,
    path: Path,
) -> dict[str, Any]:
    if state is None:
        if command is None:
            raise _refuse()
        state = history.new_state(command, targets=targets, run_id=str(uuid.uuid4()))
        state["router"] = router
        history.write_state(path, state, exclusive=True)
        return state
    if history.has_uncertainty(state) or state["router"] != router or command is not None:
        raise _refuse()
    current = {target["cell"]: target for target in targets}
    if any(current.get(target["cell"]) != target for target in state["targets"]):
        raise _refuse()
    old = {target["cell"] for target in state["targets"]}
    state["targets"] = targets
    for target in targets:
        if target["cell"] not in old:
            state["outcomes"][target["cell"]] = {
                "status": "pending",
                "attempt": 0,
                "code": "",
                "completion": None,
            }
    history.write_state(path, state)
    return state


def _mark(
    state: dict[str, Any],
    target: dict[str, Any],
    status: str,
    *,
    code: str = "",
    completion: dict[str, Any] | None = None,
) -> None:
    outcome = state["outcomes"][target["cell"]]
    if outcome["status"] in {"pending", "known-no-action"}:
        outcome["attempt"] += 1
    outcome.update(status=status, code=code, completion=completion)


async def _cell(
    client: httpx.AsyncClient,
    target: dict[str, Any],
    state: dict[str, Any],
    manifest: dict[str, Any],
    router: str,
    router_token: str,
    path: Path,
    deadline: float,
) -> bool:
    try:
        await _point(client, router, router_token, target, deadline)
        token, token_end = await _token(
            client, target, manifest["keys"][(target["cell"], target["deployment_id"])], deadline
        )
        await _capability(client, target, token, state["command"]["action"], deadline)
        if min(token_end, deadline) - time.monotonic() < 35:
            raise _refuse()
        preview = _context(target, state)
        preview["attempt"] += 1
        if len(_encoded({"context": preview, **state["command"]["arguments"]})) > 65536:
            raise _refuse()
    except Exception:  # noqa: BLE001 — preflight has invoked no administration business helper
        _mark(state, target, "known-no-action", code="PREFLIGHT_REFUSED")
        history.write_state(path, state)
        print(target["cell"] + " known-no-action PREFLIGHT_REFUSED", file=sys.stderr)
        return False
    _mark(state, target, "dispatching")
    history.write_state(path, state)
    body = {"context": _context(target, state), **state["command"]["arguments"]}
    try:
        status, answer = await _request(
            client,
            "POST",
            target["origin"] + "/v1/admin/cell." + state["command"]["action"],
            bearer=token,
            body=body,
            limit=_ACTION_LIMIT,
            deadline=min(deadline, time.monotonic() + 35),
            action=True,
        )
        code = _no_action(answer, target, state)
        if code is None and status != 200:
            raise _refuse()
        completion = _completion(answer, target, state) if code is None else None
    except Exception:  # noqa: BLE001 — every post-send failure must retain uncertain completion
        outcome_status = "UNSETTLED" if state["command"]["mutating"] else "known-no-action"
        code = "COMPLETION_UNKNOWN" if state["command"]["mutating"] else "READ_INCOMPLETE"
        _mark(state, target, outcome_status, code=code)
        history.write_state(path, state)
        print(target["cell"] + " " + outcome_status + " " + code, file=sys.stderr)
        return False
    if code is not None:
        _mark(state, target, "known-no-action", code=code)
        history.write_state(path, state)
        print(target["cell"] + " known-no-action " + code, file=sys.stderr)
        return False
    _mark(state, target, "completed", completion=completion)
    history.write_state(path, state)
    print(target["cell"] + " completed")
    if not state["command"]["mutating"]:
        print(json.dumps(answer["result"], sort_keys=True, ensure_ascii=True, allow_nan=False))
    return True


async def _execute(
    router: str,
    credentials: Path,
    state_path: Path,
    command: Command | None,
    state: dict[str, Any] | None,
) -> int:
    import httpx

    if state is not None:
        history.resume_read(state_path, state)
    if state is not None and history.has_uncertainty(state):
        print("UNSETTLED: an earlier action has no confirmed completion", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 3600
    router = _origin(router)
    manifest = _manifest(credentials)
    token = _router_token(manifest)
    transport = httpx.AsyncHTTPTransport(verify=_tls(manifest), retries=0, trust_env=False)
    async with httpx.AsyncClient(
        transport=transport, trust_env=False, follow_redirects=False, timeout=15.0
    ) as client:
        targets = await _census(client, router, token, deadline)
        if any((row["cell"], row["deployment_id"]) not in manifest["keys"] for row in targets):
            raise _refuse()
        state = _adopt(state, command, targets, router, state_path)
        for target in targets:
            if state["outcomes"][target["cell"]]["status"] == "completed":
                continue
            if time.monotonic() >= deadline:
                return 1
            if not await _cell(
                client, target, state, manifest, router, token, state_path, deadline
            ):
                return 1
        final = await _census(client, router, token, deadline)
        if final != targets or any(
            row["status"] != "completed" for row in state["outcomes"].values()
        ):
            raise _refuse()
    return 0


def execute(
    *,
    router: str,
    credentials: Path,
    state_path: Path,
    command: Command | None,
    state: dict[str, Any] | None,
) -> int:
    """Run once under the caller's lock, without replaying a sent action.

    DNS, regular-file I/O and cancellation retain the normal runtime's scheduling
    limits. A local timeout supplies no remote write or process-quiescence proof.
    """
    try:
        return asyncio.run(_execute(router, credentials, state_path, command, state))
    except Exception:  # noqa: BLE001 — custody and transport failures must never print private details
        print("Cell administration stopped without complete confirmation", file=sys.stderr)
        return 1
