"""Helpers the settings field declarations share."""

from __future__ import annotations

import base64
import functools
import json
import os
import re
from collections.abc import Iterable

from pydantic import AfterValidator, BeforeValidator

from server.core.webhook_secrets import WEBHOOK_SECRET_PREFIX


def decode_json_or_b64(raw: str, field: str) -> object:
    """Parse either raw JSON or base64-encoded JSON. base64 sidesteps shell
    newline-mangling when PEM keys are stored in .env files."""
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"{field} is not valid JSON: {e}") from e
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except Exception as e:
        raise ValueError(f"{field} is not raw JSON or base64-encoded JSON: {e}") from e
    return json.loads(decoded)


@functools.lru_cache(maxsize=16)
def json_map_items(
    raw: str, field: str, *, strings_only: bool = False
) -> tuple[tuple[str, str], ...]:
    """A string → string map setting (JWT kid → key, rerank name → URL), decoded once
    per raw value: signing and verification read it on every token. Keyed on the raw
    string, so a changed value (a test override) is decoded afresh. `strings_only`
    refuses a value that is not a string instead of converting it."""
    parsed = decode_json_or_b64(raw, field)
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object")
    if strings_only and not all(isinstance(v, str) for v in parsed.values()):
        raise ValueError(f"{field} must map names to strings")
    return tuple((str(k), str(v)) for k, v in parsed.items())


# The checked-in HS256 default. Fine for a single-user dev box (auth is off);
# with auth on it lets anyone who has read this file mint accepted tokens.
DEV_JWT_SECRET = "dev-only-jwt-secret-do-not-use-in-prod"


# LOOM_* variables that are not settings fields: read straight from the
# environment by the server, the CLI, deploy scripts or the clients.
NON_SETTINGS_ENV = frozenset(
    {
        "LOOM_ADMIN_TOKEN",
        "LOOM_AGENT",
        "LOOM_API_KEY",
        "LOOM_API_KEY_FILE",
        "LOOM_AUTO_RUN",
        "LOOM_BASE_URL",
        "LOOM_BIND_PORT",
        "LOOM_CH_CONTAINER",
        "LOOM_CH_HOST_PORT",
        "LOOM_CH_NATIVE_HOST_PORT",
        "LOOM_CLICKHOUSE_IMAGE",
        "LOOM_COLOUR",
        "LOOM_COLOUR_PORT",
        "LOOM_CONFIG_FILE",
        "LOOM_EMBEDDING_API_KEY",
        "LOOM_ENV_FILE",
        "LOOM_GRACEFUL_SHUTDOWN_S",
        "LOOM_HOOK_EXIT_MS",
        "LOOM_HOOK_TIMEOUT_MS",
        # Compose-only, from an install's .env: the images and the published port.
        "LOOM_EXTRACTOR_IMAGE_REPOSITORY",
        "LOOM_IMAGE_REPOSITORY",
        "LOOM_INSTANCE_LABEL",
        "LOOM_KEEP_ALIVE_S",
        "LOOM_LISTEN_HOST",
        "LOOM_MENTOR_BRIEF",
        "LOOM_NAMESPACE",
        "LOOM_ORG",
        "LOOM_OUTPUT_DIR",
        "LOOM_PORT",
        "LOOM_PREFLIGHT_HEALTH_URL",
        "LOOM_PREFLIGHT_MIGRATE",
        "LOOM_PREFLIGHT_PORT",
        "LOOM_PREFLIGHT_STRICT",
        "LOOM_PROFILE",
        "LOOM_PUBLIC_HEALTH_URL",
        "LOOM_REBUILD_DIR",
        "LOOM_REDEPLOY_READY_TIMEOUT_S",
        "LOOM_RELEASES_URL",
        "LOOM_REPO",
        "LOOM_REPOSITORY",
        "LOOM_SETTINGS_FILE",
        "LOOM_SHUNT_READ_CAP",
        "LOOM_UVICORN_LOOP",
        "LOOM_VERSION",
    }
)
# Families of non-settings names, each documented as one `<PREFIX>...` row: a
# webhook signing secret is any variable a registration names.
NON_SETTINGS_ENV_PREFIXES = (WEBHOOK_SECRET_PREFIX,)


def unknown_env_keys(fields: Iterable[str], env_file: str | None) -> list[str]:
    """LOOM_* names in the environment or the dotenv file that match no
    settings field and no known non-settings variable or family: a typo or a name
    this build does not read, which `extra="ignore"` would drop silently. Names only."""
    names = {k for k in os.environ if k.startswith("LOOM_")}
    if env_file and os.path.isfile(env_file):
        from dotenv import dotenv_values

        names |= {k for k in dotenv_values(env_file) if k.startswith("LOOM_")}
    known = {f"LOOM_{f.upper()}" for f in fields} | NON_SETTINGS_ENV
    return sorted(
        n for n in names - known if not any(n.startswith(p) for p in NON_SETTINGS_ENV_PREFIXES)
    )


# A field whose name matches is a secret: `repr(settings)` shows only its length
# and the configuration reference never prints its default. The word-boundary
# suffixes avoid false positives like `clickstack_user_id_keys`; `jwt_secret_previous`
# is the root secret a rotation still opens credentials with. `code_repos_json`
# entries may carry a `github_token`; the OTLP headers carry the exporter's
# bearer token.
_SECRET_FIELD = re.compile(
    "|".join(
        (
            r"(?:^|_)secret(?:_previous)?$",
            r"(?:^|_)password$",
            r"(?:^|_)(?:admin|extractor|github|scim|router|operator)_token$",
            r"(?:^|_)key$",
            r"(?:^|_)public_keys_json$",
            r"(?:^|_)credentials_json$",
            r"^code_repos_json$",
            # Its URLs can carry a credential (`_check_rerank_endpoints`).
            r"^rerank_endpoints_json$",
            r"^document_extractor_url$",
            r"^document_extractors_json$",
            r"^document_extractor_tokens_json$",
            r"(?:^|_)otlp_headers$",
            # A Postgres DSN carries the password: the memory authority's and the router's.
            r"(?:^|_)pg_dsn$",
        )
    )
)


def is_secret_field(name: str) -> bool:
    return _SECRET_FIELD.search(name.lower()) is not None


def _json_shaped(kind: type, *, literals: tuple[str, ...] = ()) -> AfterValidator:
    """A JSON-valued str field whose syntax and top-level shape are checked at
    load: '' (and any of `literals`) pass, anything else must parse to `kind`.
    The field stays a str; its reader parses the entries. The error names the
    field, never the value (it can carry credentials)."""
    noun = "object" if kind is dict else "array"

    def check(value: str) -> str:
        text = value.strip()
        if not text or text.lower() in literals:
            return value
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"not valid JSON (line {e.lineno} column {e.colno})") from None
        if not isinstance(parsed, kind):
            raise ValueError(f"must be a JSON {noun}")
        return value

    return AfterValidator(check)


JsonObject = _json_shaped(dict)
JsonArray = _json_shaped(list)
# '' / "off" = rule-only, "measured" = the shipped prior, else a JSON object route -> rate.
RoutePrior = _json_shaped(dict, literals=("measured", "off"))


def _casefold(value: object) -> object:
    return value.strip().lower() if isinstance(value, str) else value


# Before a mode field's Literal check: `INFO` and ` info ` load as `info`; a
# value outside the set fails at load.
CaseFolded = BeforeValidator(_casefold)
