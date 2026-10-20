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

A pass reaches namespaces in (org, namespace) order from where the last one
stopped (the pass cursor), and each gets an equal share of the pass's
budget. A user whose build failed is skipped for a while, longer after each
failure; one whose profile row refuses the write (locked, pinned, over a
limit, blacklisted) is skipped until their memories change.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from server.core.errors import LoomServerError
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.llm import client as llm_client
from server.llm import customer
from server.llm.json_text import strip_code_fence
from server.memory.app import profile_schema as schema_app
from server.memory.domain import lock, profile_schema
from server.memory.domain.profile_facts import fact_text, render_fold_input
from server.memory.domain.prompt_fence import FENCE_NOTE, fence, new_nonce
from server.memory.domain.provenance import pipeline_actor
from server.memory.domain.sensitivity import value_reads_high
from server.memory.store import profile_schemas, profile_sources
from server.memory.store.profile_schemas import ActiveSchema
from server.observability import cost_accumulator
from server.observability.genai import llm_stage
from server.settings import settings
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# The actor its writes carry: no request is in flight.
WORKER_ACTOR = "process:structured-profile-worker"
# A failed build waits this long before the user is tried again, doubling up to the cap.
BACKOFF_BASE_SECONDS = 300.0
BACKOFF_CAP_SECONDS = 6 * 3600.0

# A refused user is tried again after this long even when nothing changed, so a
# lock whose `lock_until` ran out without a write does not hold the profile back.
REFUSAL_RETRY_SECONDS = 6 * 3600.0

# Both kept in this process only, so they start over when the worker lease moves.
# (org, namespace, user_id) -> (monotonic time it may be retried, its last wait).
_backoff: dict[tuple[str, str, str], tuple[float, float]] = {}
# (org, namespace, user_id) -> (digest, profile row version, monotonic time it lapses):
# what a refused build saw.
_refused: dict[tuple[str, str, str], tuple[str, int, float]] = {}

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


class Refused(Exception):
    """The user's profile row will not take this build: a lock, a pin or a 4xx refusal.
    `digest` and `profile_version` are what the build saw; the user waits until
    either moves on."""

    def __init__(self, digest: str, profile_version: int) -> None:
        super().__init__(f"refused at digest {digest!r}, profile version {profile_version}")
        self.digest = digest
        self.profile_version = profile_version


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
    # (org, namespace): build only there (a test's own namespace); None reaches every one.
    only: tuple[str, str] | None = None


@dataclass
class StructuredProfileResult:
    namespaces_scanned: int = 0
    profiles_written: int = 0
    # Builds dropped because the user's facts were erased while the model ran.
    skipped: int = 0
    # Builds the profile row refused; the user waits for their memories to change.
    refused: int = 0
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


def render_facts(rows: list[dict[str, Any]]) -> str:
    """One dated line per fact, oldest first, so the newest statement is read last."""
    return render_fold_input([{**r, "value": fact_text(r.get("value"))} for r in rows])


def parse_answer(text: str) -> Any:
    """The model's JSON object; None when the answer is not one."""
    try:
        return json.loads(strip_code_fence(text or ""))
    except ValueError:
        return None


def _note_failure(key: tuple[str, str, str]) -> None:
    _, wait = _backoff.get(key, (0.0, 0.0))
    wait = min(BACKOFF_CAP_SECONDS, wait * 2 if wait else BACKOFF_BASE_SECONDS)
    _backoff[key] = (time.monotonic() + wait, wait)


def _prune() -> None:
    """Drop the waits that are over (a backoff past its cap, a refusal that lapsed),
    so the maps hold only users still waiting."""
    now = time.monotonic()
    for key, (until, _) in list(_backoff.items()):
        if until + BACKOFF_CAP_SECONDS < now:
            del _backoff[key]
    for key, (_, _, lapses) in list(_refused.items()):
        if lapses <= now:
            del _refused[key]


def backing_off(org: str, namespace: str) -> tuple[str, ...]:
    """Users of the namespace whose last build failed and who are still waiting."""
    now = time.monotonic()
    waiting = [
        u
        for (o, n, u), (until, _) in _backoff.items()
        if (o, n) == (org, namespace) and until > now
    ]
    return tuple(sorted(waiting))


def refused_at(org: str, namespace: str) -> dict[str, tuple[str, int]]:
    """User -> the (digest, profile version) their refused build saw, while it holds."""
    now = time.monotonic()
    return {
        u: (digest, version)
        for (o, n, u), (digest, version, lapses) in _refused.items()
        if (o, n) == (org, namespace) and lapses > now
    }


def _cursor() -> tuple[str, str] | None:
    """The (org, namespace) the last pass stopped at; None for the first pass."""
    try:
        cursor = json.loads(pass_context.resume_cursor() or "[]")
    except ValueError:
        return None
    return (str(cursor[0]), str(cursor[1])) if len(cursor) == 2 else None


async def _build_users(
    schema: ActiveSchema,
    users: list[str],
    cfg: StructuredProfileConfig,
    result: StructuredProfileResult,
) -> None:
    for user_id in users:
        pass_context.check_stop()
        key = (schema.org, schema.namespace, user_id)
        try:
            dropped = await build_profile(schema, user_id, cfg)
        except customer.HOLDS:
            continue  # a held call is no failure: the user is built once the org is back
        except Refused as e:
            result.refused += 1
            _refused[key] = (e.digest, e.profile_version, time.monotonic() + REFUSAL_RETRY_SECONDS)
            continue
        except Exception as e:  # one user's build never stops the pass
            result.errors += 1
            _note_failure(key)
            log.exception("structured profile failed for %s/%s", schema.namespace, user_id)
            pass_context.note_failure(e, schema.namespace)
            continue
        _backoff.pop(key, None)
        _refused.pop(key, None)
        if dropped is None:
            result.skipped += 1
        else:
            result.profiles_written += 1
            result.values_dropped += dropped


async def _scan(
    schema: ActiveSchema,
    cfg: StructuredProfileConfig,
    result: StructuredProfileResult,
    limit: int,
    tried: set[str],
) -> list[str] | None:
    """Up to `limit` users of the namespace to build, none of `tried`; None (counted
    and noted) when the scan fails, so one namespace never stops the pass."""
    try:
        behind = await profile_sources.users_behind(
            org=schema.org,
            namespace=schema.namespace,
            label=cfg.label,
            schema_sha256=schema.sha256,
            cooldown_seconds=cfg.cooldown_seconds,
            limit=limit,
            skip=tuple(sorted(set(backing_off(schema.org, schema.namespace)) | tried)),
            refused=refused_at(schema.org, schema.namespace),
        )
    except Exception as e:  # one namespace's failure never stops the pass
        result.errors += 1
        log.exception("structured profile scan failed for %s/%s", schema.org, schema.namespace)
        pass_context.note_failure(e, schema.namespace)
        return None
    return [b.user_id for b in behind]


async def run_pass(cfg: StructuredProfileConfig | None = None) -> StructuredProfileResult:
    cfg = cfg or StructuredProfileConfig()
    result = StructuredProfileResult()
    with record_pass("structured_profile") as rec:
        _prune()
        off = sorted(await customer.switched_off_orgs())
        budget = max(1, cfg.max_users_per_pass)
        # Every namespace reached can build at least once, so a pass reaches at most `budget`.
        spaces = await profile_schemas.list_active(
            exclude_orgs=off, after=_cursor(), limit=budget, only=cfg.only
        )
        share = math.ceil(budget / len(spaces)) if spaces else 0
        tried: dict[tuple[str, str], set[str]] = {}
        # Namespaces that used their whole share: the budget others left goes to them.
        hungry: list[ActiveSchema] = []
        for schema in spaces:
            if budget <= 0:
                break
            pass_context.check_stop()
            result.namespaces_scanned += 1
            pass_context.set_namespace(schema.namespace)
            # The cursor moves on before the namespace's work, so one that keeps failing
            # is tried again when the rotation comes back to it, never on every pass.
            pass_context.set_cursor(json.dumps([schema.org, schema.namespace]))
            limit = min(share, budget)
            users = await _scan(schema, cfg, result, limit, set())
            if users is None:
                continue
            budget -= len(users)
            tried[(schema.org, schema.namespace)] = set(users)
            if len(users) == limit:
                hungry.append(schema)
            await _build_users(schema, users, cfg, result)
        for schema in hungry:
            if budget <= 0:
                break
            pass_context.check_stop()
            users = await _scan(schema, cfg, result, budget, tried[(schema.org, schema.namespace)])
            if users is None:
                continue
            budget -= len(users)
            await _build_users(schema, users, cfg, result)
        if not result.profiles_written and not result.errors:
            pass_context.mark_idle()
        rec.outcome = dataclasses.asdict(result)
    return result


def _refuses_a_write(row: dict[str, Any] | None) -> bool:
    """Whether the live profile row refuses a changed value: pinned, or locked for it."""
    if row is None:
        return False
    mode = lock.effective_mode(SimpleNamespace(**row))
    return bool(row.get("pinned")) or "value" in lock.REFUSES.get(mode, frozenset())


async def build_profile(
    schema: ActiveSchema, user_id: str, cfg: StructuredProfileConfig
) -> int | None:
    """Build and write one user's profile; returns how many of the model's values
    broke the schema and were stored as null, or None when a fact the build read
    stopped being a current fact while the model ran and nothing was written.
    Raises `Refused` when the profile row will not take the write."""
    org, ns = schema.org, schema.namespace
    # The digest and its time first: a fact written while the model runs leaves the
    # profile behind it, and the next pass rebuilds it.
    read_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    digest, count = await profile_sources.sources_digest(
        org=org, namespace=ns, user_id=user_id, scope=WHOLE_NAMESPACE
    )
    existing = await profile_sources.profile_row(
        org=org, namespace=ns, user_id=user_id, label=cfg.label, scope=WHOLE_NAMESPACE
    )
    profile_version = int(existing["version"]) if existing else 0
    if _refuses_a_write(existing):
        raise Refused(digest, profile_version)  # checked before paying for the model call
    rows = await profile_sources.user_facts(
        org=org,
        namespace=ns,
        user_id=user_id,
        types=profile_sources.STRUCTURED_TYPES,
        limit=cfg.memory_limit,
        scope=WHOLE_NAMESPACE,
    )
    # An unlabelled row holding a credential is never sent to the model either.
    rows = [r for r in rows if not value_reads_high(r.get("value"))]
    used = [r["memory_id"] for r in rows]
    facts = render_facts(rows)
    answer: Any = {}
    with customer.for_org(org), pipeline_actor(WORKER_ACTOR):
        if facts:
            answer = await _ask(org, ns, schema.props, facts, cfg)
        # A fact the model read that is no longer current (erased or deleted while it
        # ran) stops the write: this narrows the window in which a profile could land
        # above an erasure, and the erasure saga's census fails closed on one that does.
        if used:
            live = await profile_sources.live_ids(
                org=org, namespace=ns, user_id=user_id, memory_ids=used, scope=WHOLE_NAMESPACE
            )
            if len(live) < len(used):
                return None
        profile, dropped = profile_schema.conform(schema.props, answer)
        try:
            await schema_app.write_structured_profile(
                org=org,
                namespace=ns,
                user_id=user_id,
                label=cfg.label,
                value=profile,
                schema_sha256=schema.sha256,
                sources_digest=digest,
                source_count=count,
                sources_read_at=read_at,
                memory_ids=used,
            )
        except (HTTPException, LoomServerError) as e:
            # A 4xx (a lock or pin set since the check, the char limit, the blacklist)
            # refuses the same build again; anything else is a failure to retry.
            if 400 <= getattr(e, "status_code", 500) < 500:
                raise Refused(digest, profile_version) from e
            raise
    return dropped


async def _ask(
    org: str,
    ns: str,
    props: list[profile_schema.ProfileProperty],
    facts: str,
    cfg: StructuredProfileConfig,
) -> Any:
    started = time.monotonic()
    # Bind an accumulator so this worker's spend is visible.
    cost_accumulator.begin_op()
    try:
        with llm_stage("structured_profile"):
            client = (
                llm_client.get_llm_client_for_model(cfg.model)
                if cfg.model
                else llm_client.get_llm_client()
            )
            out = await client.complete(
                system=system_prompt(props),
                # The facts are what agents wrote: data, never instructions.
                user=fence("facts", facts, new_nonce(facts)),
                # 64 short properties fit; an answer cut off here fails to parse and backs off.
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
