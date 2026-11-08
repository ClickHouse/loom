"""The deployment's published configuration, and each process's drift from it.

Every Loom process of a deployment is a full replica on one ClickHouse, run
from one env. What must agree between them is checked rather than trusted: the
workers leader publishes, as one `loom.deployment_config` row named `settings`
(a JSON object), the MUST_MATCH settings and an HMAC fingerprint of its root
secret (never the secret) under `_keys`. Every process compares itself with
that snapshot at boot and on each instance heartbeat (`server.core.instances`):
a different root secret (its tokens and stored credentials do not open
elsewhere) or a different MUST_MATCH value (its vectors are in another space)
is drift, `drift()` lists it, the process is not ready and `/v1/health`
reports it.

The leader rewrites the snapshot only when it agrees with the stored one on
what must match and no other live instance still runs the stored one, so a
drifted leader never publishes itself over the deployment and builds whose
snapshots differ in shape (N-1 and N during a rollout) never flip the published
configuration back and forth. A new root secret or MUST_MATCH value is
published by the operator, with `loom deployment publish`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from typing import Annotated, Any

from pydantic import TypeAdapter

from server.core import embedding_space, readiness
from server.core.errors import error_kind
from server.settings import Settings, settings

log = logging.getLogger(__name__)

ROW_NAME = "settings"

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


# The key of the snapshot entry that holds key fingerprints, not a setting.
KEYS_ENTRY = "_keys"
_FINGERPRINT_INPUT = b"loom.deployment-config.key-check"


def _fingerprint(secret: str) -> str:
    return hmac.new(secret.encode(), _FINGERPRINT_INPUT, hashlib.sha256).hexdigest()[:32]


def key_fingerprints(s: Settings) -> dict[str, Any]:
    """The fingerprint of `s`'s root secret, which signs as kid `default` and
    seals every stored credential and every other signing key."""
    return {"root": _fingerprint(str(s.jwt_secret))}


def check_keys(s: Settings, published: dict[str, Any]) -> None:
    """Raise ValueError unless `s` holds the deployment's root secret; a snapshot
    without its fingerprint proves nothing and is refused."""
    theirs = published.get(KEYS_ENTRY)
    if not isinstance(theirs, dict) or "root" not in theirs:
        raise ValueError(
            "the published configuration holds no root-secret fingerprint; "
            "publish it with `loom deployment publish`"
        )
    if theirs["root"] != key_fingerprints(s)["root"]:
        raise ValueError(
            "this process's LOOM_JWT_SECRET differs from the deployment's, so its "
            "tokens, signing keys and stored credentials do not open on the other "
            "replicas: give it the deployment's LOOM_JWT_SECRET, or publish this one "
            "with `loom deployment publish`"
        )


def snapshot(s: Settings) -> dict[str, Any]:
    """What `s` publishes: its key fingerprints under KEYS_ENTRY and its
    MUST_MATCH values."""
    return {KEYS_ENTRY: key_fingerprints(s)} | _must_match(s)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:32]


def fingerprints(s: Settings) -> dict[str, str]:
    """What an instance row carries: hashes of `s`'s MUST_MATCH values, of its
    key fingerprints and of its whole snapshot (`config_fp`, equal to the
    published row's digest when `s` is what is published)."""
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
    # The published row normalised as this process's own (a trailing slash, an
    # empty provider or userinfo is the same space); a field it lacks is not judged.
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
        # `/v1/health` shows it without a token: the error's kind; the text is logged.
        STATE["error"] = error_kind(e)
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
        if stored and not _may_replace(stored, live):
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
    one, so builds whose snapshots differ in shape (N-1 and N during a rollout)
    do not flip it back and forth."""
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
    "read_published",
    "snapshot",
    "write",
]
