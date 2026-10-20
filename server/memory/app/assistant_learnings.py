"""File the rules an assistant states about itself as learnings queue candidates.

The extractor marks such a rule `about="agent"` when assistant-turn learnings
are on. It is never written as a memory: an end user can talk a bot into
saying anything, and an agent-level memory would reach every user of the bot.
It waits in `loom.learnings_queue` for an admin's `learnings.review`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from server.core.datetimes import naive_utc_now
from server.memory.app.pipeline_state import WorkflowState
from server.memory.domain import assistant_rules
from server.memory.store import learnings as learnings_repo
from server.memory.store.rows import next_version

log = logging.getLogger(__name__)

# A rule the write-time judge graded UNCERTAIN, as the fact write path scores it.
_UNCERTAIN_CONFIDENCE = 0.75


async def file_assistant_rules(state: WorkflowState, facts: list[dict[str, Any]]) -> int:
    """File each rule as a pending candidate for (org, namespace, agent), or add
    this conversation as a supporter of the one already pending. Returns how many
    rules did either; a rule whose queue write fails is reported in `state.errors`
    and never written anywhere else."""
    filed = 0
    for fact in facts:
        key = str(fact.get("key") or "").strip()
        try:
            if await _file_one(state, fact):
                filed += 1
        except Exception as e:  # noqa: BLE001 — a queue write must not fail the user's extraction
            log.warning("learnings queue write failed for %r: %s", key, e)
            state.errors.append(f"learnings_queue_failed:{key}")
    return filed


async def _file_one(state: WorkflowState, fact: dict[str, Any]) -> bool:
    key = str(fact.get("key") or "").strip()
    value = str(fact.get("value") or "").strip()
    if not key or not value:
        return False
    agent = state.agent or "-"
    cid = assistant_rules.candidate_id(state.org, state.namespace, agent, key)
    entry = assistant_rules.supporter(
        state.org, state.namespace, agent, state.user_id, state.session_id, key
    )
    verdict = str(fact.get("judge_verdict") or "").upper()
    confidence = _UNCERTAIN_CONFIDENCE if verdict == "UNCERTAIN" else 1.0
    # Read, then write a new version: two sessions filing the same rule at the
    # same moment can lose one supporter, never the candidate.
    row = await learnings_repo.find_candidate(state.org, state.namespace, cid)
    if row is None:
        now = naive_utc_now()
        new_row: dict[str, Any] = {
            "candidate_id": cid,
            "org": state.org,
            "namespace": state.namespace,
            "agent": agent,
            "memory_key": key,
            "memory_type": "semantic",
            "proposed_value": json.dumps(value),
            "supporter_count": 1,
            "confidence_avg": confidence,
            "supporters": [entry],
            "supporter_users": [state.user_id],
            "rationale": assistant_rules.rationale(fact, state.messages),
            "source_kind": assistant_rules.SOURCE_KIND,
            "seeded_from_thread_id": state.session_id if state.session_id != "-" else None,
            "seeded_from_msg_id": None,
            "status": "pending",
            "proposed_at": now,
            "version": next_version(),
        }
    else:
        support = assistant_rules.add_supporter(row, entry, state.user_id, confidence)
        if support is None:
            return False
        new_row = {
            **row,
            **support,
            "org": state.org,
            "namespace": state.namespace,
            "candidate_id": str(row["candidate_id"]),
            "version": next_version(),
        }
    await learnings_repo.insert_row(new_row)
    return True
