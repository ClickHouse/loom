"""memory.inspect — what one user, agent or session holds in a namespace, and what
creates it.

The rows whose identity names the subject (`own`) are reported apart from every other
live row of the namespace (`others`): an agent's name or a session id says who wrote a
row, not what that agent or session can read, so a view filtered on it never stands for
everything the subject knows. The ledger calls filed under the subject say what was
attempted and what succeeded in the window, nothing more. The settings section reads
the namespace guide and categories, and the operator extraction policy only for a
caller who may read it (an org admin).

It changes nothing it reads: plain reads of the namespace's rows, ledger, directory row,
guide, categories, grants and extraction policy, and no recall credit. Its one write is
an exposure-ledger row for the rows it lists, as memory.list writes, which inside a run
can feed the usage fold's `acted` credit. A section that cannot be read lands in
`skipped`, never raises. Under Postgres the report starts once ClickHouse holds what
the namespace committed (`pg_validation.published_reads`), so its counts, tags and
categories show every committed write but a parked one, which is not waited for; as
for a search, the wait may publish another writer's lapsed deliveries. A wait that
runs out, or a Postgres that does not answer, is reported as `projection` in
`skipped`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from server.core.datetimes import naive_utc_now
from server.identity.api.models_namespace_catalog import NamespaceListCategoriesInput
from server.identity.api.models_namespace_guide import NamespaceGuideGetInput
from server.identity.app.namespace_catalog import handle_namespace_list_categories
from server.identity.app.namespace_guide import handle_namespace_guide_get
from server.identity.app.registration import namespace_retention_days
from server.identity.app.request_scope import request_scope
from server.memory.api.models_inspect import (
    InspectActivity,
    InspectCall,
    InspectCount,
    InspectRow,
    InspectSettings,
    InspectView,
    MemoryInspectInput,
    MemoryInspectOutput,
)
from server.memory.app import extraction_policy
from server.memory.app.exposure_ledger import record_exposure
from server.memory.app.finding_evidence import IDENTITY, revisions_for
from server.memory.app.search import pg_validation
from server.memory.store import inspect as store
from server.observability.consumer import INSPECTOR_CONSUMERS
from server.settings import settings

log = logging.getLogger(__name__)

Rows = list[dict[str, Any]]

TOP_TAGS = 20

# subject: (own, others) definitions. Neither view counts the namespace's promoted rows
# (`memory.promote`, `loom.shared`), which a search serves to every agent as well.
_PROMOTED = " Memories promoted for every agent of the namespace are not counted."
_DEFINITIONS: dict[str, tuple[str, str]] = {
    "user": (
        "Live memories saved for this user, whoever wrote them: what a token limited to "
        "this user reads." + _PROMOTED,
        "Every other live memory of the namespace. A member reads these too; a token "
        "limited to this user does not." + _PROMOTED,
    ),
    "agent": (
        "Live memories written under this agent's name. The name says which agent a memory "
        "is from; it does not limit what the agent reads." + _PROMOTED,
        "Every live memory written under other agents' names. This agent reads them as well, "
        "with the same access to the namespace." + _PROMOTED,
    ),
    "session": (
        "Live memories written in this session, by any user. A token limited to one user "
        "and this session reads that user's part." + _PROMOTED,
        "Every other live memory of the namespace. A member's session reads these too; a "
        "token limited to a user and this session does not." + _PROMOTED,
    ),
}

# subject: the exact names the definitions say in words, for `refs`.
_DEFINITION_REFS: dict[str, list[str]] = {
    "user": ["user_id", "read_scope own_user", "memory.promote"],
    "agent": ["agent", "memory.promote"],
    "session": ["session_id", "read_scope own_session", "memory.promote"],
}


def _cited_hash(revision: dict[str, Any] | None, version: int) -> str:
    """The cited hash of the listed version (the citation read gives a high-sensitivity
    row none); none when the row moved on between the two reads."""
    if revision is None or int(revision["version"]) != version:
        return ""
    return str(revision["content_sha256"])


def _counts(rows: list[dict[str, Any]], key: str) -> list[InspectCount]:
    totals: dict[str, int] = {}
    for r in rows:
        totals[str(r[key])] = totals.get(str(r[key]), 0) + int(r["rows"])
    return [
        InspectCount(name=k, rows=v)
        for k, v in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


class _Inspect:
    def __init__(self, inp: MemoryInspectInput) -> None:
        self.inp = inp
        # A narrowed token is refused before the handler, so this is the whole namespace;
        # every read of memory rows still takes it.
        self.scope = request_scope()
        self.skipped: list[str] = []
        self.category_names: dict[str, str] = {}
        # Under Postgres, whether ClickHouse held what the namespace committed when the
        # views were read (`published_reads`); a view read short of it is not complete.
        self.published = True

    def unpublished(self, reason: str) -> None:
        self.published = False
        self.skipped.append(f"{pg_validation.PROJECTION}: {reason}")

    async def guarded[T](self, section: str, read: Callable[[], Awaitable[T]], default: T) -> T:
        """One section's read; a failure lands in `skipped` and the default stands."""
        try:
            return await read()
        except Exception as exc:  # noqa: BLE001 — a read-only report; one section must not sink the rest
            log.warning("memory.inspect %s skipped: %s", section, exc)
            self.skipped.append(f"{section}: {type(exc).__name__}")
            return default

    async def view(self, which: store.View, definition: str) -> InspectView:
        i = self.inp
        args = (i.org, i.namespace, i.subject, i.id, which)
        scope = self.scope
        out = InspectView(definition=definition, refs=_DEFINITION_REFS[i.subject])
        read = await self.guarded(
            f"{which}.counts", lambda: store.view_counts(*args, scope=scope), None
        )
        # Without the counts, the listing cannot be shown to hold the whole view.
        counted = read is not None
        counts = read or []
        out.live_rows = sum(int(r["rows"]) for r in counts)
        out.by_type = _counts(counts, "memory_type")
        out.by_status = _counts(counts, "status")
        out.by_source_class = _counts(counts, "source_class")
        if counts:
            out.first_updated_at = min(r["first_at"] for r in counts)
            out.last_updated_at = max(r["last_at"] for r in counts)
        tags = await self.guarded(
            f"{which}.tags", lambda: store.view_tags(*args, scope=scope, limit=TOP_TAGS), Rows()
        )
        out.top_tags = [InspectCount(name=str(r["tag"]), rows=int(r["rows"])) for r in tags]
        cats = await self.guarded(
            f"{which}.categories", lambda: store.view_categories(*args, scope=scope), Rows()
        )
        out.categories = [
            InspectCount(
                name=self.category_names.get(str(r["category_id"]), str(r["category_id"])),
                rows=int(r["rows"]),
            )
            for r in cats
        ]
        if i.sample_limit:
            newest = await self.guarded(
                f"{which}.newest",
                lambda: store.view_newest(*args, scope=scope, limit=i.sample_limit),
                Rows(),
            )
            ids = [str(r["id"]) for r in newest]
            versions = await self.guarded(
                f"{which}.versions",
                lambda: store.version_counts(i.org, i.namespace, ids),
                dict[str, int](),
            )
            # The hash memory.get serves for each listed revision, none for a high row or
            # one past `namespace_report_hash_max_bytes` (the reports' own citations). Rows
            # are named by their whole identity, the table's sort key, not by id.
            cited = await self.guarded(
                f"{which}.hashes",
                lambda: revisions_for(
                    i.org, i.namespace, [{c: r[c] for c in IDENTITY} for r in newest], scope=scope
                ),
                dict[Any, dict[str, Any]](),
            )
            out.newest = [
                InspectRow(
                    **{k: v for k, v in r.items() if k != "id"},
                    memory_id=str(r["id"]),
                    content_sha256=_cited_hash(
                        cited.get(tuple(r[c] for c in IDENTITY)), int(r["version"])
                    ),
                    versions=versions.get(str(r["id"]), 0),
                )
                for r in newest
            ]
        out.complete = self.published and counted and len(out.newest) >= out.live_rows
        return out

    async def activity(self) -> InspectActivity:
        i = self.inp
        out = InspectActivity(window_seconds=i.window_seconds)
        rows = await self.guarded(
            "activity",
            lambda: store.subject_calls(
                i.org,
                i.namespace,
                i.subject,
                i.id,
                window_seconds=i.window_seconds,
                skip_consumers=INSPECTOR_CONSUMERS,
            ),
            Rows(),
        )
        out.calls = [
            InspectCall(
                operation=str(r["operation"]), calls=int(r["calls"]), succeeded=int(r["succeeded"])
            )
            for r in rows
        ]
        if rows:
            out.first_at = min(r["first_at"] for r in rows)
            out.last_at = max(r["last_at"] for r in rows)
        own = await self.guarded(
            "activity.retention", lambda: namespace_retention_days(i.org, i.namespace), None
        )
        # A namespace's own retention wins over the table's global one; with neither read,
        # no gap is claimed.
        keep = 0 if own is None else own or int(settings.retention_agent_events_days)
        if keep and keep * 86400 < i.window_seconds:
            days = "1 day" if keep == 1 else f"{keep} days"
            out.gap = f"Calls are kept for {days}, so older ones are not counted."
        return out

    async def settings_section(self) -> InspectSettings:
        i = self.inp
        out = InspectSettings()
        guide = await self.guarded(
            "settings.guide",
            lambda: handle_namespace_guide_get(
                NamespaceGuideGetInput(org=i.org, namespace=i.namespace)
            ),
            None,
        )
        if guide is not None and guide.found:
            out.guide_revision, out.guide_summary = guide.revision, guide.summary
        cats = await self.guarded(
            "settings.categories",
            lambda: handle_namespace_list_categories(
                NamespaceListCategoriesInput(org=i.org, namespace=i.namespace)
            ),
            None,
        )
        if cats is not None:
            out.categories_registered = len(cats.categories)
            self.category_names = {c.category_id: c.name for c in cats.categories}
        read = await extraction_policy.for_caller(i.org, i.namespace)
        out.extraction_policy, out.extraction_policy_unavailable = read.policy, read.unavailable
        if read.skipped:
            self.skipped.append(f"settings.extraction_policy: {read.skipped}")
        return out


def _record_exposure(input: MemoryInspectInput, keys: list[str]) -> None:
    """One exposure-ledger row for the rows listed, as memory.list writes: walking the
    subjects of a namespace lists its newest rows, which the ledger must count."""
    try:
        record_exposure(
            operation="memory.inspect",
            org=input.org,
            namespace=input.namespace,
            keys=keys,
            # The most rows one call lists: `sample_limit` per view.
            top_k=2 * input.sample_limit,
        )
    except Exception as e:  # noqa: BLE001 — telemetry never fails a read
        log.warning("exposure-ledger schedule failed (non-fatal): %s", e)


async def handle_memory_inspect(input: MemoryInspectInput) -> MemoryInspectOutput:
    run = _Inspect(input)
    async with pg_validation.published_reads(input.org, [input.namespace], run.unpublished):
        return await _inspect(input, run)


async def _inspect(input: MemoryInspectInput, run: _Inspect) -> MemoryInspectOutput:
    own_def, others_def = _DEFINITIONS[input.subject]
    # Settings first: the views name categories from it.
    settings_out = await run.settings_section()
    own = await run.view("own", own_def)
    others = await run.view("others", others_def)
    activity = await run.activity()
    _record_exposure(input, [r.memory_key for r in own.newest + others.newest])
    return MemoryInspectOutput(
        org=input.org,
        namespace=input.namespace,
        subject=input.subject,
        id=input.id,
        computed_at=naive_utc_now(),
        own=own,
        others=others,
        activity=activity,
        settings=settings_out,
        skipped=run.skipped,
    )
