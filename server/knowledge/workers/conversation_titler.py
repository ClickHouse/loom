"""Conversation-titler worker.

LLM-generates a short title for *untitled* conversation threads — those built
by `conversation.append` without a prior `conversation.create`, so they have no
`loom.conversation_threads` metadata row. The title lands on a fresh metadata
row and surfaces in the dashboard's Conversations list (cards lead with the title), turning
a raw thread_id into something readable.

On by default (`conversation_titler_enabled`), gated per-namespace by
`conversation_logging_enabled` — any namespace that records conversations gets
readable titles, and the Conversations page's Record conversations switch
doubles as the off-switch.
Makes one LLM call per untitled thread; no-ops when the LLM is the fake dev
provider, so on-by-default is safe out of the box.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypedDict, cast

from server.core.datetimes import naive_utc_now_seconds
from server.knowledge.domain.conversations import (
    metadata_object,
    title_refused,
    with_title_refused,
)
from server.knowledge.store import conversations as conversation_repo
from server.knowledge.store.conversations import ThreadInsertRow
from server.llm import customer
from server.memory.app import extraction_policy
from server.memory.domain.extraction_policy import with_policy
from server.memory.domain.sensitivity import reads_high
from server.observability import cost_accumulator
from server.observability.genai import llm_stage
from server.settings import settings
from server.settings.budgets import llm_max_tokens
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.status import record_pass

log = logging.getLogger(__name__)

_TITLE_SYSTEM = (
    "Write a short, specific title for this conversation — at most 60 "
    "characters, capturing the topic. No quotes, no trailing punctuation, no "
    "preamble. Output only the title."
)
# The opening turns the title is written from.
_OPENING_TURNS = 8  # not a setting: the shape of the title prompt


@dataclass
class ConversationTitlerConfig:
    # Read when a pass builds its config, not at import: a settings change
    # (a test override, a runtime reload) reaches the next pass.
    max_threads_per_pass: int = field(
        default_factory=lambda: settings.conversation_titler_max_threads_per_pass
    )
    min_turns: int = field(default_factory=lambda: settings.conversation_titler_min_turns)
    window_hours: int = field(default_factory=lambda: settings.conversation_titler_window_hours)


@dataclass
class ConversationTitlerResult:
    titled: int = 0
    # Threads marked so they are never titled: the model's title, or every opening
    # turn, read as high.
    refused: int = 0
    skipped_titled: int = 0
    skipped_deleted: int = 0
    errors: int = 0


class TitleCandidate(TypedDict):
    """A recent thread with enough turns in a namespace that records."""

    org: str
    namespace: str
    thread_id: str
    agent: str
    n: int
    created_at: datetime


async def _make_title(
    row: TitleCandidate,
    cfg: ConversationTitlerConfig,
    llm: Any,
    existing: ThreadInsertRow | None = None,
) -> Literal["titled", "refused"] | None:
    """Title one thread. Says whether it stored a title, marked the thread refused, or
    left it as it was (None) for a later pass."""
    turns = await conversation_repo.opening_turns(
        str(row["org"]), str(row["namespace"]), str(row["thread_id"]), limit=_OPENING_TURNS
    )
    # A turn that reads as high never reaches the model: the title is listed to
    # every reader of the namespace. A turn can be as long as the request cap allows, so
    # the check runs in a worker thread.
    spoken = [r for r in turns if str(r.get("content") or "").strip()]
    plain = await asyncio.to_thread(
        lambda: [r for r in spoken if not reads_high(str(r["content"]))]
    )
    snippet = "\n".join(f"{r['role']}: {str(r['content'])[:500]}" for r in plain)
    if not snippet:
        if spoken and len(turns) >= _OPENING_TURNS:
            # Every opening turn reads as high and no later turn can join them.
            # The thread is marked, so it stops taking one of the scan's slots on every
            # pass. With fewer turns it stays unmarked: a plain turn may still arrive.
            await _store_title(row, existing, "", refused=True)
            return "refused"
        return None
    # A title is written from the thread's own turns and stored: the model is
    # given the operator's extraction policy, as extraction is.
    policy = await extraction_policy.effective(str(row["org"]), str(row["namespace"]))

    # Bind a cost accumulator, label the leg, and drain it into an AgentEvent
    # afterwards: `begin_op()` alone only gives `report_llm()` somewhere to
    # write, and without the drain the worker's spend never reaches
    # agent_events or a cost panel.
    row_started = time.monotonic()
    cost_accumulator.begin_op()
    try:
        with llm_stage("title"), customer.for_org(str(row["org"]), namespace=str(row["namespace"])):
            out = await llm.complete(
                system=with_policy(_TITLE_SYSTEM, policy),
                user=snippet,
                max_tokens=llm_max_tokens("title"),
            )
    finally:
        emit_worker_cost_event_for(
            org=str(row["org"]),
            namespace=str(row["namespace"]),
            operation="worker.conversation_titler",
            started=row_started,
        )
    title = (getattr(out, "text", "") or "").strip().strip('"').strip()[:80]
    if not title:
        return None
    # A title that reads as high is never stored. The thread is marked instead,
    # so later passes do not pay for the same title again.
    refused = reads_high(title)
    await _store_title(row, existing, "" if refused else title, refused=refused)
    return "refused" if refused else "titled"


async def _store_title(
    row: TitleCandidate, existing: ThreadInsertRow | None, title: str, *, refused: bool
) -> None:
    now = naive_utc_now_seconds()
    if existing is not None:
        # A stored row (conversation.create with no title): carry every
        # column forward — its partition (created_at), agent, metadata,
        # run_id, user_id — and change only the title and the version.
        new_row: ThreadInsertRow = {**existing, "title": title, "updated_at": now}
    else:
        new_row = {
            "org": row["org"],
            "namespace": row["namespace"],
            "agent": row["agent"],
            "thread_id": row["thread_id"],
            "title": title,
            "metadata": "{}",
            "run_id": "-",
            "user_id": "-",
            "created_at": row.get("created_at") or now,
            "updated_at": now,
            "tombstoned_at": None,
        }
    if refused:
        new_row["metadata"] = with_title_refused(metadata_object(new_row["metadata"]))
    await conversation_repo.insert_thread(new_row)


async def run_pass(cfg: ConversationTitlerConfig | None = None) -> ConversationTitlerResult:
    cfg = cfg or ConversationTitlerConfig()
    result = ConversationTitlerResult()
    with record_pass("conversation_titler") as rec:
        from server.llm.client import get_llm_client

        # No-op without a real LLM (fake dev provider → stub titles); makes
        # on-by-default safe out of the box.
        llm = get_llm_client()
        if str(getattr(llm, "model", "")).lower().startswith("fake"):
            log.debug("conversation_titler: fake LLM (%s) — skipping", getattr(llm, "model", ""))
            rec.outcome = {"candidates": 0, "titled": 0}
            return result

        # Recent threads with enough turns in opted-in namespaces. Over-fetch
        # (many already have titles) and stop once the cap's model calls are made.
        rows = await conversation_repo.titler_candidates(
            window_hours=int(cfg.window_hours),
            min_turns=int(cfg.min_turns),
            limit=int(max(1, cfg.max_threads_per_pass) * 5),
            exclude_orgs=await customer.switched_off_orgs(),  # theirs wait, taking no slot
        )
        candidates = cast(list[TitleCandidate], rows)
        stored = await conversation_repo.thread_rows(
            [(str(r["org"]), str(r["namespace"]), str(r["thread_id"])) for r in candidates]
        )
        for row in candidates:
            # A refused thread is written like a titled one (a model call or a mark), so it
            # takes a slot.
            if result.titled + result.refused >= cfg.max_threads_per_pass:
                break
            try:
                # Re-checked on the row itself: the scan's exclusion ran on
                # a merge state the row read may have moved past.
                existing = stored.get(
                    (str(row["org"]), str(row["namespace"]), str(row["thread_id"]))
                )
                if existing is not None and existing.get("tombstoned_at") is not None:
                    result.skipped_deleted += 1
                    continue
                if existing is not None and (
                    str(existing.get("title") or "").strip()
                    or title_refused(existing.get("metadata"))
                ):
                    result.skipped_titled += 1
                    continue
                made = await _make_title(row, cfg, llm, existing)
                if made == "titled":
                    result.titled += 1
                elif made == "refused":
                    result.refused += 1
            except customer.HOLDS:
                # A held call is no failure: the thread is titled once the org is back
                # on and the policy is read.
                continue
            except Exception as e:
                result.errors += 1
                log.exception("conversation titling failed for %s", dict(row))
                pass_context.note_failure(e, str(row.get("namespace") or ""))
        rec.outcome = {
            "candidates": len(candidates),
            "titled": result.titled,
            "refused": result.refused,
            "skipped_titled": result.skipped_titled,
            "skipped_deleted": result.skipped_deleted,
            "errors": result.errors,
        }
    return result
