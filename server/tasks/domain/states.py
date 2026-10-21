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
