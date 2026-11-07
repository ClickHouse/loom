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
gets an all-null profile without a model call, and so does a user with a
profile served but none of their facts served yet.

A pass reaches namespaces in (org, namespace) order from where the last one
stopped (the pass cursor), and each gets an equal share of the pass's budget.
A namespace with nothing to build spends none of it, so the pass reads on
until its budget is spent or it has reached every namespace. The cursor moves
past a namespace before its work, so one that keeps failing is tried again when
the rotation comes back to it, never on every pass. The worker reads idle only
after a pass that came round the rotation and took no user: a user taken but
not written may leave others behind in the scan list. Past half of
`structured_profile_max_pass_seconds` it takes no more users, which leaves the
build in flight the second half to finish. A user whose build failed is
skipped for a while, longer after each failure, and so is a namespace whose
scan or stored schema failed, until its wait is over or its schema changes. A
user whose profile row refuses the write (locked, pinned, labelled `high`,
blacklisted), or whose answer the model cut off at the output cap, is skipped
until their sources digest, the profile row's version (an unpin, an unlock) or
the schema changes, or `structured_profile_refusal_retry_seconds` pass.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException

from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import LoomServerError, ValidationError
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.llm import client as llm_client
from server.llm import customer
from server.llm.json_text import strip_code_fence
from server.memory.app import profile_schema as schema_app
from server.memory.app.model_attribution import newest_subject
from server.memory.domain import lock, profile_schema
from server.memory.domain.profile_facts import fact_text, render_fold_input
from server.memory.domain.prompt_fence import FENCE_NOTE, fence, new_nonce
from server.memory.domain.provenance import pipeline_actor
from server.memory.domain.sensitivity import is_high, value_reads_high
from server.memory.store import profile_schemas, profile_sources
from server.memory.store.profile_sources import Behind
from server.observability import cost_accumulator
from server.observability.genai import llm_stage
from server.settings import settings
from server.settings.budgets import llm_max_tokens
from server.workers import pass_context
from server.workers.cost_event import emit_worker_cost_event_for
from server.workers.holds import hold_seconds
from server.workers.status import record_pass

log = logging.getLogger(__name__)

# One scan lists up to SCAN_FACTOR times the namespace's share; later passes take from
# it until it drains or is `structured_profile_scan_max_age_seconds` old, whichever
# comes first.
SCAN_FACTOR = 10  # not a setting: the scan's shape, sized with the scan-cost measurement
# The stop reasons of a reply cut off at max_tokens, as the providers name them.
_CUT_OFF = ("length", "max_tokens")  # not a setting: provider wire values


@dataclass(frozen=True)
class _Wait:
    """A failed or held build, or a failed scan, waiting to be retried."""

    # Monotonic time it may be retried.
    until: float
    # Its last wait, doubled by the next failure.
    wait: float
    # The schema hash that failed; a namespace's wait ends with a new hash.
    schema_sha256: str = ""


@dataclass(frozen=True)
class _Refusal:
    """What a refused build saw, held until it lapses."""

    schema_sha256: str
    digest: str
    profile_version: int
    # Monotonic time it lapses.
    lapses: float


# Kept in this process only, so each starts over when the worker lease moves.
# (org, namespace, user_id) -> the user's wait after a failed or held build.
_backoff: dict[tuple[str, str, str], _Wait] = {}
# (org, namespace) -> the wait of a namespace whose scan or schema failed.
_namespace_backoff: dict[tuple[str, str], _Wait] = {}
# (org, namespace, user_id) -> what the user's refused build saw.
_refused: dict[tuple[str, str, str], _Refusal] = {}


@dataclass
class _Scanned:
    """A namespace's users behind as one scan found them, taken from by later passes."""

    schema_sha256: str
    scanned_at: float
    behind: deque[Behind]
    # The scan found fewer users than it asked for: every user behind is in the list.
    complete: bool


# (org, namespace) -> the namespace's last scan, in this process only.
_scans: dict[tuple[str, str], _Scanned] = {}


@dataclass(frozen=True)
class ActiveSchema:
    """A namespace's schema as a pass uses it, parsed once."""

    org: str
    namespace: str
    props: list[profile_schema.ProfileProperty]
    sha256: str


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


class CutOff(Exception):
    """The model's answer stopped at the output cap."""


class Refused(Exception):
    """The profile row will not take this build (a lock, a pin, a `high` label or a
    4xx), or the model's answer to it was cut off at the output cap.

    `digest` and `profile_version` (the version of the key's head, redacted or not)
    are what the build saw; the user waits until either or the schema moves on.
    """

    def __init__(self, digest: str, profile_version: int, status: int, why: str = "") -> None:
        # status: the write's HTTP status, 0 for a refusal seen without a write.
        what = why or (f"status {status}" if status else "held: locked, pinned or high")
        super().__init__(f"{what}, at digest {digest!r}, profile version {profile_version}")
        self.digest = digest
        self.profile_version = profile_version
        self.status = status


@dataclass
class StructuredProfileConfig:
    # (org, namespace): build only there (a test's own namespace); None reaches every one.
    only: tuple[str, str] | None = None


@dataclass
class StructuredProfileResult:
    namespaces_reached: int = 0
    profiles_written: int = 0
    # Builds that wrote nothing: no facts and no profile, already current, no profile
    # served with none of the facts counted served, or the sources changed while the
    # model ran.
    skipped: int = 0
    # Builds the profile row refused; the user waits for their memories to change.
    refused: int = 0
    values_dropped: int = 0
    errors: int = 0


def _range(low: Any, high: Any, unit: str = "") -> str | None:
    """A bound pair in words, or None when neither bound is set."""
    if low is not None and high is not None:
        return f"{low} to {high}{unit}"
    if low is not None:
        return f"at least {low}{unit}"
    if high is not None:
        return f"at most {high}{unit}"
    return None


def _limits(constraints: dict[str, Any]) -> list[str]:
    """The constraints `conform` checks, in words, so the model answers in that form."""
    parts = []
    if "pattern" in constraints:
        parts.append(f"pattern {constraints['pattern']}")
    lengths = _range(constraints.get("min_length"), constraints.get("max_length"), " characters")
    values = _range(constraints.get("min_value"), constraints.get("max_value"))
    for part in (lengths, values):
        if part is not None:
            parts.append(part)
    return parts


def system_prompt(props: list[profile_schema.ProfileProperty]) -> str:
    """The prompt naming each property, its type, its constraints and what it holds."""
    lines = []
    for p in props:
        parts = [f"array of {p.item_type}" if p.type == "array" else p.type]
        if p.enum:
            parts.append("one of " + ", ".join(json.dumps(v) for v in p.enum))
        parts += _limits(dict(p.constraints))
        lines.append(f'- "{p.name}" ({", ".join(parts)}): {p.description}')
    return _SYSTEM + "\n".join(lines)


def render_facts(rows: list[dict[str, Any]]) -> str:
    """One dated line per fact, oldest first, so the newest statement is read last."""
    return render_fold_input([{**r, "value": fact_text(r.get("value"))} for r in rows])


def parse_answer(text: str) -> dict[str, Any] | None:
    """The model's JSON object; None when the answer is not one."""
    try:
        answer = json.loads(strip_code_fence(text or ""))
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


def _next_wait(wait: float) -> float:
    first = settings.structured_profile_backoff_base_seconds
    return min(settings.structured_profile_backoff_cap_seconds, wait * 2 if wait else first)


def _note_failure(key: tuple[str, str, str]) -> None:
    last = _backoff.get(key)
    wait = _next_wait(last.wait if last else 0.0)
    _backoff[key] = _Wait(time.monotonic() + wait, wait)


def _note_namespace_failure(org: str, namespace: str, schema_sha256: str) -> None:
    last = _namespace_backoff.get((org, namespace))
    previous = last.wait if last is not None and last.schema_sha256 == schema_sha256 else 0.0
    wait = _next_wait(previous)
    _namespace_backoff[(org, namespace)] = _Wait(time.monotonic() + wait, wait, schema_sha256)


def namespaces_waiting() -> list[tuple[str, str, str]]:
    """(org, namespace, schema hash) of each namespace still backing off."""
    now = time.monotonic()
    return sorted(
        (o, n, w.schema_sha256) for (o, n), w in _namespace_backoff.items() if w.until > now
    )


def _prune() -> None:
    """Drop what is over (a backoff past its cap, a refusal that lapsed, a scan too
    old to use), so the maps hold only what a pass may still read."""
    now = time.monotonic()
    cap = settings.structured_profile_backoff_cap_seconds
    for key, w in list(_backoff.items()):
        if w.until + cap < now:
            del _backoff[key]
    for space, w in list(_namespace_backoff.items()):
        if w.until + cap < now:
            del _namespace_backoff[space]
    for key, refusal in list(_refused.items()):
        if refusal.lapses <= now:
            del _refused[key]
    for space, scanned in list(_scans.items()):
        if now - scanned.scanned_at > settings.structured_profile_scan_max_age_seconds:
            del _scans[space]


def backing_off(org: str, namespace: str) -> tuple[str, ...]:
    """Users of the namespace whose last build failed or was held, still waiting."""
    now = time.monotonic()
    waiting = [
        u for (o, n, u), w in _backoff.items() if (o, n) == (org, namespace) and w.until > now
    ]
    return tuple(sorted(waiting))


def refused_at(org: str, namespace: str, schema_sha256: str) -> dict[str, tuple[str, int]]:
    """User -> the (digest, profile version) their refused build under this schema
    saw, while it holds."""
    now = time.monotonic()
    return {
        u: (refusal.digest, refusal.profile_version)
        for (o, n, u), refusal in _refused.items()
        if (o, n) == (org, namespace)
        and refusal.schema_sha256 == schema_sha256
        and refusal.lapses > now
    }


def _cursor() -> tuple[str, str] | None:
    """The (org, namespace) the last pass stopped at; None for the first pass."""
    try:
        cursor = json.loads(pass_context.resume_cursor() or "[]")
    except ValueError:
        return None
    return (str(cursor[0]), str(cursor[1])) if len(cursor) == 2 else None


def _out_of_time(stop_at: float) -> bool:
    return time.monotonic() >= stop_at


async def _build_users(
    schema: ActiveSchema,
    users: list[Behind],
    result: StructuredProfileResult,
    stop_at: float,
) -> None:
    for i, b in enumerate(users):
        pass_context.check_stop()
        if _out_of_time(stop_at):
            # The users not built go back to the front of the scan list for the next pass.
            scanned = _scans.get((schema.org, schema.namespace))
            if scanned is not None:
                scanned.behind.extendleft(reversed(users[i:]))
            return
        user_id = b.user_id
        key = (schema.org, schema.namespace, user_id)
        try:
            dropped = await build_profile(schema, user_id)
        except customer.GROUP_HOLDS as e:
            # A hold preserves the next failure wait.
            last = _backoff.get(key)
            _backoff[key] = _Wait(time.monotonic() + hold_seconds(e), last.wait if last else 0.0)
            continue
        except customer.HOLDS:
            continue  # a held call is no failure: the user is built once the org is back
        except Refused as e:
            result.refused += 1
            log.info("structured profile refused for %s/%s: %s", schema.namespace, user_id, e)
            lapses = time.monotonic() + settings.structured_profile_refusal_retry_seconds
            _refused[key] = _Refusal(schema.sha256, e.digest, e.profile_version, lapses)
            continue
        except Exception as e:  # one user's build must not stop the pass
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


def _take(schema: ActiveSchema, limit: int) -> list[Behind] | None:
    """Up to `limit` users from the namespace's last scan, leaving out those backing
    off and those still at a refusal; None when the caller should scan again: no
    list, one built under another schema or too old, or an incomplete one drained.
    A complete list that drained answers [] until it is too old."""
    scanned = _scans.get((schema.org, schema.namespace))
    if (
        scanned is None
        or scanned.schema_sha256 != schema.sha256
        or time.monotonic() - scanned.scanned_at > settings.structured_profile_scan_max_age_seconds
        or (not scanned.behind and not scanned.complete)
    ):
        return None
    waiting = set(backing_off(schema.org, schema.namespace))
    refused = refused_at(schema.org, schema.namespace, schema.sha256)
    out: list[Behind] = []
    while scanned.behind and len(out) < limit:
        b = scanned.behind.popleft()
        if b.user_id not in waiting and refused.get(b.user_id) != (b.digest, b.profile_version):
            out.append(b)
    return out


async def _scan(
    schema: ActiveSchema,
    result: StructuredProfileResult,
    limit: int,
) -> list[Behind] | None:
    """Up to `limit` users of the namespace to build, from its last scan while that
    holds, else from a new one that keeps SCAN_FACTOR times as many for the passes
    after; None (counted and noted) when the scan fails, so one namespace never
    stops the pass."""
    users = _take(schema, limit)
    if users is not None:
        return users
    asked = limit * SCAN_FACTOR
    try:
        behind = await profile_sources.users_behind(
            org=schema.org,
            namespace=schema.namespace,
            label=settings.structured_profile_label,
            schema_sha256=schema.sha256,
            cooldown_seconds=settings.structured_profile_cooldown_seconds,
            limit=asked,
            skip=backing_off(schema.org, schema.namespace),
            refused=refused_at(schema.org, schema.namespace, schema.sha256),
            timeout_seconds=settings.structured_profile_scan_timeout_seconds,
            max_memory_bytes=settings.structured_profile_scan_max_memory_bytes,
            scope=WHOLE_NAMESPACE,
        )
    except Exception as e:  # one namespace's scan must not stop the pass
        result.errors += 1
        _note_namespace_failure(schema.org, schema.namespace, schema.sha256)
        log.exception("structured profile scan failed for %s/%s", schema.org, schema.namespace)
        pass_context.note_failure(e, schema.namespace)
        return None
    _namespace_backoff.pop((schema.org, schema.namespace), None)
    _scans[(schema.org, schema.namespace)] = _Scanned(
        schema.sha256, time.monotonic(), deque(behind), complete=len(behind) < asked
    )
    return _take(schema, limit) or []


def _parse(row: dict[str, Any], result: StructuredProfileResult) -> ActiveSchema | None:
    """The stored schema, parsed; None (counted and noted) for one that no longer parses."""
    try:
        props = profile_schema.parse(json.loads(row["schema_json"]))
    except (ValueError, ValidationError) as e:
        result.errors += 1
        _note_namespace_failure(row["org"], row["namespace"], row["schema_sha256"])
        log.warning("profile schema of %s/%s does not parse: %s", row["org"], row["namespace"], e)
        pass_context.note_failure(e, row["namespace"])
        return None
    return ActiveSchema(row["org"], row["namespace"], props, row["schema_sha256"])


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return (row["org"], row["namespace"])


async def run_pass(cfg: StructuredProfileConfig | None = None) -> StructuredProfileResult:
    cfg = cfg or StructuredProfileConfig()
    result = StructuredProfileResult()
    with record_pass("structured_profile") as rec:
        _prune()
        off = sorted(await customer.switched_off_orgs())
        max_users = settings.structured_profile_max_users_per_pass
        budget = max_users
        # The pass takes no user in the second half of its deadline, which leaves the
        # last build that half to finish in.
        stop_at = time.monotonic() + settings.structured_profile_max_pass_seconds / 2
        # A namespace with nothing to build spends no budget, so read on to the next page.
        reached: set[tuple[str, str]] = set()
        # Namespaces that used their whole share: the budget others left goes to them,
        # from the users their scan already found.
        hungry: list[ActiveSchema] = []
        after = _cursor()
        wrapped = False
        while budget > 0 and not wrapped and not _out_of_time(stop_at):
            # Backing-off namespaces are left out in SQL so the limit counts only runnable ones.
            rows = await profile_schemas.list_active(
                exclude_orgs=off,
                after=after,
                limit=budget,
                only=cfg.only,
                waiting=namespaces_waiting(),
            )
            # A short page holds every namespace left; one already reached means the
            # rotation came back round.
            wrapped = len(rows) < budget or any(_key(r) in reached for r in rows)
            rows = [r for r in rows if _key(r) not in reached]
            reached.update(_key(r) for r in rows)
            if rows:
                after = _key(rows[-1])
            spaces = [s for s in (_parse(r, result) for r in rows) if s is not None]
            share = math.ceil(budget / len(spaces)) if spaces else 0
            for schema in spaces:
                if budget <= 0 or _out_of_time(stop_at):
                    break
                pass_context.check_stop()
                result.namespaces_reached += 1
                pass_context.set_namespace(schema.namespace)
                # Move the cursor first so a namespace that keeps failing waits for the rotation.
                pass_context.set_cursor(json.dumps([schema.org, schema.namespace]))
                limit = min(share, budget)
                users = await _scan(schema, result, limit)
                if users is None:
                    continue
                budget -= len(users)
                if len(users) == limit:
                    hungry.append(schema)
                await _build_users(schema, users, result, stop_at)
        for schema in hungry:
            if budget <= 0 or _out_of_time(stop_at):
                break
            pass_context.check_stop()
            users = _take(schema, budget) or []
            budget -= len(users)
            await _build_users(schema, users, result, stop_at)
        # Idle only after a full rotation that took no user.
        took_none = budget == max_users
        if wrapped and took_none and not result.errors and not _out_of_time(stop_at):
            pass_context.mark_idle()
        rec.outcome = dataclasses.asdict(result)
    return result


def _refuses_a_write(row: dict[str, Any] | None) -> bool:
    """Whether the profile key's head holds against a rebuild: pinned, locked for a
    changed value, or labelled `high` (a rebuild would write an unlabelled row that
    profile.render and context.pack serve)."""
    if row is None:
        return False
    if is_high(row.get("sensitivity"), row.get("tags")):
        return True
    mode = lock.effective_mode(SimpleNamespace(**row))
    return bool(row.get("pinned")) or "value" in lock.REFUSES.get(mode, frozenset())


async def build_profile(schema: ActiveSchema, user_id: str) -> int | None:
    """Build and write one user's profile; returns how many of the model's values
    broke the schema and were stored as null, or None when nothing was written: no facts
    and no profile, the profile already current, no profile served with none of the
    facts counted served, or the sources changed while the model ran.
    Raises `Refused` when the profile row will not take the write."""
    org, ns = schema.org, schema.namespace
    # The digest and its time first: the write is checked against this digest once
    # the model has answered.
    read_at = naive_utc_now_seconds()
    digest, count = await profile_sources.sources_digest(
        org=org, namespace=ns, user_id=user_id, scope=WHOLE_NAMESPACE
    )
    existing = await profile_sources.profile_row(
        org=org,
        namespace=ns,
        user_id=user_id,
        label=settings.structured_profile_label,
        scope=WHOLE_NAMESPACE,
    )
    if count == 0 and existing is None:
        # No profile to clear and nothing to build from: a user erased after the scan
        # listed them gets no new row.
        return None
    if existing and profile_sources.is_current(existing["tags"], schema.sha256, digest):
        return None  # built since the scan listed the user (another pass, another replica)
    # A pin, a lock or a `high` label still holds on a head that reads as no profile
    # (redacted, expired or high); checked before paying for the model call.
    hold = existing or await profile_sources.profile_head_hold(
        org=org,
        namespace=ns,
        user_id=user_id,
        label=settings.structured_profile_label,
        scope=WHOLE_NAMESPACE,
    )
    # A refusal is keyed on the head's version: an unpin or an unlock moves it on.
    profile_version = int(hold["version"]) if hold else 0
    if _refuses_a_write(hold):
        raise Refused(digest, profile_version, 0)
    limit = settings.structured_profile_memory_limit
    rows = await profile_sources.user_facts(
        org=org,
        namespace=ns,
        user_id=user_id,
        types=profile_sources.STRUCTURED_TYPES,
        limit=limit,
        scope=WHOLE_NAMESPACE,
    )
    if not rows and existing is None:
        # Nothing served and no profile to replace: the user stays pending. A served profile
        # is never kept here: it may carry a source that has left since.
        return None
    # Short of a counted fact (not served at the version read, or a replica behind the
    # digest's): tagged SHORT, so it reads as behind and rebuilds after the cooldown.
    tagged_digest = digest if len(rows) >= min(count, limit) else profile_sources.SHORT
    # A row whose value reads as high, whatever its label, is never sent to the model either.
    rows = [r for r in rows if not value_reads_high(r.get("value"))]
    used = [r["memory_id"] for r in rows]
    facts = render_facts(rows)
    answer: Any = {}
    with (
        customer.for_org(org, namespace=ns, subject=newest_subject(rows), user_id=user_id),
        pipeline_actor(profile_sources.WORKER_ACTOR),
    ):
        if facts:
            try:
                answer = await _ask(org, ns, schema.props, facts)
            except CutOff as e:
                # The same facts under the same schema are cut off again.
                raise Refused(digest, profile_version, 0, str(e)) from e
        # Sources that moved while the model ran (a fact edited, redacted, erased,
        # deleted or added) stop the write: the model may have read text that is gone,
        # and the next pass rebuilds from what is current. This narrows the window in
        # which a profile could land above an erasure or a redact, and the erasure
        # saga's census fails closed on one that does.
        now_digest, _ = await profile_sources.sources_digest(
            org=org, namespace=ns, user_id=user_id, scope=WHOLE_NAMESPACE
        )
        if now_digest != digest:
            return None
        profile, dropped = profile_schema.conform(schema.props, answer)
        try:
            await schema_app.write_structured_profile(
                org=org,
                namespace=ns,
                user_id=user_id,
                label=settings.structured_profile_label,
                value=profile,
                schema_sha256=schema.sha256,
                sources_digest=tagged_digest,
                source_count=count,
                sources_read_at=read_at,
                memory_ids=used,
            )
        except (HTTPException, LoomServerError) as e:
            # A 4xx from memory.set (a lock or pin set since the check, the
            # blacklist) refuses the same build again; anything else is a failure
            # to retry.
            status = e.status_code
            if 400 <= status < 500:
                raise Refused(digest, profile_version, status) from e
            raise
    return dropped


async def _ask(
    org: str,
    ns: str,
    props: list[profile_schema.ProfileProperty],
    facts: str,
) -> dict[str, Any]:
    started = time.monotonic()
    # Bind an accumulator so this worker's spend is visible.
    cost_accumulator.begin_op()
    try:
        with llm_stage("structured_profile"):
            model = settings.structured_profile_model
            client = (
                llm_client.get_llm_client_for_model(model) if model else llm_client.get_llm_client()
            )
            out = await client.complete(
                system=system_prompt(props),
                # The facts are what agents wrote: data, never instructions.
                user=fence("facts", facts, new_nonce(facts)),
                # Room for a full answer: the schema's estimate of it, and the object.
                max_tokens=llm_max_tokens("structured_profile")
                + profile_schema.answer_tokens(props),
                response_format={"type": "json_object"},
            )
    finally:
        emit_worker_cost_event_for(
            org=org, namespace=ns, operation="worker.structured_profile", started=started
        )
    if out.stop_reason in _CUT_OFF:
        raise CutOff(f"the answer was cut off at {out.tokens_out} tokens")
    answer = parse_answer(out.text)
    if answer is None:
        raise ValueError("the profile model's answer is not a JSON object")
    return answer
