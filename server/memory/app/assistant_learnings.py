"""File the rules an assistant states about itself as learnings queue candidates.

The extractor marks such a rule `about="agent"` when assistant-turn learnings
are on. It is never written as a memory: an end user can talk a bot into
saying anything, and an agent-level memory would reach every user of the bot.
It waits in `loom.learnings_queue` for an admin's `learnings.review`. The
candidate row holds the rule; each session that states it adds a row to
`loom.learnings_supporters` with that user's own quotes, so a user's words
live only on that user's rows.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from server.core.datetimes import naive_utc_now
from server.memory.app import blacklist_guard
from server.memory.app.drops import record_drop
from server.memory.app.pipeline_state import WorkflowState
from server.memory.domain import assistant_rules
from server.memory.domain.extracted_facts import judged_confidence
from server.memory.domain.sensitivity import reads_high
from server.memory.store import learnings as learnings_repo

log = logging.getLogger(__name__)

# The version of a candidate's filing row. A review writes `next_version()`, always
# newer, so a late duplicate filing never reverts a decision.
_FILED_VERSION = 1


async def file_assistant_rules(state: WorkflowState, facts: list[dict[str, Any]]) -> int:
    """File each rule as a pending candidate for (org, namespace, agent) and record
    this session as its supporter. Returns how many rules wrote a row (a new
    candidate or supporter; a session restating a rule it already backs writes
    none); a rule whose queue write fails is reported in `state.errors` and written
    nowhere."""
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
    agent = state.rule_agent
    if not key or not value or not agent:
        return False
    # A rule that reads as a credential is never filed: promoted, it would reach
    # every user of the bot.
    if reads_high(value):
        record_drop(state.org, state.namespace, "learnings.file", "sensitive", item=key)
        return False
    # Only what the assistant said is its rule: a fact citing no assistant turn
    # would show a user's words to the reviewer as the bot's.
    cited = assistant_rules.cited_assistant_turns(fact, state.messages)
    if not cited:
        record_drop(state.org, state.namespace, "learnings.file", "no_assistant_turn", item=key)
        return False
    # Refuse (blacklist), as memory.bulk_set does for every other extracted fact.
    if await _blacklisted(state, key, value, "rule not filed"):
        return False
    cid = assistant_rules.candidate_id(state.org, state.namespace, agent, key, value)
    row = await learnings_repo.find_candidate(state.org, state.namespace, cid)
    if row is not None and row["status"] != "pending":
        return False  # decided: support no longer changes anything
    # A promoted rule is served back to the bot, which restates it in other words; a
    # new wording under the key of a promoted rule is not filed again. A rejected rule
    # is never served back, so it does not stop a different rule under its key.
    if row is None and await learnings_repo.rule_promoted(state.org, state.namespace, agent, key):
        record_drop(state.org, state.namespace, "learnings.file", "already_promoted", item=key)
        return False
    new_supporter = not await learnings_repo.supporter_exists(
        state.org, state.namespace, cid, state.user_id, state.session_id
    )
    if new_supporter:
        asked, said = assistant_rules.cited_texts(fact, state.messages, cited)
        # A quote that matches a blacklist pattern is left blank, as one that reads
        # as a credential is. Checked whole: the cut and the folded spacing could
        # split a pattern or change its spacing.
        if asked and await _blacklisted(state, key, asked, "quote stored blank"):
            asked = ""
        if said and await _blacklisted(state, key, said, "quote stored blank"):
            said = ""
        asked, said = assistant_rules.bounded_quotes(asked, said)
        await learnings_repo.insert_supporter(
            {
                "org": state.org,
                "namespace": state.namespace,
                "agent": agent,
                "candidate_id": cid,
                "user_id": state.user_id,
                "session_id": state.session_id,
                "confidence": judged_confidence(fact),
                "asked": asked,
                "said": said,
                "created_at": naive_utc_now(),
            }
        )
    wrote = row is None or new_supporter
    if wrote:
        # Written again with every new supporter, at version 1: an erasure that
        # deleted the candidate after the read above (no supporter left yet) cannot
        # leave this supporter without it, and any review's newer version outranks
        # it, so a decision is never undone. It holds the rule and nothing of any
        # user's conversation.
        await learnings_repo.insert_row(
            {
                "candidate_id": cid,
                "org": state.org,
                "namespace": state.namespace,
                "agent": agent,
                "memory_key": key,
                "memory_type": "semantic",
                # The first wording stays: a restatement that folds to the same
                # candidate never changes the text the reviewer reads and promotes.
                "proposed_value": row["proposed_value"] if row else json.dumps(value),
                "supporter_count": 0,
                # Not judged here: learnings.list and promotion read the supporters' mean.
                "confidence_avg": 0.0,
                "supporters": [],
                "rationale": "",
                "source_kind": assistant_rules.SOURCE_KIND,
                "seeded_from_thread_id": None,
                "seeded_from_msg_id": None,
                "status": "pending",
                # The first proposal time keeps every version in one partition
                # (toYYYYMM(proposed_at)); two first filings across a month boundary
                # land in two, which FINAL still collapses.
                "proposed_at": row["proposed_at"] if row else naive_utc_now(),
                "version": _FILED_VERSION,
            }
        )
    return wrote


async def _blacklisted(state: WorkflowState, key: str, text: str, outcome: str) -> bool:
    """Whether `text` matches a blacklist pattern for the request's org and
    namespace. A match counts its pattern's hit and is logged with `outcome`, what
    the caller does with the text, as memory.bulk_set counts the entries it drops."""
    hit = await blacklist_guard.find_match(org=state.org, namespace=state.namespace, value=text)
    if hit is None:
        return False
    await blacklist_guard.note_hit(hit.pattern_id)
    log.warning(
        "learnings queue: blacklisted text, %s key=%r pattern=%s scope=%s",
        outcome,
        key,
        hit.pattern_id,
        hit.scope,
    )
    return True
