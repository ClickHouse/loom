"""A namespace's `loom.code_index_state` rows, keyed by `state_key`."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import TypedDict

from server.core.errors import stored_caller_text


class StateEntry(TypedDict, total=False):
    """One state key's value, its free-form detail and when it was written.
    Readers `.get` each field: a missing key reads as unset."""

    value: str
    detail: str
    updated_at: datetime


# state_key -> entry: what `CodeRepo.all_state` reads for one namespace.
IndexState = dict[str, StateEntry]

# The state key of an entry's bootstrap stage, whose detail is JSON.
STAGE_KEY = "bootstrap_stage"
# A failed clone's reasons, as the mirror job writes them into the bootstrap stage's
# `{"reason": ...}` detail; `code.repos` and `code.index_status` show only these, and
# `CLONE_FAILED` for a reason in no shape the job writes.
CLONE_TIMED_OUT = "clone timed out; retrying next pass"
CLONE_FAILED = "git clone failed"
SANDBOX_REQUIRED_REFUSAL = (
    "repository job refused: LOOM_CODE_SANDBOX_REQUIRED is set and the sandbox "
    "cannot run here (the 'sandbox probe failed' warning says why)"
)
_CLONE_EXIT_RE = re.compile(r"git clone failed \(exit -?\d+\)")
_CLONE_OSERROR = "clone failed: "
# A tier's detail after its base commit could not be read; anything after it is the
# harness's output, which names checkout paths on this host.
_BASE_UNKNOWN = "base unknown"


def shown_detail(key: str, detail: str) -> str:
    """State row `key`'s detail as `code.index_status` and `code.repos` return it to a
    viewer. The bootstrap stage's `{"reason": ...}` keeps a reason only in a shape the
    mirror job writes (`shown_clone_reason`); a tier's `base unknown` detail drops the
    harness output after it, which names this host's paths. Any other detail is as
    stored."""
    if detail.startswith(_BASE_UNKNOWN):
        return _BASE_UNKNOWN
    if key != STAGE_KEY or not detail.startswith("{"):
        return detail
    try:
        parsed = json.loads(detail)
    except ValueError:
        return detail
    if not isinstance(parsed, dict) or "reason" not in parsed:
        return detail
    shown = shown_clone_reason(str(parsed["reason"]))
    if shown == parsed["reason"]:
        return detail
    return json.dumps({**parsed, "reason": shown})


def shown_clone_reason(reason: str) -> str:
    """A failed clone's reason kept only in a shape the mirror job writes: the exit,
    the timeout, the sandbox refusal or `clone failed: <kind>`. Anything else (git's
    stderr, an OSError's path, a sandbox's message) can name this host's paths or
    another org's remote: it reads as CLONE_FAILED."""
    if reason in (CLONE_TIMED_OUT, CLONE_FAILED, SANDBOX_REQUIRED_REFUSAL):
        return reason
    if _CLONE_EXIT_RE.fullmatch(reason):
        return reason
    if reason.startswith(_CLONE_OSERROR):
        kind = reason.removeprefix(_CLONE_OSERROR)
        return reason if kind and stored_caller_text(kind) == kind else CLONE_FAILED
    return CLONE_FAILED
