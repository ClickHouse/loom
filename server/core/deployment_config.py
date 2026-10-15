"""The main Loom's published settings, and a read-replica's copy of them.

A read-replica connects to the main Loom's ClickHouse and so already reads
every memory, code index and namespace. What it cannot read there is the
configuration the main Loom's env carries: which namespaces are code indexes
(`LOOM_CODE_REPOS_JSON`), which tracker namespace a repository binds to, the
embedding model the stored vectors were made with, the search tuning. The
main Loom's workers leader therefore publishes, at boot, every setting its env
sets explicitly — one `loom.deployment_config` row named `settings`, a JSON
object — and a read-replica applies each value to a setting its own env
leaves unset, at boot and every `LOOM_READ_REPLICA_CONFIG_REFRESH_SECONDS`.

A token is good on every Loom of a deployment: a read-replica verifies with
the main Loom's signing keys and opens the credentials the main Loom sealed
with its secret. The snapshot carries an HMAC fingerprint of each key (never
the key) under `_keys`, and a read-replica whose keys do not match refuses to
boot,
so a mismatch is a boot error rather than every token refused.

What is never published: secrets (`is_secret_field`; credentials inside
`code_repos_json` entries and inside URLs are stripped), and the areas that
describe one process rather than the deployment's data — its ClickHouse
connection, its auth and tokens, its process shape, rate limits, telemetry
export, blob storage and its read-replica settings.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
from typing import Annotated, Any

from pydantic import TypeAdapter

from server.settings import Settings, settings
from server.settings._auth import AuthSettings
from server.settings._clickhouse import ClickHouseSettings
from server.settings._env import is_secret_field
from server.settings._observability import ObservabilitySettings
from server.settings._ratelimit import RateLimitSettings
from server.settings._read_replica import ReadReplicaSettings
from server.settings._runtime import RuntimeSettings
from server.settings._storage import StorageSettings

log = logging.getLogger(__name__)

ROW_NAME = "settings"

# Areas that describe this process, not the deployment's data.
_PROCESS_AREAS = (
    ClickHouseSettings,
    AuthSettings,
    RuntimeSettings,
    RateLimitSettings,
    ObservabilitySettings,
    StorageSettings,
    ReadReplicaSettings,
)
_PROCESS_FIELDS = frozenset(name for area in _PROCESS_AREAS for name in area.model_fields)

# Secret by name only because entries may carry a token; published with the
# credentials taken out.
_SANITISED_SECRET_FIELDS = frozenset({"code_repos_json"})

# Settings whose value fixes how stored data was made: a read-replica that sets
# a different value would read the shared rows wrongly, so it refuses to boot.
MUST_MATCH = ("embedding_model", "embedding_dim")

_URL_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)


def publishable(name: str) -> bool:
    """Whether the main Loom publishes setting `name` (when its env sets it)."""
    if name not in Settings.model_fields or name in _PROCESS_FIELDS:
        return False
    return name in _SANITISED_SECRET_FIELDS or not is_secret_field(name)


def _strip(value: Any) -> Any:
    """`value` without credentials: secret-named keys dropped at any depth,
    userinfo removed from URLs."""
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if not is_secret_field(str(k))}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    if isinstance(value, str):
        return _URL_USERINFO.sub(r"\g<scheme>", value)
    return value


def _published_value(name: str, value: Any) -> Any:
    if name in _SANITISED_SECRET_FIELDS and isinstance(value, str) and value.strip():
        try:
            return json.dumps(_strip(json.loads(value)))
        except json.JSONDecodeError:
            # The main Loom parses it the same way and logs the error itself;
            # an unreadable value is not published rather than published raw.
            return None
    return _strip(value)


# The key of the snapshot entry that holds key fingerprints, not a setting.
KEYS_ENTRY = "_keys"
_FINGERPRINT_INPUT = b"loom.deployment-config.key-check"


def _fingerprint(secret: str) -> str:
    return hmac.new(secret.encode(), _FINGERPRINT_INPUT, hashlib.sha256).hexdigest()[:32]


def key_fingerprints(s: Settings) -> dict[str, Any]:
    """Fingerprints of `s`'s token signing keys (per kid), its signing kid and
    the secret credentials are sealed with."""
    return {
        "signing": {kid: _fingerprint(key) for kid, key in sorted(s.jwt_secret_map().items())},
        "active_kid": s.jwt_active_kid,
        "sealing": _fingerprint(str(s.jwt_secret)),
    }


def check_keys(s: Settings, published: dict[str, Any]) -> None:
    """Raise ValueError unless `s` verifies the main Loom's tokens and the
    main Loom verifies `s`'s: each side's signing kid is a key the other holds
    with the same secret. A snapshot without fingerprints is not checked."""
    theirs = published.get(KEYS_ENTRY)
    if not isinstance(theirs, dict):
        return
    ours = key_fingerprints(s)
    their_signing = theirs.get("signing") or {}
    problems = []

    def mismatch(kid: str, signer: str, holder: str, held: dict[str, Any]) -> None:
        if kid not in held:
            problems.append(f"{signer} signs with kid {kid!r}, which {holder} lacks")
        else:
            problems.append(f"{holder} holds a different key for kid {kid!r}")

    their_kid = str(theirs.get("active_kid") or "")
    if ours["signing"].get(their_kid) != their_signing.get(their_kid):
        mismatch(their_kid, "the main Loom", "this process", ours["signing"])
    our_kid = ours["active_kid"]
    if our_kid != their_kid and their_signing.get(our_kid) != ours["signing"][our_kid]:
        mismatch(our_kid, "this process", "the main Loom", their_signing)
    if theirs.get("sealing") and theirs.get("sealing") != ours["sealing"]:
        problems.append("LOOM_JWT_SECRET differs, so stored credentials do not open")
    if problems:
        raise ValueError(
            "this read-replica's token keys do not match the main Loom's ("
            + "; ".join(problems)
            + "): give it the main Loom's LOOM_JWT_SECRET, LOOM_JWT_SECRETS_JSON and "
            "LOOM_JWT_ACTIVE_KID so one token works on both"
        )


def snapshot(s: Settings) -> dict[str, Any]:
    """The settings `s` publishes: each one its env set explicitly, sanitised,
    plus its key fingerprints under KEYS_ENTRY."""
    out: dict[str, Any] = {KEYS_ENTRY: key_fingerprints(s)}
    for name in sorted(s.model_fields_set):
        if not publishable(name):
            continue
        value = _published_value(name, s.model_dump(mode="json", include={name})[name])
        if value is not None:
            out[name] = value
    return out


async def publish(ch: Any, *, published_by: str) -> None:
    """Write the main Loom's snapshot, stamped with the publishing process's
    label. Only the deployment's workers leader calls it, so a branch server on
    the same service never overwrites the deployment's shape. Never fatal."""
    try:
        await ch.insert(
            "loom.deployment_config",
            [
                {
                    "name": ROW_NAME,
                    "value": json.dumps(snapshot(settings), sort_keys=True),
                    "published_by": published_by,
                    "build_commit": settings.build_commit,
                }
            ],
        )
    except Exception as e:  # noqa: BLE001 — publishing must not fail the main Loom's boot
        log.warning("deployment config publish failed: %s", e)


# What this read-replica last applied, for /v1/health.
STATE: dict[str, Any] = {
    "applied": [],
    "keys_checked": False,
    "published_by": "",
    "published_at": "",
    "error": "",
}


async def _read(ch: Any) -> dict[str, Any] | None:
    row = await ch.query_one(
        "SELECT value, published_by, toString(published_at) AS published_at "
        "FROM loom.deployment_config FINAL WHERE name = {name:String}",
        params={"name": ROW_NAME},
    )
    if row is None:
        return None
    STATE["published_by"] = str(row.get("published_by") or "")
    STATE["published_at"] = str(row.get("published_at") or "")
    value = json.loads(str(row.get("value") or "{}"))
    return value if isinstance(value, dict) else None


def _coerce(name: str, value: Any) -> Any:
    field = Settings.model_fields[name]
    annotation: Any = field.annotation
    if field.metadata:
        annotation = Annotated[annotation, *field.metadata]
    return TypeAdapter(annotation).validate_python(value)


def apply(s: Settings, published: dict[str, Any], explicit: frozenset[str]) -> list[str]:
    """Set each published value on `s` whose name is not in `explicit` (the
    read-replica's own env). Returns the names whose value changed. Raises
    ValueError when a MUST_MATCH setting differs from the read-replica's own or
    its token keys do not match the main Loom's."""
    check_keys(s, published)
    for name in MUST_MATCH:
        if name in explicit and name in published:
            if _coerce(name, published[name]) != getattr(s, name):
                raise ValueError(
                    f"LOOM_{name.upper()}={getattr(s, name)!r} differs from the main Loom's "
                    f"{published[name]!r}: the shared vectors were made with the main Loom's; "
                    "unset it here"
                )
    changed: list[str] = []
    for name, raw in sorted(published.items()):
        if name in explicit or not publishable(name):
            continue
        try:
            value = _coerce(name, raw)
        except ValueError as e:
            log.warning("published setting %s ignored: %s", name, e)
            continue
        if getattr(s, name) != value:
            setattr(s, name, value)
            changed.append(name)
    if {"llm_provider", "llm_model"} & set(changed):
        s.adopt_published_llm()
    return changed


def explicit_fields(s: Settings) -> frozenset[str]:
    """The settings a read-replica's env chose, which it keeps over the main Loom's.
    An empty or "auto" conflict judge leaves the choice to the main Loom."""
    unset = {"memory_conflict_judge"} if s.memory_conflict_judge is None else set()
    return s.env_fields_set() - unset


# The read-replica's own env, captured before the first overlay.
_EXPLICIT: frozenset[str] | None = None


async def load(ch: Any, *, boot: bool) -> list[str]:
    """Apply the main Loom's snapshot to this read-replica's settings. At boot a
    ClickHouse error or a MUST_MATCH mismatch raises; on a refresh it logs and
    keeps what was applied."""
    global _EXPLICIT
    if _EXPLICIT is None:
        _EXPLICIT = explicit_fields(settings)
    try:
        published = await _read(ch)
        if published is None:
            log.warning(
                "no deployment config published on this service yet; the read-replica "
                "serves with its own settings only"
            )
            return []
        changed = apply(settings, published, _EXPLICIT)
    except Exception as e:
        STATE["error"] = str(e)
        if boot:
            raise
        log.warning("deployment config refresh failed; keeping the last applied: %s", e)
        return []
    STATE["error"] = ""
    STATE["applied"] = sorted(n for n in published if n not in _EXPLICIT and publishable(n))
    STATE["keys_checked"] = isinstance(published.get(KEYS_ENTRY), dict)
    if changed:
        log.info("applied %d published setting(s): %s", len(changed), ", ".join(changed))
    return changed


async def refresh_loop(ch: Any) -> None:
    from server.core.ch import set_query_tag

    set_query_tag("read_replica.deployment_config")
    while True:
        await asyncio.sleep(settings.read_replica_config_refresh_seconds)
        await load(ch, boot=False)


__all__ = [
    "KEYS_ENTRY",
    "MUST_MATCH",
    "STATE",
    "apply",
    "check_keys",
    "key_fingerprints",
    "load",
    "publish",
    "publishable",
    "snapshot",
]
