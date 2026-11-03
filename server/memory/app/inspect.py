"""memory.inspect — what one user, agent or session holds in a namespace, and what
creates it.

The rows whose identity names the subject (`own`) are reported apart from every other
live row of the namespace (`others`): an agent's name or a session id says who wrote a
row, not what that agent or session can read, so a view filtered on it never stands for
everything the subject knows. The ledger calls filed under the subject say what was
attempted and what succeeded in the window, nothing more. The settings section reads
the namespace guide and categories, and the operator extraction policy only for a
caller who may read it (an org admin).

Read-only: plain reads over `loom.memory`, `loom.memory_categories`,
`loom.memory_versions` and `loom.agent_events`; no recall credit or usage event. A
section that cannot be read lands in `skipped`, never raises.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from server.auth.authz import row_in_scope
from server.identity.api.models_namespace_catalog import NamespaceListCategoriesInput
from server.identity.api.models_namespace_guide import NamespaceGuideGetInput
from server.identity.app.namespace_catalog import handle_namespace_list_categories
from server.identity.app.namespace_guide import handle_namespace_guide_get
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
from server.memory.store import inspect as store
from server.observability.consumer import INSPECTOR_CONSUMERS
from server.operations.base import Role
from server.settings import settings

log = logging.getLogger(__name__)

Rows = list[dict[str, Any]]

TOP_TAGS = 20

# subject: (own, others) definitions. Neither view counts the namespace's promoted rows
# (`memory.promote`, `loom.shared`), which a search serves to every agent as well.
_PROMOTED = " Promoted rows (memory.promote) are not counted."
_DEFINITIONS: dict[str, tuple[str, str]] = {
    "user": (
        "Live rows filed under this user_id, whoever wrote them: what a token pinned to "
        "this user (read_scope own_user) reads." + _PROMOTED,
        "Every other live row of the namespace. A member reads these too; a token pinned "
        "to this user does not." + _PROMOTED,
    ),
    "agent": (
        "Live rows written under this agent name. The name records the writer; it is not a "
        "read boundary." + _PROMOTED,
        "Every live row other agents wrote. This agent reads them as well, under the same "
        "namespace grant." + _PROMOTED,
    ),
    "session": (
        "Live rows written in this session_id, by any user. A token pinned to one user and "
        "this session (read_scope own_session) reads that user's part." + _PROMOTED,
        "Every other live row of the namespace. A member's session reads these too; a "
        "token pinned to a user and this session does not." + _PROMOTED,
    ),
}


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
        self.skipped: list[str] = []
        self.category_names: dict[str, str] = {}

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
        out = InspectView(definition=definition)
        read = await self.guarded(f"{which}.counts", lambda: store.view_counts(*args), None)
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
            f"{which}.tags", lambda: store.view_tags(*args, limit=TOP_TAGS), Rows()
        )
        out.top_tags = [InspectCount(name=str(r["tag"]), rows=int(r["rows"])) for r in tags]
        cats = await self.guarded(
            f"{which}.categories", lambda: store.view_categories(*args), Rows()
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
                f"{which}.newest", lambda: store.view_newest(*args, limit=i.sample_limit), Rows()
            )
            ids = [str(r["id"]) for r in newest]
            versions = await self.guarded(
                f"{which}.versions",
                lambda: store.version_counts(i.org, i.namespace, ids),
                dict[str, int](),
            )
            out.newest = [
                InspectRow(
                    **{k: v for k, v in r.items() if k != "id"},
                    memory_id=str(r["id"]),
                    versions=versions.get(str(r["id"]), 0),
                )
                for r in newest
            ]
        out.complete = counted and len(out.newest) >= out.live_rows
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
        keep = settings.retention_agent_events_days
        if keep and keep * 86400 < i.window_seconds:
            out.gap = f"The ledger keeps {keep} days, so calls older than that are not counted."
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
        if await row_in_scope(i.org, i.namespace, Role.ORG_ADMIN, hides_answer=False):
            out.extraction_policy = await self.guarded(
                "settings.extraction_policy",
                lambda: extraction_policy.effective(i.org, i.namespace),
                None,
            )
            if out.extraction_policy is None:
                out.extraction_policy_unavailable = "the policy could not be read"
        else:
            out.extraction_policy_unavailable = (
                "only an org admin reads the operator extraction policy"
            )
        return out


async def handle_memory_inspect(input: MemoryInspectInput) -> MemoryInspectOutput:
    run = _Inspect(input)
    own_def, others_def = _DEFINITIONS[input.subject]
    # Settings first: the views name categories from it.
    settings_out = await run.settings_section()
    own = await run.view("own", own_def)
    others = await run.view("others", others_def)
    activity = await run.activity()
    return MemoryInspectOutput(
        org=input.org,
        namespace=input.namespace,
        subject=input.subject,
        id=input.id,
        computed_at=datetime.now(UTC).replace(tzinfo=None),
        own=own,
        others=others,
        activity=activity,
        settings=settings_out,
        skipped=run.skipped,
    )
