"""The deployment's published configuration, and each process's drift from it.

Every Loom process of a deployment is a full replica on one ClickHouse, run
from one env. What must agree between them is checked rather than trusted: the
workers leader publishes, as one `loom.deployment_config` row named `settings`
(a JSON object), every setting its env sets explicitly plus the MUST_MATCH
settings, and an HMAC fingerprint of its root secret (never the secret) under
`_keys`. Every process compares itself with that snapshot at boot and on each
instance heartbeat (`server.core.instances`): a different root secret (its
tokens and stored credentials do not open elsewhere) or a different MUST_MATCH
value (its vectors are in another space) is drift, `drift()` lists it, the
process is not ready and `/v1/health` reports it.

The leader rewrites the snapshot only when it agrees with the stored one on
what must match and no other live instance still runs the stored one, so a
drifted leader never publishes itself over the deployment and a mixed env
during a rollout never flips the published configuration back and forth. A
new root secret or MUST_MATCH value is published by the operator, with
`loom deployment publish`.

What is never published: secrets (`is_secret_field`; credentials inside
`code_repos_json` entries and inside URLs are stripped), and the areas that
describe one process rather than the deployment's data — its ClickHouse
connection, its auth and tokens, its process shape, rate limits, telemetry
export and blob storage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import TypeAdapter

from server.core import embedding_space, readiness
from server.settings import Settings, settings
from server.settings._auth import AuthSettings
from server.settings._clickhouse import ClickHouseSettings
from server.settings._env import is_secret_field
from server.settings._observability import ObservabilitySettings
from server.settings._ratelimit import RateLimitSettings
from server.settings._runtime import RuntimeSettings
from server.settings._storage import StorageSettings

log = logging.getLogger(__name__)

ROW_NAME = "settings"

# Areas that describe this process, not the deployment's data. Only the
# pre-033 window needs this (see `snapshot`).
_PROCESS_AREAS = (
    ClickHouseSettings,
    AuthSettings,
    RuntimeSettings,
    RateLimitSettings,
    ObservabilitySettings,
    StorageSettings,
)
_PROCESS_FIELDS = frozenset(name for area in _PROCESS_AREAS for name in area.model_fields)

# Secret by name only because entries may carry a token; published with the
# credentials taken out. Only the pre-033 window needs this (see `snapshot`).
_SANITISED_SECRET_FIELDS = frozenset({"code_repos_json"})

# Settings whose value fixes how stored data was made: a process that holds a
# different value reads and writes the shared rows wrongly, so it is not ready.
# The embedding space's whole identity: one label at one width can be two spaces.
MUST_MATCH = embedding_space.FIELDS


# The env's embedding settings once a dashboard configuration replaced the live
# ones (`set_environment_source`), else None. A committed provider transition is
# the deployment's, read from ClickHouse by every replica: what must match is the
# env each replica was started with, not the transition it applied.
def _live_is_environment() -> dict[str, Any] | None:
    return None


_environment: Callable[[], dict[str, Any] | None] = _live_is_environment


def set_environment_source(source: Callable[[], dict[str, Any] | None]) -> None:
    global _environment
    _environment = source


def _must_match(s: Settings) -> dict[str, Any]:
    """`s`'s MUST_MATCH values as its env sets them, as one public identity (an
    empty provider is the one the model implies; no credentials in the URL)."""
    values = s.model_dump(include=set(MUST_MATCH))
    env = _environment() if s is settings else None
    if env is not None:
        values.update(env)
    return _identity(values)


def _identity(values: dict[str, Any]) -> dict[str, Any]:
    return dict(zip(MUST_MATCH, embedding_space.public_identity(values), strict=True))


_URL_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.IGNORECASE)


def publishable(name: str) -> bool:
    """Whether the leader publishes setting `name` (when its env sets it)."""
    if name not in Settings.model_fields or name in _PROCESS_FIELDS:
        return False
    return name in _SANITISED_SECRET_FIELDS or not is_secret_field(name)


def _strip(value: Any) -> Any:
    """`value` without credentials: secret-named keys dropped at any depth,
    userinfo removed from URLs. Only the pre-033 window needs it (see `snapshot`)."""
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
            # The process parses it the same way and logs the error itself;
            # an unreadable value is not published rather than published raw.
            return None
    return _strip(value)


# The key of the snapshot entry that holds key fingerprints, not a setting.
KEYS_ENTRY = "_keys"
_FINGERPRINT_INPUT = b"loom.deployment-config.key-check"


def _fingerprint(secret: str) -> str:
    return hmac.new(secret.encode(), _FINGERPRINT_INPUT, hashlib.sha256).hexdigest()[:32]


def key_fingerprints(s: Settings) -> dict[str, Any]:
    """The fingerprint of `s`'s root secret, which signs as kid `default` and
    seals every stored credential and every other signing key. For one
    release it is also published in the fields a read-replica of the build
    before 033 checks (one key map holding `default`), so such a replica
    restarting mid-upgrade still boots."""
    root = _fingerprint(str(s.jwt_secret))
    return {"root": root, "signing": {"default": root}, "active_kid": "default", "sealing": root}


def check_keys(s: Settings, published: dict[str, Any]) -> None:
    """Raise ValueError unless `s` holds the deployment's root secret. A snapshot
    without its fingerprint is not checked."""
    theirs = published.get(KEYS_ENTRY)
    # A build before 033 publishes no `root` fingerprint.
    if not isinstance(theirs, dict) or "root" not in theirs:
        return
    if theirs["root"] != key_fingerprints(s)["root"]:
        raise ValueError(
            "this process's LOOM_JWT_SECRET differs from the deployment's, so its "
            "tokens, signing keys and stored credentials do not open on the other "
            "replicas: give it the deployment's LOOM_JWT_SECRET, or publish this one "
            "with `loom deployment publish`"
        )


def snapshot(s: Settings) -> dict[str, Any]:
    """The settings `s` publishes: each one its env set explicitly and every
    MUST_MATCH one, sanitised, plus its key fingerprints under KEYS_ENTRY.

    This build reads only KEYS_ENTRY and MUST_MATCH. The other settings are for
    the pre-033 window: a read-replica of that build applies each published
    setting its env leaves unset. Next release the snapshot is KEYS_ENTRY and
    MUST_MATCH only, and `_PROCESS_AREAS`, `_strip`, `_SANITISED_SECRET_FIELDS`,
    `config_fp` and `_may_replace`'s live-digest rule go with the rest."""
    out: dict[str, Any] = {KEYS_ENTRY: key_fingerprints(s)}
    for name in sorted(set(s.model_fields_set) - set(MUST_MATCH)):
        if not publishable(name):
            continue
        value = _published_value(name, s.model_dump(mode="json", include={name})[name])
        if value is not None:
            out[name] = value
    return out | _must_match(s)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:32]


def fingerprints(s: Settings) -> dict[str, str]:
    """What an instance row carries: hashes of `s`'s MUST_MATCH values, of its
    key fingerprints and of its whole snapshot (`config_fp`, equal to the
    published row's digest when `s` is what is published; only the pre-033
    window needs it, see `snapshot`)."""
    return {
        "settings_fp": _digest(_must_match(s)),
        "keys_fp": _digest(key_fingerprints(s)),
        "config_fp": _digest(snapshot(s)),
    }


def _coerce(name: str, value: Any) -> Any:
    field = Settings.model_fields[name]
    annotation: Any = field.annotation
    if field.metadata:
        annotation = Annotated[annotation, *field.metadata]
    return TypeAdapter(annotation).validate_python(value)


def compare(s: Settings, published: dict[str, Any]) -> list[str]:
    """How `s` differs from the published snapshot on what must match: the
    token keys and MUST_MATCH. Empty when it agrees."""
    problems: list[str] = []
    try:
        check_keys(s, published)
    except ValueError as e:
        problems.append(str(e))
    mine = _must_match(s)
    # The published row normalised as this process's own (an older build's keeps a
    # trailing slash, an empty provider, userinfo); a field it lacks is not judged.
    given = {}
    for name in MUST_MATCH:
        if name in published:
            try:
                given[name] = _coerce(name, published[name])
            except ValueError:
                given[name] = published[name]
    try:
        theirs = _identity({**mine, **given})
    except (ValueError, TypeError):
        theirs = {**mine, **given}
    for name in given:
        if theirs[name] != mine[name]:
            problems.append(
                f"LOOM_{name.upper()}={mine[name]!r} differs from the deployment's "
                f"{theirs[name]!r}: the shared rows were made with the deployment's value"
            )
    return problems


# The last comparison and the snapshot it read, for drift() and /v1/health.
STATE: dict[str, Any] = {
    "drift": [],
    "checked": False,
    "published_by": "",
    "published_at": "",
    "error": "",
}


def drift() -> list[str]:
    """Why this process disagrees with the deployment's published configuration
    on what must match; empty when it agrees, when nothing is published yet and
    outside the deployment (a branch server's env is not the deployment's). A
    process with drift is not ready."""
    return list(STATE["drift"])


def readiness_check() -> readiness.CheckResult:
    """Not ready on drift, nor in a deployment before the first read of the
    published snapshot succeeded (the verdict until then says nothing)."""
    if settings.deployment and not STATE["checked"]:
        why = STATE["error"] or "not read yet"
        return readiness.result(False, f"deployment configuration unchecked: {why}"[:300])
    found = drift()
    return readiness.result(not found, "; ".join(found)[:300])


async def read_published(ch: Any) -> dict[str, Any] | None:
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


async def check(ch: Any) -> None:
    """Compare this process with the published snapshot. A failed read keeps
    the last verdict. Never raises."""
    if not settings.deployment:
        STATE["drift"] = []
        return
    try:
        published = await read_published(ch)
    except Exception as e:  # noqa: BLE001 — a failed read keeps the last verdict
        STATE["error"] = str(e)
        log.warning("deployment config check failed; keeping the last verdict: %s", e)
        return
    found = compare(settings, published) if published else []
    if found and found != STATE["drift"]:
        log.error("this process's configuration differs from the deployment's: %s", found)
    STATE.update(drift=found, checked=True, error="")


async def publish(ch: Any, *, live: list[dict[str, Any]], published_by: str = "") -> bool:
    """Write this process's snapshot, stamped with its label, unless the stored
    one is the same or may not be replaced (`_may_replace`). The deployment's
    workers leader calls it (`loom deployment publish` calls `write`). Never
    fatal; True when it wrote."""
    if not settings.deployment:
        return False
    try:
        mine = snapshot(settings)
        stored = await read_published(ch)
        if stored == mine:
            return False
        if stored is not None and not _may_replace(stored, live):
            return False
        await write(ch, mine, published_by=published_by)
    except Exception as e:  # noqa: BLE001 — publishing must not fail the leader
        log.warning("deployment config publish failed: %s", e)
        return False
    return True


async def write(ch: Any, snap: dict[str, Any], *, published_by: str) -> None:
    """Store `snap` as the deployment's snapshot. Raises when the write fails."""
    await ch.insert(
        "loom.deployment_config",
        [
            {
                "name": ROW_NAME,
                "value": json.dumps(snap, sort_keys=True),
                "published_by": published_by,
                "build_commit": settings.build_commit,
            }
        ],
    )


def _may_replace(stored: dict[str, Any], live: list[dict[str, Any]]) -> bool:
    """Whether the leader may replace the stored snapshot by its own. Never
    when it drifts from it (its root secret or a MUST_MATCH value differs):
    that takes `loom deployment publish`. Otherwise only once no other live
    instance (`live`, from `server.core.instances.read_live`) runs the stored
    one, so a mixed env during a rollout does not flip it back and forth. That
    live-digest rule matters only while the snapshot carries more than must
    match (the pre-033 window, see `snapshot`)."""
    from server.core.instances import instance_id

    if compare(settings, stored):
        return False
    theirs = _digest(stored)
    return not any(
        r.get("config_fp") == theirs and r.get("instance_id") != instance_id() for r in live
    )


__all__ = [
    "KEYS_ENTRY",
    "MUST_MATCH",
    "STATE",
    "check",
    "check_keys",
    "compare",
    "drift",
    "fingerprints",
    "key_fingerprints",
    "publish",
    "publishable",
    "read_published",
    "snapshot",
    "write",
]
