"""Private local history for a serial cell administration run."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from server.cli.cell_each import Command

MAX_STATE_BYTES = 32 * 1024 * 1024  # not a setting: private state has one fixed custody bound
MAX_TARGETS = 4096  # not a setting: persisted target lists share the directory's protocol ceiling
_STATES = {"pending", "known-no-action", "dispatching", "completed", "UNSETTLED"}
_READS = {"settings_list", "settings_history", "signing_keys_list", "blacklist_global_list"}
_ACTIONS = _READS | {
    "settings_set",
    "signing_keys_rotate",
    "signing_keys_retire",
    "subject_revoke",
    "ratelimit_namespace_set",
    "budget_set",
    "blacklist_global_add",
    "blacklist_global_remove",
}
_TARGET_KEYS = {
    "cell",
    "deployment_id",
    "origin",
    "state",
    "mode",
    "mutation_generation",
    "configuration_digests",
    "automation",
    "recovery",
}
_TOP_KEYS = {"version", "run_id", "router", "command", "targets", "outcomes"}
_ARGUMENTS = {
    "settings_list": {"prefix"},
    "settings_set": {"name", "value", "clear", "expected"},
    "settings_history": {"name", "limit"},
    "signing_keys_list": set(),
    "signing_keys_rotate": {"algorithm", "activate_in"},
    "signing_keys_retire": {"kid"},
    "subject_revoke": {"subject", "reason"},
    "ratelimit_namespace_set": {"namespace", "op_class", "clear", "capacity", "rate"},
    "budget_set": {"op", "consumer", "clear", "budget_ms", "budget"},
    "blacklist_global_add": {"pattern_id", "pattern", "pattern_type", "reason"},
    "blacklist_global_list": {"limit"},
    "blacklist_global_remove": {"pattern_id"},
}
_ARGUMENT_TYPES = {
    "clear": (bool,),
    "limit": (int,),
    "value": (str, type(None)),
    "expected": (str, type(None)),
    "capacity": (float, type(None)),
    "rate": (float, type(None)),
    "budget_ms": (int, type(None)),
    "budget": (str, type(None)),
    "consumer": (str, type(None)),
    "activate_in": (float,),
}


def _bad() -> ValueError:
    return ValueError("Invalid private cell administration state")


def _json(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _bad() from None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _bad()
        result[key] = value
    return result


def _constant(_value: str) -> Any:
    raise _bad()


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise _bad()
    return number


def _text(value: Any, maximum: int, *, empty: bool = False) -> bool:
    return (
        type(value) is str
        and (empty or bool(value))
        and len(value.encode("utf-8")) <= maximum
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _bot(value: Any) -> bool:
    return (
        type(value) is dict
        and value.keys() == {"name", "thumbprint"}
        and _text(value["name"], 256)
        and type(value["thumbprint"]) is str
        and re.fullmatch(r"[A-Za-z0-9_-]{43}", value["thumbprint"]) is not None
    )


def _target(value: Any) -> None:
    if type(value) is not dict or value.keys() != _TARGET_KEYS:
        raise _bad()
    if not _text(value["cell"], 128) or not _text(value["deployment_id"], 256):
        raise _bad()
    origin = value["origin"]
    if not _text(origin, 512):
        raise _bad()
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or origin != f"https://{parsed.netloc}"
        or parsed.netloc != parsed.netloc.lower()
    ):
        raise _bad()
    if value["state"] != "serving" or value["mode"] != "enforced":
        raise _bad()
    if not _uuid(value["mutation_generation"]):
        raise _bad()
    digests = value["configuration_digests"]
    if (
        type(digests) is not list
        or not 1 <= len(digests) <= 8
        or any(
            type(item) is not str or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in digests
        )
        or len(set(digests)) != len(digests)
    ):
        raise _bad()
    if not _bot(value["automation"]) or not _bot(value["recovery"]):
        raise _bad()
    if any(value["automation"][key] == value["recovery"][key] for key in ("name", "thumbprint")):
        raise _bad()
    if len(_json(value)) > 1024:
        raise _bad()


def _command(value: Any) -> None:
    if type(value) is not dict or value.keys() != {"action", "arguments", "digest", "mutating"}:
        raise _bad()
    if type(value["action"]) is not str or value["action"] not in _ACTIONS:
        raise _bad()
    if type(value["arguments"]) is not dict or len(_json(value["arguments"])) > 65536:
        raise _bad()
    if value["arguments"].keys() != _ARGUMENTS[value["action"]]:
        raise _bad()
    if any(
        type(item) not in {str, bool, int, float, type(None)}
        for item in value["arguments"].values()
    ):
        raise _bad()
    if any(
        type(item) not in _ARGUMENT_TYPES.get(key, (str,))
        for key, item in value["arguments"].items()
    ):
        raise _bad()
    if type(value["mutating"]) is not bool or value["mutating"] != (value["action"] not in _READS):
        raise _bad()
    intent = {"action": value["action"], "arguments": value["arguments"]}
    if value["digest"] != hashlib.sha256(_json(intent)).hexdigest():
        raise _bad()


def _outcome(value: Any) -> None:
    if type(value) is not dict or value.keys() != {"status", "attempt", "code", "completion"}:
        raise _bad()
    if type(value["status"]) is not str or value["status"] not in _STATES:
        raise _bad()
    if type(value["attempt"]) is not int or not 0 <= value["attempt"] <= 1000000:
        raise _bad()
    if type(value["code"]) is not str or re.fullmatch(r"[A-Z0-9_]{0,64}", value["code"]) is None:
        raise _bad()
    if value["status"] == "pending" and value["attempt"] != 0:
        raise _bad()
    if value["status"] != "pending" and value["attempt"] == 0:
        raise _bad()
    completion = value["completion"]
    if value["status"] == "completed":
        if type(completion) is not dict or not completion or len(_json(completion)) > 2048:
            raise _bad()
    elif completion is not None:
        raise _bad()


def _completion(state: dict[str, Any], target: dict[str, Any]) -> None:
    outcome = state["outcomes"][target["cell"]]
    if outcome["status"] != "completed":
        return
    value = outcome["completion"]
    if value.keys() != {
        "version",
        "context",
        "action",
        "started_audit",
        "completed_audit",
        "result_digest",
    }:
        raise _bad()
    context = {
        "version": 1,
        "expected_cell": target["cell"],
        "expected_deployment_id": target["deployment_id"],
        "expected_bot": target["automation"]["name"],
        "run_id": state["run_id"],
        "attempt": outcome["attempt"],
        "command_digest": state["command"]["digest"],
    }
    saved_context = value["context"]
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or type(saved_context) is not dict
        or saved_context.keys() != context.keys()
        or any(
            type(saved_context[key]) is not type(item) or saved_context[key] != item
            for key, item in context.items()
        )
    ):
        raise _bad()
    if value["action"] != state["command"]["action"]:
        raise _bad()
    for key in ("started_audit", "completed_audit", "result_digest"):
        if type(value[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", value[key]) is None:
            raise _bad()


def _validate(state: Any) -> None:
    if type(state) is not dict or state.keys() != _TOP_KEYS:
        raise _bad()
    if type(state["version"]) is not int or state["version"] != 1 or not _uuid(state["run_id"]):
        raise _bad()
    if state["router"] is not None and not _text(state["router"], 512):
        raise _bad()
    _command(state["command"])
    targets, outcomes = state["targets"], state["outcomes"]
    if (
        type(targets) is not list
        or not 1 <= len(targets) <= MAX_TARGETS
        or type(outcomes) is not dict
    ):
        raise _bad()
    cells: set[str] = set()
    for target in targets:
        _target(target)
        if target["cell"] in cells:
            raise _bad()
        cells.add(target["cell"])
    if outcomes.keys() != cells:
        raise _bad()
    for outcome in outcomes.values():
        _outcome(outcome)
    for target in targets:
        _completion(state, target)
    if sum(outcome["status"] in {"dispatching", "UNSETTLED"} for outcome in outcomes.values()) > 1:
        raise _bad()


def new_state(command: Command, *, targets: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    """Freeze accepted public targets and intent without credential references."""
    if type(targets) is not list or not 1 <= len(targets) <= MAX_TARGETS:
        raise _bad()
    for target in targets:
        _target(target)
    state = {
        "version": 1,
        "run_id": run_id,
        "router": None,
        "command": {
            "action": command.action,
            "arguments": command.arguments,
            "digest": command.digest,
            "mutating": command.mutating,
        },
        "targets": targets,
        "outcomes": {
            target["cell"]: {"status": "pending", "attempt": 0, "code": "", "completion": None}
            for target in targets
        },
    }
    _validate(state)
    # Own the accepted snapshot instead of retaining a caller's mutable target list.
    owned: dict[str, Any] = json.loads(_json(state))
    return owned


def has_uncertainty(state: dict[str, Any]) -> bool:
    """An outstanding dispatch cannot be settled by local history or a retry."""
    return any(
        value["status"] in {"dispatching", "UNSETTLED"} for value in state["outcomes"].values()
    )


def resume_read(path: Path, state: dict[str, Any]) -> None:
    """A lost fixed read may repeat; it supplies no unknown business mutator."""
    if state["command"]["mutating"]:
        return
    for outcome in state["outcomes"].values():
        if outcome["status"] == "dispatching":
            outcome.update(status="known-no-action", code="READ_INCOMPLETE")
            write_state(path, state)


@contextmanager
def private_parent(path: Path) -> Iterator[tuple[int, str]]:
    """Pin every directory without following links; the leaf remains owner-only."""
    parts = path.absolute().parts
    if not parts[-1] or any(part in {".", ".."} for part in parts[1:]):
        raise _bad()
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts[1:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid not in {0, os.geteuid()}:
                raise _bad()
            writable = info.st_mode & 0o022
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if writable and not sticky_root:
                raise _bad()
        info = os.fstat(descriptor)
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise _bad()
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _read_bytes_at(parent: int, name: str, limit: int) -> tuple[bytes, os.stat_result]:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent
    )
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or not info.st_mode & stat.S_IRUSR
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            raise _bad()
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(raw) > limit or any(getattr(after, key) != getattr(info, key) for key in stable):
            raise _bad()
    return raw, info


def read_private(path: Path, limit: int) -> bytes:
    """Read a bounded private input through the same pinned directory boundary."""
    if type(limit) is not int or not 0 < limit <= MAX_STATE_BYTES:
        raise _bad()
    with private_parent(Path(path)) as (parent, name):
        raw, _info = _read_bytes_at(parent, name, limit)
        return raw


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Admit one local coordinator; lock release never settles a remote action."""
    with private_parent(Path(path)) as (parent, name):
        descriptor = os.open(
            f".{name}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
            dir_fd=parent,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise _bad()
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)


def _read_at(parent: int, name: str) -> tuple[dict[str, Any], os.stat_result]:
    raw, info = _read_bytes_at(parent, name, MAX_STATE_BYTES)
    try:
        state = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
        )
        _validate(state)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _bad() from None
    return state, info


def read_state(path: Path) -> dict[str, Any]:
    """Read one bounded complete state; malformed history never becomes a new run."""
    with private_parent(Path(path)) as (parent, name):
        state, _info = _read_at(parent, name)
        return state


def _transition(previous: dict[str, Any], current: dict[str, Any]) -> None:
    for key in ("version", "run_id", "router", "command"):
        if previous[key] != current[key]:
            raise _bad()
    targets = {row["cell"]: row for row in current["targets"]}
    if any(targets.get(row["cell"]) != row for row in previous["targets"]):
        raise _bad()
    allowed = {
        "pending": {"pending", "dispatching", "known-no-action"},
        "known-no-action": {"known-no-action", "dispatching"},
        "dispatching": {"dispatching", "completed", "known-no-action", "UNSETTLED"},
        "completed": {"completed"},
        "UNSETTLED": {"UNSETTLED"},
    }
    for cell, old in previous["outcomes"].items():
        new = current["outcomes"][cell]
        if new["status"] not in allowed[old["status"]]:
            raise _bad()
        if old["status"] in {"completed", "UNSETTLED"} and new != old:
            raise _bad()
        increment = old["status"] in {"pending", "known-no-action"} and new != old
        if new["attempt"] != old["attempt"] + int(increment):
            raise _bad()
    changes = sum(current["outcomes"][cell] != old for cell, old in previous["outcomes"].items())
    if changes > 1:
        raise _bad()
    if has_uncertainty(previous):
        if current["targets"] != previous["targets"]:
            raise _bad()
        for cell, old in previous["outcomes"].items():
            if old["status"] != "dispatching" and current["outcomes"][cell] != old:
                raise _bad()
    for cell in current["outcomes"].keys() - previous["outcomes"].keys():
        if current["outcomes"][cell]["status"] != "pending":
            raise _bad()


def _publish(parent: int, name: str, raw: bytes, *, exclusive: bool) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=parent,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent)
        else:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def write_state(path: Path, state: dict[str, Any], *, exclusive: bool = False) -> None:
    """Publish acknowledged local history before admitting any later action."""
    _validate(state)
    raw = _json(state)
    if len(raw) > MAX_STATE_BYTES:
        raise _bad()
    with private_parent(Path(path)) as (parent, name):
        if not exclusive:
            previous, _info = _read_at(parent, name)
            _transition(previous, state)
        _publish(parent, name, raw, exclusive=exclusive)
