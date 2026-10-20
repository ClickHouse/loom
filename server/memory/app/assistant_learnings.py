"""File the rules an assistant states about itself as learnings queue candidates.

The extractor marks such a rule `about="agent"` when assistant-turn learnings
are on. It is never written as a memory: an end user can talk a bot into
saying anything, and an agent-level memory would reach every user of the bot.
It waits in `loom.learnings_queue` for an admin's `learnings.review`; each
session that states it adds a row to `loom.learnings_supporters`, and the
earliest of those rows names the user who filed it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
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

# The learning agent a deferred replay restored from its queue payload.
_REPLAY_AGENT: ContextVar[str | None] = ContextVar("loom_learning_agent", default=None)


def token_pinned() -> bool:
    """Whether the request's token is bound to one end user (an end-user token,
    or one minted with `read_user_id`)."""
    from server.auth.context import current_auth

    auth = current_auth()
    return auth is not None and bool(auth.read_user_id)


def learning_agent(body_agent: str) -> str:
    """The agent a filed rule is for. A token bound to one end user files for the
    agent its issuer signed, and for none ('': nothing is filed) when it names
    none, never for the body's. Any other caller files for the body's agent. A
    deferred replay uses the one resolved when the request was queued."""
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is None:
        replayed = _REPLAY_AGENT.get()
        return replayed if replayed is not None else (body_agent or "-")
    if auth.read_user_id:
        return auth.agent_principal.removeprefix("agent:")
    return body_agent or "-"


@contextmanager
def replay_learning_agent(agent: str | None) -> Iterator[None]:
    """Run a deferred replay under the learning agent its request resolved; None
    (a payload queued before it was stored) falls back to the body's agent."""
    token = _REPLAY_AGENT.set(agent)
    try:
        yield
    finally:
        _REPLAY_AGENT.reset(token)


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
    agent = state.learning_agent
    if not key or not value or not agent:
        return False
    cid = assistant_rules.candidate_id(state.org, state.namespace, agent, key, value)
    row = await learnings_repo.find_candidate(state.org, state.namespace, cid)
    if row is not None and row["status"] != "pending":
        return False  # decided: support no longer changes anything
    # The supporter row first: the earliest one names who filed the candidate, so
    # a candidate row never exists without it. A session already backing it is
    # left as it is, so that row keeps its time.
    if not await learnings_repo.supporter_exists(
        state.org, state.namespace, cid, state.user_id, state.session_id
    ):
        await learnings_repo.insert_supporter(
            {
                "org": state.org,
                "namespace": state.namespace,
                "candidate_id": cid,
                "user_id": state.user_id,
                "session_id": state.session_id,
                "confidence": judged_confidence(fact),
                "created_at": naive_utc_now(),
            }
        )
    if row is None:
        await learnings_repo.insert_row(
            {
                "candidate_id": cid,
                "org": state.org,
                "namespace": state.namespace,
                "agent": agent,
                "memory_key": key,
                "memory_type": "semantic",
                "proposed_value": json.dumps(value),
                "supporter_count": 0,
                "confidence_avg": judged_confidence(fact),
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
    return True
