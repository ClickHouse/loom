"""What the code.bisect_candidates stages share: the issue's facts, the widening and
mark-stage records, and the request's deadline and cut."""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_MAX_PATHS = 40


@dataclass
class IssueFacts:
    """What the issue's QA row and graph say."""

    fact: dict[str, Any] = field(default_factory=dict)
    bad: str = ""
    good: str = ""
    master: bool = False  # the bug reproduces on the development branch
    opened_at: datetime | None = None  # a PR merged after it is not the culprit
    introduced: str = ""
    caused: list[int] = field(default_factory=list)  # PRs the `caused_by` edges blame
    fixes: list[int] = field(default_factory=list)  # PRs whose `fixes` edge names the issue
    qa_ok: bool = True
    stats: dict[str, Any] = field(default_factory=dict)  # the issue corpus's vocabulary
    text: str = ""  # the report's title and body head, for the fusion
    # a merge-time window when no release versions bound the scan
    before: datetime | None = None
    after: datetime | None = None


@dataclass
class MarkStage:
    """What the marks stage did: its status, the bounded git calls, and one Mark per
    candidate."""

    status: str = "skipped"  # ok | skipped | ledger_failed
    git_calls: int = 0
    marks: dict[int, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        ms = list(self.marks.values())
        return {
            "status": self.status,
            "git_calls": self.git_calls,
            "demoted": sum(1 for m in ms if m.demoted),
            "promoted": sum(1 for m in ms if m.promoted),
            "relands": sum(1 for m in ms if m.reland_of or m.reland_via),
        }


# `deadline_ms`: the instant the call answers by, and the optional stages it cut. An
# optional stage (fix blame, marks, the duplicate check's wait, the rerank) waits no
# longer than its own budget or what is left, whichever is less, and one that would start
# with too little left is skipped. The scan itself always runs: it is the answer.
_DEADLINE: ContextVar[float | None] = ContextVar("bisect_deadline", default=None)
_CUT: ContextVar[list[str] | None] = ContextVar("bisect_cut", default=None)
# Less time left than this and the stage is not started.
STAGE_MIN_S = 0.5
RERANK_MIN_S = 1.5


def _budget(default_s: float) -> float:
    """A stage's wall budget: its own, or what is left of `deadline_ms` when less."""
    end = _DEADLINE.get()
    if end is None:
        return default_s
    return max(0.0, min(default_s, end - time.monotonic()))


def _cut(stage: str) -> None:
    cut = _CUT.get()
    if cut is not None and stage not in cut:
        cut.append(stage)


def _short(need_s: float, stage: str) -> bool:
    """True, with `stage` named as cut, when the deadline leaves less than `need_s`."""
    end = _DEADLINE.get()
    if end is None or end - time.monotonic() >= need_s:
        return False
    _cut(stage)
    return True


def _expired(stage: str) -> bool:
    """After a timeout: True, with `stage` named as cut, when the deadline is what ran out."""
    end = _DEADLINE.get()
    if end is None or end - time.monotonic() > 0.05:
        return False
    _cut(stage)
    return True


@dataclass
class Widening:
    """What bounded a frameless scan."""

    paths: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    scope_patterns: list[str] = field(default_factory=list)
    widened_by: list[str] = field(default_factory=list)
    component: str = ""
    fix: dict[str, Any] | None = None
