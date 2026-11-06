"""How the CLI's HTTP clients of a running server find it and authenticate
(the base URL, the token, the token's unverified claims), and how they
report a failed call."""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://localhost:7777"
# Where the local token lives, as help text prints it.
TOKEN_FILE = "~/.loom/token"
# The source label for values read from the client config file.
CONFIG_SOURCE = "~/.loom/config.json"
# The source label of an agent's (no) token: see `resolve_token`.
AGENT_SOURCE = "agent mode"
# What a command that needs a token tells an agent instead of "save one".
AGENT_NO_TOKEN = (
    "agent mode (LOOM_CLIENT_ID with a workload token): this command uses no saved token. "
    "Pass --token or set LOOM_API_KEY, or use `loom call`, which exchanges the workload "
    "token itself; to act as a person, unset LOOM_CLIENT_ID first."
)


def refused_for_agent(source: str) -> bool:
    """Print `AGENT_NO_TOKEN` and answer True when `resolve_token` found no
    token because this is an agent; a command that needs a token then exits 2."""
    if source != AGENT_SOURCE:
        return False
    print(f"ERROR: {AGENT_NO_TOKEN}", file=sys.stderr)
    return True


# Every command resolves the server URL and the token in the plugin hooks'
# order (`resolveServerUrl` / `resolveToken` in the plugin's loom-client.mjs),
# so `loom <cmd>` and the hooks on one machine reach the same server as the
# same principal. `home` and `env` default to the process's.


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def config_file(home: Path | None = None, env: Mapping[str, str] | None = None) -> Path:
    """`LOOM_CONFIG_FILE`, else `~/.loom/config.json` (what `loom init` writes)."""
    override = _env(env).get("LOOM_CONFIG_FILE")
    return Path(override) if override else (home or Path.home()) / ".loom" / "config.json"


def loom_config(home: Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The client config file's JSON object; {} when it is missing or not an object."""
    try:
        loaded = json.loads(config_file(home, env).read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def resolve_server_url(
    explicit: str | None, home: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """(url, source): the flag, `LOOM_BASE_URL`, the config file's `url`, the default."""
    if explicit:
        return explicit.rstrip("/"), "--server-url"
    from_env = _env(env).get("LOOM_BASE_URL", "").strip()
    if from_env:
        return from_env.rstrip("/"), "LOOM_BASE_URL"
    cfg = str(loom_config(home, env).get("url") or "").strip()
    if cfg:
        return cfg.rstrip("/"), CONFIG_SOURCE
    return DEFAULT_BASE_URL, "default"


def default_base_url() -> str:
    """The server URL a command uses when no flag names one."""
    return resolve_server_url(None)[0]


def token_path() -> Path:
    """`~/.loom/token`: the file `loom mint-token --save` and `loom init` write."""
    return Path.home() / ".loom" / "token"


def agent_mode(env: Mapping[str, str] | None = None) -> bool:
    """An agent with a workload trust (`LOOM_CLIENT_ID`) and its workload
    token: it signs in as itself or not at all (the plugin's `agentMode`)."""
    environ = _env(env)
    return bool(
        environ.get("LOOM_CLIENT_ID")
        and (environ.get("LOOM_WORKLOAD_TOKEN") or environ.get("LOOM_WORKLOAD_TOKEN_FILE"))
    )


def resolve_token(
    explicit: str | None, home: Path | None = None, env: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """(token, source): the flag, `LOOM_API_KEY`; for an agent nothing
    (`AGENT_SOURCE`); otherwise the config file's `token`, `LOOM_API_KEY_FILE`
    or `~/.loom/token`, else ('', 'none') for the tokenless single-user stack.

    An agent's Loom token lives minutes and is bound to the server it was
    exchanged at, so these commands neither borrow the plugin's copy nor take
    a person's saved one; `loom call` (the SDK's `Agent`) exchanges the
    workload token itself."""
    if explicit and explicit.strip():
        return explicit.strip(), "--token"
    environ = _env(env)
    from_env = environ.get("LOOM_API_KEY", "").strip()
    if from_env:
        return from_env, "LOOM_API_KEY"
    if agent_mode(env):
        return "", AGENT_SOURCE
    cfg = str(loom_config(home, env).get("token") or "").strip()
    if cfg:
        return cfg, CONFIG_SOURCE
    override = environ.get("LOOM_API_KEY_FILE")
    p = Path(override) if override else (home or Path.home()) / ".loom" / "token"
    try:
        saved = p.read_text(encoding="utf-8").strip()
    except OSError:
        saved = ""
    return (saved, str(p)) if saved else ("", "none")


def jwt_claims(token: str) -> dict[str, Any]:
    """Unverified payload of a JWT (org / namespaces for the defaults); {}
    for anything that is not three dot-separated base64url parts."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        out = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, UnicodeError):
        return {}
    return out if isinstance(out, dict) else {}


def error_line(status: int, reason: str, raw: bytes) -> str:
    """`HTTP <status> <code>: <message>` from Loom's error envelope, the message
    capped at 200 characters. The raw body is never printed: it can echo the
    request (a validation error quotes its input), and this line lands in
    terminals and CI logs."""
    code, message = "", ""
    try:
        detail = json.loads(raw or b"{}").get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, dict):
        code, message = str(detail.get("code") or ""), str(detail.get("message") or "")
    elif isinstance(detail, list) and detail and isinstance(detail[0], dict):
        code, message = "validation_error", str(detail[0].get("msg") or "")
    elif isinstance(detail, str):
        message = detail
    line = f"HTTP {status} {code or reason}"
    return f"{line}: {message[:200]}" if message else line


# ---- JSON over HTTP ------------------------------------------------------------
# Every client command's GET / POST to a Loom server: (status, JSON object).
# Status 0 with a `detail` string means no Loom answer came: nothing answered,
# or something that is not a Loom server did (a proxy's login page), which must
# never read as a successful op.


def _headers(token: str) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _read_error(e: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        raw = e.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — an unreadable error body is an empty one
        raw = ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {"detail": raw[:400]}
    return parsed if isinstance(parsed, dict) else {"detail": str(parsed)[:400]}


def _send(req: urllib.request.Request, timeout: float) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, _read_error(e)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 0, {"detail": f"{type(e).__name__}: {e}"}
    try:
        out = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return 0, {"detail": f"HTTP {status}, not a JSON answer: {raw[:120]!r}"}
    return status, out if isinstance(out, dict) else {}


def get_json(url: str, token: str, timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
    """GET `url`. An error status still carries its JSON body."""
    return _send(urllib.request.Request(url, headers=_headers(token)), timeout)


def post_json(
    url: str, body: dict[str, Any], token: str, timeout: float = 90.0
) -> tuple[int, dict[str, Any]]:
    """POST `body` as JSON to `url`."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST", headers=_headers(token)
    )
    return _send(req, timeout)


def post_bytes(
    url: str, data: bytes, token: str, timeout: float = 90.0
) -> tuple[int, dict[str, Any]]:
    """POST `data` as the raw body (`knowledge.upload`, which takes a file's bytes)."""
    headers = {**_headers(token), "Content-Type": "application/octet-stream"}
    return _send(urllib.request.Request(url, data=data, method="POST", headers=headers), timeout)


def post_op(
    base_url: str, op: str, body: dict[str, Any], token: str, timeout: float = 90.0
) -> tuple[int, dict[str, Any]]:
    """POST `body` to the op endpoint `<base_url>/v1/<op>`."""
    return post_json(f"{base_url.rstrip('/')}/v1/{op}", body, token, timeout)
