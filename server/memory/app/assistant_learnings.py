"""File the rules an assistant states about itself as learnings queue candidates.

The extractor marks such a rule `about="agent"` when assistant-turn learnings
are on. It is never written as a memory: an end user can talk a bot into
saying anything, and an agent-level memory would reach every user of the bot.
It waits in `loom.learnings_queue` for an admin's `learnings.review`; each
session that states it adds a row to `loom.learnings_supporters`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from server.core.datetimes import naive_utc_now
from server.memory.app.pipeline_state import WorkflowState
from server.memory.domain import assistant_rules
from server.memory.domain.extracted_facts import judged_confidence
from server.memory.store import learnings as learnings_repo

log = logging.getLogger(__name__)

# The version of a candidate's filing row. A review writes `next_version()`, always
# newer, so a late duplicate filing never reverts a decision.
_FILED_VERSION = 1


@dataclass(frozen=True)
class LearningsPin:
    """What the token decides about filing. A token bound to one end user cannot
    turn filing on, and files for the agent its issuer signed, not the body's."""

    pinned: bool = False
    agent: str = ""


_REPLAY_PIN: ContextVar[LearningsPin | None] = ContextVar("loom_learnings_pin", default=None)


def request_pin() -> LearningsPin:
    """The pin of the request in flight, or the one a deferred replay restored."""
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is None:
        return _REPLAY_PIN.get() or LearningsPin()
    return LearningsPin(
        pinned=bool(auth.read_user_id),
        agent=auth.agent_principal.removeprefix("agent:"),
    )


@contextmanager
def replay_pin(pin: LearningsPin) -> Iterator[None]:
    """Run a deferred replay under the pin its enqueuing token had."""
    token = _REPLAY_PIN.set(pin)
    try:
        yield
    finally:
        _REPLAY_PIN.reset(token)


async def file_assistant_rules(state: WorkflowState, facts: list[dict[str, Any]]) -> int:
    """File each rule as a pending candidate for (org, namespace, agent) and record
    this session as its supporter. Returns how many rules reached the queue; a
    rule whose queue write fails is reported in `state.errors` and written nowhere."""
    filed = 0
    for fact in facts:
        key = str(fact.get("key") or "").strip()
        try:
            if await _file_one(state, key, fact):
                filed += 1
        except Exception as e:  # noqa: BLE001 — a queue write must not fail the user's extraction
            log.warning("learnings queue write failed for %r: %s", key, e)
            state.errors.append(f"learnings_queue_failed:{key}")
    return filed


async def _file_one(state: WorkflowState, key: str, fact: dict[str, Any]) -> bool:
    value = str(fact.get("value") or "").strip()
    if not key or not value:
        return False
    agent = state.learning_agent or state.agent or "-"
    cid = assistant_rules.candidate_id(state.org, state.namespace, agent, key, value)
    confidence = judged_confidence(fact)
    row = await learnings_repo.find_candidate(state.org, state.namespace, cid)
    if row is not None and row["status"] != "pending":
        return False  # decided: support no longer changes anything
    if row is None:
        await learnings_repo.insert_row(
            {
                "candidate_id": cid,
                "org": state.org,
                "namespace": state.namespace,
                "agent": agent,
                "filed_by_user": state.user_id,
                "memory_key": key,
                "memory_type": "semantic",
                "proposed_value": json.dumps(value),
                "supporter_count": 0,
                "confidence_avg": confidence,
                "supporters": [],
                "rationale": assistant_rules.rationale(fact, state.messages),
                "source_kind": assistant_rules.SOURCE_KIND,
                "seeded_from_thread_id": state.session_id if state.session_id != "-" else None,
                "seeded_from_msg_id": None,
                "status": "pending",
                "proposed_at": naive_utc_now(),
                "version": _FILED_VERSION,
            }
        )
    await learnings_repo.insert_supporter(
        {
            "org": state.org,
            "namespace": state.namespace,
            "candidate_id": cid,
            "user_id": state.user_id,
            "session_id": state.session_id,
            "confidence": confidence,
            "created_at": naive_utc_now(),
        }
    )
    return True
