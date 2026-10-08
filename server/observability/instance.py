"""Which process wrote this row.

A build is not a process: the deployment's web workers, a CI server and
isolated worktree servers can all write to the same `loom.agent_events`, and
only the container sets `HOSTNAME`. Three stamps, from one place, for every ledger row and the OTel
resource:

  instance        hostname:pid — the process, unique on a host for its life
  build_commit    settings.build_commit (the /v1/health field; '' unknown)
  instance_label  LOOM_INSTANCE_LABEL — set by whoever launched the process
                  ('' for the deployment; the isolated-server launcher sets
                  `iso:<worktree>:<port>`)

All three are fail-quiet: a stamp that cannot be read is ''. Values are
computed on first use per process (never at import, so a forked web worker
carries its own pid).
"""

from __future__ import annotations

import os
import socket
from typing import Any

_INSTANCE_LABEL_ENV = "LOOM_INSTANCE_LABEL"

_cache: dict[str, Any] = {}


def instance_id() -> str:
    """`hostname:pid` for this process. Recomputed after a fork."""
    pid = os.getpid()
    cached = _cache.get("instance")
    if cached is not None and cached[0] == pid:
        return str(cached[1])
    try:
        host = socket.gethostname() or os.environ.get("HOSTNAME", "") or "unknown"
    except Exception:  # noqa: BLE001 — a stamp never raises
        host = os.environ.get("HOSTNAME", "") or "unknown"
    value = f"{host}:{pid}"
    _cache["instance"] = (pid, value)
    return value


def instance_label() -> str:
    """The launcher's label for this process ('' when none was set)."""
    return str(os.environ.get(_INSTANCE_LABEL_ENV, "") or "")[:64]


class InstanceLabelMissing(RuntimeError):
    """A server process started with no `LOOM_INSTANCE_LABEL` outside the
    deployment."""


def require_instance_label(label: str | None, *, deployment: bool) -> str:
    """The launch rule: a process is either THE deployment
    (`LOOM_DEPLOYMENT=1`, set by the compose files and nothing else) or it
    carries a non-empty `LOOM_INSTANCE_LABEL`. Returns the label that will
    stamp every row from this process; raises `InstanceLabelMissing` with the
    fix in the message otherwise.

    Why hard: the label is the one stamp that says *who launched* a process, and hand-rolled
    launchers copy the worker and migration switches but not the label when it is optional.
    A row nobody can attribute is a row every reading has to exclude by guesswork; refusing
    to boot is the only check that reaches launchers this repo does not own."""
    value = str(label or "").strip()
    if value:
        return value[:64]
    if deployment:
        return ""
    raise InstanceLabelMissing(
        "refusing to start: LOOM_INSTANCE_LABEL is empty and LOOM_DEPLOYMENT is not set. "
        "Every process that shares a ClickHouse service with others must "
        "say who launched it: set LOOM_INSTANCE_LABEL=<who>:<what>:<port> (isolated servers "
        "use iso:<worktree>:<port>, CI ci:integration, `make server` dev:make-server:<port>), "
        "or LOOM_DEPLOYMENT=1 for the compose deployment only."
    )


def build_commit() -> str:
    """The short sha this process was built from, '' if unknown."""
    try:
        from server.settings import settings

        return str(settings.build_commit)
    except Exception:  # noqa: BLE001 — a stamp never raises
        return ""


def stamps() -> dict[str, str]:
    """The three ledger columns, in the order the tables declare them."""
    return {
        "build_commit": build_commit(),
        "instance": instance_id(),
        "instance_label": instance_label(),
    }


def stamp_row(row: dict[str, Any]) -> dict[str, Any]:
    """Add the three stamps to `row` in place (a writer's own value wins) and
    return it. `server.core.ch.insert` projects ledger rows onto the live column
    set, so a stamp column a table lacks costs the column, not the insert."""
    for k, v in stamps().items():
        if not row.get(k):
            row[k] = v
    return row


def resource_attributes() -> dict[str, str]:
    """OTel resource attributes carrying the same three stamps. Setting
    `service.instance.id` to hostname:pid replaces the `$HOSTNAME or
    "unknown"` default that left every non-container process anonymous."""
    s = stamps()
    attrs = {
        "service.instance.id": s["instance"],
        "loom.instance": s["instance"],
        "loom.build_commit": s["build_commit"],
    }
    if s["instance_label"]:
        attrs["loom.instance_label"] = s["instance_label"]
    return attrs
