"""Repository for the loom.code_* table family (code intelligence plane).

ALL code-plane SQL lives in this package — workers, handlers, and services stay
SQL-free. The CH client is injected (default = server.core.ch.get_client())
so unit tests can supply a mock (repository convention).

NO PREWHERE in this plane, deliberately: a multi-condition PREWHERE (org AND
namespace) over multi-part code_edges can return a fraction of the matching rows
(granule pruning across parts with this deep key). Correctness beats the marginal
PREWHERE win, so every read here filters in plain WHERE. A PREWHERE may return only
behind a setting, with tests/integration/test_code_plane_prewhere_parity.py green on
the deployed ClickHouse and a wall-clock win measured.

Layout: `rows` (column registries, row builders), `sql` (shared fragments and
batched reads) and `symbol_rank` (pure row ranking) are leaves; `base` holds
the client and index state; one mixin module per table family builds on it;
`repo` composes them into `CodeRepo`. Imports point one way, toward the
leaves.

RMT discipline: every version row mirrors EVERY persisted column. The
``CODE_*_COLS`` dicts (column -> default) are the single source of truth;
``build_*_row`` raises on unknown keys so a schema/builder drift is a unit
test failure, not a silent zeroed column under FINAL (as in the memory
store). Tombstoning reads the live row FINAL and re-emits it with
tombstoned_at set — never a partial row.
"""

from __future__ import annotations

from server.code.store.index.facets import (
    CLOSEST_KIND_WEIGHTS,
    closest_scores,
)
from server.code.store.index.graph_stats import (
    parse_published_summary,
    summary_stamp,
)
from server.code.store.index.repo import (
    CodeRepo,
)
from server.code.store.index.rows import (
    CODE_PR_COLS,
    CODE_REVIEW_BRIEF_COLS,
    CODE_SYMBOL_STATS_COLS,
    VERSION_INVARIANT_COLS,
    build_row,
    version_now,
)
from server.code.store.index.sql import (
    is_test_path,
    like_contains,
    skip_tests_sql,
)
from server.code.store.index.status import (
    counts_fingerprint,
    parse_shared_coverage,
    state_fingerprint,
)
from server.code.store.index.symbol_rank import (
    SYMBOL_NOISE_PATH_RE,
    collapse_outline_rows,
    demote_generated_paths,
    demote_member_rows,
    demote_noise_paths,
    is_generated_path,
    is_noise_path,
    prefer_scip_edges,
    return_type_from_signature,
)

__all__ = [
    "CLOSEST_KIND_WEIGHTS",
    "CODE_PR_COLS",
    "CODE_REVIEW_BRIEF_COLS",
    "CODE_SYMBOL_STATS_COLS",
    "CodeRepo",
    "SYMBOL_NOISE_PATH_RE",
    "VERSION_INVARIANT_COLS",
    "build_row",
    "closest_scores",
    "collapse_outline_rows",
    "counts_fingerprint",
    "demote_generated_paths",
    "demote_member_rows",
    "demote_noise_paths",
    "is_generated_path",
    "is_noise_path",
    "is_test_path",
    "like_contains",
    "parse_published_summary",
    "parse_shared_coverage",
    "prefer_scip_edges",
    "return_type_from_signature",
    "skip_tests_sql",
    "state_fingerprint",
    "summary_stamp",
    "version_now",
]
