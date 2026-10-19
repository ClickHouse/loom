"""Profile-consolidation worker.

Folds a user's durable preference/decision memories into ONE versioned profile
section (`memory_type='profile'`, label = `settings.profile_consolidation_label`)
that the harness loads via `profile.render` / `context.pack`. Default-off +
namespace opt-in (`consolidation_enabled`); makes LLM calls. Rate-limited by the
section's own age: a user whose section was rewritten within the cooldown is
not a candidate.

loom is NOT in the agent loop: this writes the profile DATA; the harness injects
it (never per-turn: re-injecting it every turn bloats context). The section is
`memory_type='profile'`, which is non-searchable, so it never pollutes recall.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.memory.domain.sensitivity import value_reads_high
from server.observability import cost_accumulator
from server.observability.genai import llm_stage
from server.settings import settings
from server.settings.budgets import llm_max_tokens
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.status import record_pass

log = logging.getLogger(__name__)

_FOLD_SYSTEM = (
    "You maintain a concise standing profile for an AI assistant. Given a list "
    "of durable facts the user has stated (preferences and decisions), write a "
    "tight profile the assistant should always know: stable preferences, tools, "
    "working style, and standing decisions. Omit one-off events and time-bound "
    "details. Each fact is dated: when two facts disagree about the same thing, "
    "keep the MOST RECENT one and drop the older (a preference the user changed "
    "is gone, not 'formerly'). If a fact records that the user asked you not to "
    "remember or to forget something, leave that something out of the profile "
    "entirely. No preamble — output only the profile text, under 600 characters."
)


@dataclass
class ProfileConsolidationConfig:
    # Read when the config is built (each pass), not when this module is imported.
    max_identities_per_pass: int = field(
        default_factory=lambda: settings.profile_consolidation_max_identities_per_pass
    )
    min_memories: int = field(default_factory=lambda: settings.profile_consolidation_min_memories)
    memory_limit: int = field(default_factory=lambda: settings.profile_consolidation_memory_limit)
    cooldown_seconds: int = field(
        default_factory=lambda: settings.profile_consolidation_cooldown_seconds
    )
    label: str = field(default_factory=lambda: settings.profile_consolidation_label)
    model: str = field(default_factory=lambda: settings.profile_consolidation_model)


@dataclass
class ProfileConsolidationResult:
    identities_scanned: int = 0
    profiles_written: int = 0
    errors: int = 0


async def run_pass(
    cfg: ProfileConsolidationConfig | None = None,
) -> ProfileConsolidationResult:
    cfg = cfg or ProfileConsolidationConfig()
    result = ProfileConsolidationResult()
    with record_pass("profile_consolidation") as rec:
        from server.core.ch import get_client

        ch = get_client()
        # Real users (user_id != '-') in opted-in namespaces with enough durable
        # preference/decision memories to be worth folding, whose section was
        # not rewritten within the cooldown. The cooldown is part of the scan,
        # before the LIMIT: filtering after it would hand back the same
        # just-folded top identities every pass and never reach the rest.
        # `updated_at`, not `created_at`: a rewrite keeps the section's
        # creation time.
        identities = await ch.query_all(
            """
            SELECT m.org AS org, m.namespace AS namespace, m.user_id AS user_id,
                   count() AS n
            FROM loom.memory AS m FINAL
            INNER JOIN loom.namespaces AS n FINAL
                    ON m.org = n.org AND m.namespace = n.namespace
            WHERE n.active = true AND n.consolidation_enabled = true
              AND m.tombstoned_at IS NULL
              AND m.user_id != '-'
              AND m.memory_type IN {types:Array(String)}
              AND (m.org, m.namespace, m.user_id) NOT IN (
                  SELECT org, namespace, user_id
                  FROM loom.memory FINAL
                  WHERE memory_type = 'profile' AND memory_key = {label:String}
                    AND tombstoned_at IS NULL
                    AND updated_at > now() - toIntervalSecond({cool:UInt32})
              )
            GROUP BY m.org, m.namespace, m.user_id
            HAVING n >= {minm:UInt32}
            ORDER BY n DESC
            LIMIT {lim:UInt32}
            """,
            params={
                "minm": int(cfg.min_memories),
                "lim": int(max(1, cfg.max_identities_per_pass)),
                "types": ["preference", "decision"],
                "label": cfg.label,
                "cool": int(max(0, cfg.cooldown_seconds)),
            },
        )
        result.identities_scanned = len(identities)
        for row in identities:
            try:
                if await _consolidate_identity(ch, row, cfg):
                    result.profiles_written += 1
            except Exception as e:
                result.errors += 1
                log.exception("profile consolidation failed for %s", dict(row))
                pass_context.note_failure(e, str(row.get("namespace") or ""))
        rec.outcome = {
            "identities_scanned": result.identities_scanned,
            "profiles_written": result.profiles_written,
            "errors": result.errors,
        }
    return result


def render_fold_input(rows: list[dict[str, Any]]) -> str:
    """The fold's input: one dated line per fact, oldest first, so the newest
    statement of a changed preference is the last the model reads. Rows with
    an empty value are dropped; "" when nothing remains."""
    dated: list[tuple[str, str]] = []
    for r in rows:
        value = " ".join(str(r.get("value") or "").split())
        if not value:
            continue
        since = str(r.get("since") or "")[:10] or "undated"
        dated.append((since, value))
    dated.sort(key=lambda t: (t[0] == "undated", t[0]))
    return "\n".join(f"- [{since}] {value}" for since, value in dated)


async def _consolidate_identity(
    ch: Any, row: dict[str, Any], cfg: ProfileConsolidationConfig
) -> bool:
    # The fold reads only what is current — no superseded or
    # rejected version, nothing past its stated end or TTL — and sees each
    # fact's date, oldest first, so the prompt's recency rule has something
    # to act on. FINAL already keeps one live version per key.
    rows = await ch.query_all(
        """
        SELECT value, toString(coalesce(valid_at, created_at)) AS since
        FROM loom.memory FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND user_id = {uid:String} AND tombstoned_at IS NULL
          -- a high row is readable by key only, never folded into the profile
          AND sensitivity != 'high'
          AND memory_type IN ('preference', 'decision')
          AND status NOT IN ('superseded', 'rejected', 'proposed')  -- proposed: not a fact yet
          AND (valid_to IS NULL OR valid_to > now())
          AND (expires_at IS NULL OR expires_at > now())
          AND NOT arrayExists(t -> startsWith(t, 'attribute:'), tags)
          AND NOT has(tags, 'about:other')
          -- The fold reads facts whose subject is the user (the extractor
          -- keys them `user_…`; most preference/decision rows) and never a
          -- stored request to forget.
          AND startsWith(memory_key, 'user_')
          AND NOT has(tags, 'forget')
        ORDER BY confidence DESC, created_at DESC
        LIMIT {lim:UInt32}
        """,
        params={
            "org": row["org"],
            "ns": row["namespace"],
            "uid": row["user_id"],
            "lim": int(cfg.memory_limit),
        },
    )
    # An unlabelled row (copy_to, an import, before the sweep) holding a credential is never
    # sent to the fold model, whose output becomes a plain profile section.
    rows = [r for r in rows if not value_reads_high(r.get("value"))]
    facts = render_fold_input(rows)
    if not facts:
        return False

    from server.llm.client import get_llm_client, get_llm_client_for_model

    # Bind an accumulator so this worker's spend is visible. The drain is what
    # makes it visible: `begin_op()` only gives `report_llm()` a place to write.
    row_started = time.monotonic()
    cost_accumulator.begin_op()
    try:
        with llm_stage("profile_fold"):
            client = get_llm_client_for_model(cfg.model) if cfg.model else get_llm_client()
            out = await client.complete(
                system=_FOLD_SYSTEM,
                user=facts,
                max_tokens=llm_max_tokens("profile_fold"),
            )
    finally:
        emit_worker_cost_event_for(
            org=str(row["org"]),
            namespace=str(row["namespace"]),
            operation="worker.profile_consolidation",
            started=row_started,
        )
    text = (getattr(out, "text", "") or "").strip()
    if not text:
        return False

    from server.memory.app.profile import write_folded_section

    await write_folded_section(
        org=row["org"],
        namespace=row["namespace"],
        user_id=row["user_id"],
        label=cfg.label,
        value=text,
    )
    return True
