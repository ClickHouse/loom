"""The task inbox plane's enumerations: the values the wire and the rows share."""

from __future__ import annotations

from typing import Literal

TaskStatus = Literal[
    "triage",
    "todo",
    "in_progress",
    "blocked",
    "deferred",
    "done",
    "wont_do",
    "failed",
    "cancelled",
]
Disclosure = Literal["confidential", "internal", "public_ok"]
PolicyMode = Literal["deny", "review", "auto"]
BlockedOn = Literal["sender", "human", "external"]
RunStatus = Literal["active", "completed", "failed", "cancelled"]

# An inbox's `webhook_secret_ref` must name an environment variable with this
# prefix: any other name could hand a server secret to a URL the caller chose
# as the signing key.
WEBHOOK_SECRET_PREFIX = "LOOM_WEBHOOK_SECRET_"
