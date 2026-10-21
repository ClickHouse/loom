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

from server.core import ch as core_ch
from server.core.ch_params import fitting
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.llm import client as llm_client
from server.llm import customer
from server.memory.app import profile as profile_app
from server.memory.domain.profile_facts import render_fold_input
from server.memory.domain.provenance import CLI_ACTOR_PREFIX, PIPELINE_ACTORS
from server.memory.domain.sensitivity import value_reads_high
from server.memory.domain.trust import subject_of
from server.memory.store.profile_sources import user_facts
from server.observability import cost_accumulator
from server.observability.genai import llm_stage
from server.settings import settings
from server.settings.budgets import llm_max_tokens
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.holds import Held, hold_seconds
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# Identities (org, namespace, user_id) whose fold was held (`customer.GROUP_HOLDS`), each
# until its hold ends: the selection leaves them out meanwhile, so the pass's slots go to
# identities whose fold can run.
_HELD: Held[tuple[str, str, str]] = Held()

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
        ch = core_ch.get_client()
        # Real users (user_id != '-') in opted-in namespaces with enough durable
        # preference/decision memories to be worth folding, whose section was
        # not rewritten within the cooldown. The cooldown is part of the scan,
        # before the LIMIT: filtering after it would hand back the same
        # just-folded top identities every pass and never reach the rest.
        # `updated_at`, not `created_at`: a rewrite keeps the section's
        # creation time. The fold's model call is for the newest principal that
        # wrote the identity's facts (`writer`; a pipeline is none).
        held = fitting(sorted(_HELD.current()))  # one parameter, one form field
        held_sql = (
            "AND (m.org, m.namespace, m.user_id) NOT IN {held:Array(Tuple(String, String, String))}"
            if held
            else ""
        )
        identities = await ch.query_all(
            """
            SELECT m.org AS org, m.namespace AS namespace, m.user_id AS user_id,
                   count() AS n,
                   argMaxIf(m.actor, m.version,
                            m.actor != '' AND NOT has({pipeline:Array(String)}, m.actor)
                            AND NOT startsWith(m.actor, {cli:String})) AS writer
            FROM loom.memory AS m FINAL
            INNER JOIN loom.namespaces AS n FINAL
                    ON m.org = n.org AND m.namespace = n.namespace
            WHERE n.active = true AND n.consolidation_enabled = true
              AND m.tombstoned_at IS NULL
              AND m.user_id != '-'
              AND m.memory_type IN {types:Array(String)}
              -- an org whose model calls are switched off waits, and takes no slot;
              -- so does an identity whose fold is held (`held`)
              AND NOT has({off:Array(String)}, m.org) /*held*/
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
            """.replace("/*held*/", held_sql),
            params={
                "minm": int(cfg.min_memories),
                "lim": int(max(1, cfg.max_identities_per_pass)),
                "types": ["preference", "decision"],
                "label": cfg.label,
                "cool": int(max(0, cfg.cooldown_seconds)),
                "off": sorted(await customer.switched_off_orgs()),
                **({"held": held} if held else {}),
                "pipeline": sorted(PIPELINE_ACTORS),
                "cli": CLI_ACTOR_PREFIX,
            },
        )
        result.identities_scanned = len(identities)
        for row in identities:
            try:
                if await _consolidate_identity(ch, row, cfg):
                    result.profiles_written += 1
            except customer.GROUP_HOLDS as e:
                # A held call is no failure: the identity is folded once its org or
                # identity is back, and takes no slot until its hold ends.
                _HELD.hold(
                    [(str(row["org"]), str(row["namespace"]), str(row["user_id"]))], hold_seconds(e)
                )
            except customer.HOLDS:
                continue  # a switch gone unread holds every identity: the next pass tries
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


async def _consolidate_identity(
    ch: Any, row: dict[str, Any], cfg: ProfileConsolidationConfig
) -> bool:
    # The fold reads only what is current and sees each fact's date, oldest
    # first, so the prompt's recency rule has something to act on. FINAL
    # already keeps one live version per key.
    rows = await user_facts(
        org=row["org"],
        namespace=row["namespace"],
        user_id=row["user_id"],
        types=["preference", "decision"],
        limit=int(cfg.memory_limit),
        user_keys_only=True,
        scope=WHOLE_NAMESPACE,
        ch=ch,
    )
    # An unlabelled row (copy_to, an import, before the sweep) holding a credential is never
    # sent to the fold model, whose output becomes a plain profile section.
    rows = [r for r in rows if not value_reads_high(r.get("value"))]
    facts = render_fold_input(rows)
    if not facts:
        return False

    # Bind an accumulator so this worker's spend is visible. The drain is what
    # makes it visible: `begin_op()` only gives `report_llm()` a place to write.
    row_started = time.monotonic()
    cost_accumulator.begin_op()
    try:
        with (
            llm_stage("profile_fold"),
            customer.for_org(
                str(row["org"]),
                namespace=str(row["namespace"]),
                subject=subject_of(str(row.get("writer") or "")),
                user_id=str(row["user_id"]),
            ),
        ):
            client = (
                llm_client.get_llm_client_for_model(cfg.model)
                if cfg.model
                else llm_client.get_llm_client()
            )
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

    await profile_app.write_folded_section(
        org=row["org"],
        namespace=row["namespace"],
        user_id=row["user_id"],
        label=cfg.label,
        value=text,
        memory_ids=[str(r["memory_id"]) for r in rows],
    )
    return True
