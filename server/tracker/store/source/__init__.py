"""The live tracker source: the tracker record read inside the
request, on behalf of one tracker namespace, with that namespace's credential.

Every reader here is addressed at `(org, namespace)`, the tracker namespace the
transport (or the code-namespace binding) already authorized. The namespace's
configuration (`server/tracker/domain/namespaces.py`) decides the predicate
and the credential (one for every namespace): a standard namespace
`is_private = 0` on every statement; a restricted namespace reads with the
private user and only while its directory row says `restricted` (a row that
is missing or standard would have let a wider gate through, so the source
refuses — fail closed). The rows come back in the shape the tracker facts
and handlers consume.

Cost discipline: one bounded read per call (`LOOM_TRACKER_LIVE_BUDGET_MS`, the
client's `max_execution_time` behind it), a per-credential breaker, and a
process cache with a TTL matched to the upstream refresh (5–10 min), so a
review that asks the same numbers twice pays once. Any failure — breaker
open, credential unset, timeout, upstream error — is `TrackerUnavailable`, which
the ops render as `tracker_unavailable` and the additive facts render as
silence; a loom answer is never a claim about rows nobody read.

Nothing here writes: no loom table receives QA rows.
"""

from __future__ import annotations

from server.tracker.store.source.base import reset_cache_for_tests
from server.tracker.store.source.builds import BuildReads
from server.tracker.store.source.checks import CheckReads
from server.tracker.store.source.code_history import CodeHistoryReads
from server.tracker.store.source.conventions import (
    checks_config,
    checks_source_label,
    code_path_sql,
    code_prefixes_for,
)
from server.tracker.store.source.embed import EmbedReads
from server.tracker.store.source.items import CELL_LIMIT, ItemReads, normalise_severities
from server.tracker.store.source.links import LinkReads
from server.tracker.store.source.prs import PrReads
from server.tracker.store.source.restricted import RestrictedReads
from server.tracker.store.source.rows import issue_row, pr_row
from server.tracker.store.source.similarity import SimilarReads


class TrackerSource(
    BuildReads,
    ItemReads,
    PrReads,
    CodeHistoryReads,
    RestrictedReads,
    SimilarReads,
    EmbedReads,
    LinkReads,
    CheckReads,
):
    """Live readers for one tracker namespace. Construct per request; the cache
    and the clients are process-wide. Each reader family is a `SourceBase`
    subclass in its own module; this class only composes them."""


__all__ = [
    "CELL_LIMIT",
    "TrackerSource",
    "checks_config",
    "checks_source_label",
    "code_path_sql",
    "code_prefixes_for",
    "issue_row",
    "normalise_severities",
    "pr_row",
    "reset_cache_for_tests",
]
