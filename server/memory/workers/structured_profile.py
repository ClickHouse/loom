"""Structured-profile worker.

Keeps each user's structured profile current in the shape the namespace's
profile schema gives it (`profile_schema.set`): for every user whose profile
is missing, built under another schema, or behind the user's memories (and
older than the cooldown), one model call fills the schema's properties from
the user's current facts, and the answer, checked against the schema, is
written as a new version of the user's `structured_profile` profile row.
Only namespaces with a schema are read, so a deployment that sets none makes
no model call. An org whose model calls are switched off is passed over.

Like the free-text fold, the facts come from `profile_sources`: never a
`high` row, never a fact about someone else. A user whose facts are all gone
gets an all-null profile without a model call.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.llm import customer
from server.memory.domain import profile_schema
from server.memory.domain.prompt_fence import FENCE_NOTE, fence, new_nonce
from server.memory.domain.provenance import pipeline_actor
from server.memory.domain.sensitivity import value_reads_high
from server.memory.store import profile_schemas, profile_sources
from server.observability import cost_accumulator
from server.observability.genai import llm_stage
from server.settings import settings
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# The actor its writes carry: no request is in flight.
WORKER_ACTOR = "process:structured-profile-worker"
# Namespaces with a schema one pass reads.
_MAX_SCHEMAS = 500

_SYSTEM = (
    "You fill a structured profile of one user for an AI assistant. Given the "
    "facts the user has stated, each dated, return a JSON object with exactly "
    "the properties listed below and nothing else. Use only what the facts say; "
    "a property the facts do not settle is null. When two facts disagree, the "
    "MOST RECENT wins. If a fact records that the user asked not to remember or "
    "to forget something, leave that out. Output only the JSON object. "
    f"{FENCE_NOTE}\n\n"
    "Properties:\n"
)


@dataclass
class StructuredProfileConfig:
    # Read when the config is built (each pass), not when this module is imported.
    max_users_per_pass: int = field(
        default_factory=lambda: settings.structured_profile_max_users_per_pass
    )
    memory_limit: int = field(default_factory=lambda: settings.structured_profile_memory_limit)
    cooldown_seconds: int = field(
        default_factory=lambda: settings.structured_profile_cooldown_seconds
    )
    label: str = field(default_factory=lambda: settings.structured_profile_label)
    model: str = field(default_factory=lambda: settings.structured_profile_model)


@dataclass
class StructuredProfileResult:
    namespaces_scanned: int = 0
    profiles_written: int = 0
    values_dropped: int = 0
    errors: int = 0


def system_prompt(props: list[profile_schema.ProfileProperty]) -> str:
    """The prompt naming each property, its type and what it holds."""
    lines = []
    for p in props:
        kind = f"array of {p.item_type}" if p.type == "array" else p.type
        if p.enum:
            kind += " (one of " + ", ".join(json.dumps(v) for v in p.enum) + ")"
        lines.append(f'- "{p.name}" ({kind}): {p.description}')
    return _SYSTEM + "\n".join(lines)


def _text(value: Any) -> str:
    """A stored value (canonical JSON) as the text of its claim."""
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return str(value)
    return decoded if isinstance(decoded, str) else json.dumps(decoded, ensure_ascii=False)


def render_facts(rows: list[dict[str, Any]]) -> str:
    """One dated line per fact, oldest first, so the newest statement is read last."""
    from server.memory.workers.profile_consolidation import render_fold_input

    return render_fold_input([{**r, "value": _text(r.get("value"))} for r in rows])


def parse_answer(text: str) -> Any:
    """The model's JSON object; None when the answer is not one."""
    from server.llm.json_text import strip_code_fence

    try:
        return json.loads(strip_code_fence(text or ""))
    except ValueError:
        return None


async def run_pass(cfg: StructuredProfileConfig | None = None) -> StructuredProfileResult:
    cfg = cfg or StructuredProfileConfig()
    result = StructuredProfileResult()
    with record_pass("structured_profile") as rec:
        off = sorted(await customer.switched_off_orgs())
        schemas = await profile_schemas.list_active(exclude_orgs=off, limit=_MAX_SCHEMAS)
        budget = max(1, int(cfg.max_users_per_pass))
        for schema in schemas:
            if budget <= 0:
                break
            pass_context.check_stop()
            result.namespaces_scanned += 1
            ns = str(schema["namespace"])
            pass_context.set_namespace(ns)
            users = await profile_sources.users_behind(
                org=str(schema["org"]),
                namespace=ns,
                label=cfg.label,
                schema_sha256=str(schema["schema_sha256"]),
                cooldown_seconds=int(cfg.cooldown_seconds),
                limit=budget,
            )
            for user_id in users:
                budget -= 1
                try:
                    result.values_dropped += await build_profile(schema, user_id, cfg)
                    result.profiles_written += 1
                except customer.HOLDS:
                    continue  # a held call is no failure: the user is built once the org is back
                except Exception as e:  # one user's build never stops the pass
                    result.errors += 1
                    log.exception("structured profile failed for %s/%s", ns, user_id)
                    pass_context.note_failure(e, ns)
        if not result.profiles_written and not result.errors:
            pass_context.mark_idle()
        rec.outcome = {
            "namespaces_scanned": result.namespaces_scanned,
            "profiles_written": result.profiles_written,
            "values_dropped": result.values_dropped,
            "errors": result.errors,
        }
    return result


async def build_profile(schema: dict[str, Any], user_id: str, cfg: StructuredProfileConfig) -> int:
    """Build and write one user's profile; returns how many of the model's
    values broke the schema and were stored as null."""
    from server.memory.app.profile_schema import write_structured_profile

    org, ns = str(schema["org"]), str(schema["namespace"])
    props = profile_schema.parse(json.loads(schema["schema_json"]))
    # The digest first: a fact written while the model runs leaves the profile
    # behind it, and the next pass rebuilds it.
    digest = await profile_sources.sources_digest(org=org, namespace=ns, user_id=user_id)
    rows = await profile_sources.user_facts(
        org=org,
        namespace=ns,
        user_id=user_id,
        types=profile_sources.STRUCTURED_TYPES,
        limit=int(cfg.memory_limit),
    )
    # An unlabelled row holding a credential is never sent to the model either.
    rows = [r for r in rows if not value_reads_high(r.get("value"))]
    facts = render_facts(rows)
    answer: Any = {}
    with customer.for_org(org), pipeline_actor(WORKER_ACTOR):
        if facts:
            answer = await _ask(org, ns, props, facts, cfg)
        profile, dropped = profile_schema.conform(props, answer)
        await write_structured_profile(
            org=org,
            namespace=ns,
            user_id=user_id,
            value=profile,
            schema_sha256=str(schema["schema_sha256"]),
            sources_digest=digest,
            memory_ids=[str(r["memory_id"]) for r in rows],
        )
    return dropped


async def _ask(
    org: str,
    ns: str,
    props: list[profile_schema.ProfileProperty],
    facts: str,
    cfg: StructuredProfileConfig,
) -> Any:
    from server.llm.client import get_llm_client, get_llm_client_for_model

    started = time.monotonic()
    # Bind an accumulator so this worker's spend is visible.
    cost_accumulator.begin_op()
    try:
        with llm_stage("structured_profile"):
            client = get_llm_client_for_model(cfg.model) if cfg.model else get_llm_client()
            out = await client.complete(
                system=system_prompt(props),
                # The facts are what agents wrote: data, never instructions.
                user=fence("facts", facts, new_nonce(facts)),
                max_tokens=2048,
                response_format={"type": "json_object"},
            )
    finally:
        emit_worker_cost_event_for(
            org=org, namespace=ns, operation="worker.structured_profile", started=started
        )
    answer = parse_answer(getattr(out, "text", "") or "")
    if not isinstance(answer, dict):
        raise ValueError("the profile model's answer is not a JSON object")
    return answer
