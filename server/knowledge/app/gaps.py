"""Knowledge gaps: recording a search that found nothing, and `knowledge.gaps.list`.

A miss is recorded by the op the caller invoked, never by a search another op runs on
its behalf: a `loom_search` whose knowledge section answers is not a gap because its
memory section abstained. A miss stores the query, so it is recorded only when the
operator opted in (`knowledge_gaps_enabled`) and the request's capture level is not
`off`, the answer the trace log reads for its query column (an unreadable consent reads as
`off`). The asker is the person the token stands for (`_asker`), never a body field, so
tokens a person mints for themselves count once. A user erasure deletes the misses whose
asker or `user_id` is the user.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from server.knowledge.api.models_gaps import (
    KnowledgeGapsListInput,
    KnowledgeGapsListOutput,
    _KnowledgeGap,
)
from server.knowledge.domain.gaps import normalize_query
from server.knowledge.store import gaps as gaps_store
from server.memory.domain.sensitivity import detect_sensitivity, reads_high
from server.observability.genai import current_capture_level, current_operation
from server.settings import settings

log = logging.getLogger(__name__)


@dataclass
class _Recordings:
    """The miss recordings in flight in this process, and the misses dropped past
    `knowledge_gaps_max_in_flight`."""

    in_flight: int = 0
    dropped: int = 0


recordings = _Recordings()

# A longer query records no miss: a gap head is a question, and the normalising and the
# sensitivity check run on the request's loop.
_MAX_QUERY_CHARS = 500  # not a setting: a question's length, not a capacity

# The migration that adds loom.search_misses and loom.knowledge_gaps; an older
# replica's erasure saga has no step for them.
GAPS_MIGRATION = 95  # tests/unit/test_knowledge_gaps.py ties it to the file


def record_miss(
    *,
    op: str,
    org: str,
    namespace: str,
    query: str,
) -> None:
    """Record one miss of `op` in the background, when gaps are on, `op` is the op the
    caller invoked and the token names an asker. Never raises."""
    try:
        if (
            not settings.knowledge_gaps_enabled
            or len(query) > _MAX_QUERY_CHARS
            or not _invoked(op)
            or not every_replica_erases_gaps()
        ):
            return
        if settings.effective_identity_provider() == "single-user":
            # One identity for every caller: a gap, which needs several askers, never forms.
            return
        asker, end_user = _asker()
        if not asker or not _capturing():
            return
        normalized = normalize_query(query)
        if not normalized or _sensitive(query):
            # A query holding a credential or contact details is never kept.
            return
        row = {
            "org": org,
            "namespace": namespace,
            "user_id": end_user or "-",
            "asker": asker,
            "normalized": normalized,
        }
        if recordings.in_flight >= settings.knowledge_gaps_max_in_flight:
            recordings.dropped += 1
            if recordings.dropped % 1000 == 1:
                log.warning(
                    "knowledge gap misses dropped past the in-flight bound: %d", recordings.dropped
                )
            return
        from server.memory.app import after_write

        recordings.in_flight += 1
        task = after_write.schedule(_insert(row, int(time.time())), name="knowledge.gaps.miss")
        if task is None:
            recordings.in_flight -= 1
    except Exception as e:  # noqa: BLE001 — a miss is telemetry; it never fails a search
        log.warning("knowledge gap miss not recorded: %s", e)


def filtered(input: Any, fields: Sequence[str]) -> bool:
    """Whether `input` sets any of `fields` away from its model's default: a filtered
    search asked for part of the namespace, so its empty answer is not a gap."""
    model_fields = type(input).model_fields
    for name in fields:
        default = model_fields[name].get_default(call_default_factory=True)
        value = getattr(input, name)
        if value != default and value not in (None, "", "-"):
            return True
    return False


def reads_whole_namespace() -> bool:
    """Whether this request's token reads the whole namespace: a token bounded to one
    user or session cannot see the namespace's shared rows, so its empty memory read is
    not a gap. A scope that does not resolve reads as bounded."""
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import request_read_narrowed

    return not request_read_narrowed(current_auth())


async def _insert(row: dict[str, Any], started_s: int) -> None:
    """Insert the miss behind the erasure fence: skip it if an erasure reaching it was
    unfinished at `started_s` or has finished since, and delete it if one starts before
    the row lands."""
    try:
        asker = await _person(row["org"], row["asker"])
        if not asker:
            return
        org, namespace = row["org"], row["namespace"]
        reaching = {asker, row["user_id"]} - {"-"}
        if await erasure_since(org, namespace, started_s, reaching):
            return
        miss_id = uuid.uuid4().hex
        await gaps_store.insert_miss({**row, "asker": asker, "miss_id": miss_id})
        # Read again once the row is visible: an erasure that began meanwhile may have run
        # its step before the row landed.
        if await erasure_since(org, namespace, started_s, reaching):
            await gaps_store.delete_miss(org, namespace, miss_id)
    except Exception as e:  # noqa: BLE001 — a miss is telemetry; it never fails a search
        log.warning("knowledge gap miss not recorded: %s", e)
    finally:
        recordings.in_flight -= 1


async def erasure_since(org: str, namespace: str, since_s: int, askers: set[str] | None) -> bool:
    """Whether an erasure that may have deleted what was read was unfinished at `since_s`
    or has finished since. The namespace's erasure, or a list naming it, always counts. A
    user erasure counts when it names one of the askers' spellings, or always when
    `askers` is None: one directory read per asker, lower-cased, however many erasures
    are open. An unreadable ledger or directory counts as one."""
    from server.auth.aliases import spellings
    from server.erasure.app import ledger_reads
    from server.erasure.domain.scopes import may_cover

    try:
        # An abandoned saga stays unfinished for good: counting it would stop every build
        # and recording in the org, while a resumed one is in progress again.
        rows = await ledger_reads.unfinished_reaching(
            org, namespace, None, since_s, ledger_reads.LEDGER_CLOCK_MARGIN_S, skip_abandoned=True
        )
        asker_spellings: set[str] | None = None
        for r in rows:
            value = str(r["scope_value"])
            if r["scope_type"] != "user_id":
                if may_cover(str(r["scope_type"]), value, namespace):
                    return True
                continue
            if askers is None:
                return True
            if asker_spellings is None:
                # Read once, on the first user erasure; the directory matches any case.
                asker_spellings = set()
                for asker in {a.lower() for a in askers}:
                    asker_spellings |= {sp.lower() for sp in await spellings(org, asker)}
            if value.lower() in asker_spellings:
                return True
    except Exception as e:  # noqa: BLE001 — unknown ledger: counted as an erasure, fail closed
        log.warning("knowledge gaps: erasure ledger unreadable for %s: %s", org, e)
        return True
    return False


def _sensitive(query: str) -> bool:
    """Whether `query` reads or grades as `medium` or `high`: a draft built from it would
    show it to viewers, and no promotion would accept it."""
    return reads_high(query) or bool(detect_sensitivity(query))


def _capturing() -> bool:
    """Whether this request may keep query text: its capture level, the answer the trace
    log reads for its query column (narrowed to the most restrictive namespace a fan-out
    reads), is not `off`."""
    return current_capture_level() != "off"


def _invoked(op: str) -> bool:
    """True when the transport stamped `op` on this request: a search a worker or
    another op runs is not a question someone asked."""
    return current_operation() == op


def _asker() -> tuple[str, str]:
    """(asker, end user): the person the token stands for. The end user of an end-user
    token; else the token's `creator` claim, the person at the root of a delegated token
    or service account, so the tokens a person mints for themselves count once; else the
    token's subject. ('', '') without a token."""
    from server.auth.context import current_auth
    from server.core.principals import USER, principal_id

    auth = current_auth()
    if auth is None:
        return "", ""
    end_user = principal_id(auth.subject, USER)
    if end_user:
        return end_user, end_user
    root = auth.creator or auth.subject
    return root or "", ""


async def _person(org: str, asker: str) -> str:
    """The asker folded onto one spelling per person: a service account's creator, and a
    human's spelling as the directory ties it (the login handle for a contact email), so
    one person's two spellings count once and a user erasure finds them. '' when either
    cannot be read."""
    from server.auth.aliases import login_handle

    asker = await _registered_creator(org, asker)
    if not asker:
        return ""
    try:
        folded = await login_handle(org, asker)
        if folded is None:  # tied to two accounts: not one person
            return ""
        return folded
    except Exception as e:  # noqa: BLE001 — an unreadable tie records no miss, never a guess
        log.warning("knowledge gap asker not resolved: %s", type(e).__name__)
        return ""


# Service accounts a creator chain may climb before it is treated as unresolved.
_MAX_CREATOR_HOPS = 8  # not a setting: a bound on a registry walk, not a deployment's capacity


async def _registered_creator(org: str, asker: str) -> str:
    """The person behind a service account, for a token minted without the `creator`
    claim: the first subject in its creator chain that is not a service account, as the
    registry records it; any other asker as given. '' when the chain cannot be read, has
    no person in it, or is longer than `_MAX_CREATOR_HOPS`: an unknown principal is
    never counted as a separate person."""
    from server.auth.service_accounts import sa_creator

    subject = asker
    for _ in range(_MAX_CREATOR_HOPS):
        if not subject.startswith("sa:"):
            return subject
        creator = await sa_creator(org, subject)
        if creator is None or not creator.subject:
            return ""
        if creator.kind == "agent":
            # An agent sign-up: the namespace made its own account, and no person stands
            # behind it.
            return ""
        subject = creator.subject
    return ""


def every_replica_erases_gaps() -> bool:
    """Whether every live replica runs a build whose erasure knows the gap tables: a row
    written while an older one serves could outlive an erasure it runs. Unknown (no
    recent heartbeat read) counts as no."""
    from server.core import instances

    live = instances.live_rows()
    return bool(live) and all(int(r["schema_version"] or 0) >= GAPS_MIGRATION for r in live)


async def handle_knowledge_gaps_list(input: KnowledgeGapsListInput) -> KnowledgeGapsListOutput:
    """The namespace's gaps of its latest finished build, most askers first. Empty while
    gaps are off; 503 while an older replica serves (the rollout gate of
    docs/architecture/knowledge-gaps.md)."""
    from server.core.errors import OverloadedError

    if not settings.knowledge_gaps_enabled:
        # Off stops the list too: stored builds expire by their TTL, never shown meanwhile.
        return KnowledgeGapsListOutput(gaps=[], built_at=None, enabled=False)
    if not every_replica_erases_gaps():
        raise OverloadedError(
            "not every replica serves knowledge gaps yet; retry once the rollout is done",
            retry_after_s=30,
        )
    if not _capturing():
        # Capture is off for this namespace, perhaps since the last pass: show nothing now.
        return KnowledgeGapsListOutput(gaps=[], built_at=None, enabled=True, capturing=False)
    rows = await gaps_store.latest_gaps(input.org, input.namespace, limit=input.limit)
    marker = next((r for r in rows if not r["gap_id"]), None)
    return KnowledgeGapsListOutput(
        gaps=[
            _KnowledgeGap(
                gap_id=str(r["gap_id"]),
                head=str(r["head"]),
                samples=list(r["samples"]),
                askers=int(r["askers"]),
                asks=int(r["asks"]),
                last_at=r["last_at"],
            )
            for r in rows
            if r["gap_id"]
        ],
        built_at=marker["built_at"] if marker else None,
        capturing=True,
        enabled=True,
    )
