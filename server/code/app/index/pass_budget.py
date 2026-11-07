"""The ingest pass's result, its budget and the per-repository plan the legs share."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from server.settings import settings

_PR_NUMBER_RE = re.compile(r"#(\d+)")


@dataclass
class CodeIngestResult:
    commits: int = 0
    files_indexed: int = 0
    files_errored: int = 0  # indexed with a non-empty code_files.index_error
    files_excluded: int = 0  # refused by the ingest denylist
    chunks_new: int = 0
    chunks_kept: int = 0
    embedded: int = 0
    embed_stubs: int = 0
    symbols_new: int = 0
    edges_new: int = 0
    packs_built: int = 0
    briefs_built: int = 0  # Review briefs written beside the packs
    briefs_failed: int = 0
    described: int = 0
    head: str = ""
    errors: list[str] = field(default_factory=list)
    note: str = ""
    reconciled: int = 0  # stale paths found by content reconciliation this pass
    healed: int = 0  # of those, re-ingested this pass
    rechunked: int = 0  # files re-ingested this pass because the extractor stamp moved
    # Of those, files whose scoped stages found nothing to write
    rechunk_identical: int = 0
    # Untouched files re-resolved because a touched file
    # changed the name table they depend on, and declined sites bound
    # directly when their name gained its single definition
    dependents_reingested: int = 0
    dependents_queued: int = 0  # overflow beyond the per-commit cap
    dependents_drained: int = 0  # queued paths reconcile re-extracted this pass
    closure_resolved: int = 0
    dependents_ms: int = 0
    closure_ms: int = 0
    # Precomputed risk rows written for the commits' changed
    # files and the files their calls reach; refreshes that failed
    unit_risk_rows: int = 0
    unit_risk_failed: int = 0
    # The pass's units, planned up front and counted as done,
    # so a pass the budget cut still says how much it set out to do. Items
    # are files: a commit's changed files, a bootstrap slice, a re-chunk
    # round, a heal round.
    commits_planned: int = 0
    commits_done: int = 0
    files_planned: int = 0
    files_done: int = 0
    bootstrap_files: int = 0
    namespaces_walked: int = 0
    # The budget stopped the pass before some unit started; `resume` names
    # where (namespace and stage) — the cursors in code_index_state already
    # hold the exact point, this is what the pass row shows.
    partial: bool = False
    resume: str = ""
    # The first cut of each phase (`commits`, `bootstrap`,
    # `walk`), so the next pass starts each phase at the entry this one
    # starved; `resume` shows them all, first cut first.
    cuts: dict[str, str] = field(default_factory=dict)
    budget_s: float = 0.0
    elapsed_s: float = 0.0
    # Per-file resolution reads the rounds answered from memory / sent on.
    read_hits: int = 0
    read_misses: int = 0
    # (org, namespace, tier) -> "" when the tier served the entry this pass, else its
    # error: the tier-health records the pass writes at its end. A failure is never
    # overwritten by a later success of the same tier.
    tier_outcomes: dict[tuple[str, str, str], str] = field(default_factory=dict)

    def tier_ok(self, cfg: Any, tier: str) -> None:
        self.tier_outcomes.setdefault((cfg.org, cfg.namespace, tier), "")

    def tier_failed(self, cfg: Any, tier: str, error: BaseException | str) -> None:
        text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else error
        key = (cfg.org, cfg.namespace, tier)
        if not self.tier_outcomes.get(key):
            self.tier_outcomes[key] = text or "failed"

    def cut(self, where: str) -> None:
        """The budget stopped the pass before `where` started. `where` is
        `<namespace>:<leg>[@detail]`; the first cut per phase is kept and
        `resume` lists them, first cut first."""
        self.partial = True
        phase = cut_phase(where)
        if phase and phase not in self.cuts:
            self.cuts[phase] = where
        self.resume = (
            "; ".join(self.cuts.values())[:200] if self.cuts else (self.resume or where[:200])
        )

    def rows_written(self) -> int:
        """Best effort: the rows the pass's legs report having written."""
        return int(
            self.chunks_new
            + self.symbols_new
            + self.edges_new
            + self.packs_built
            + self.briefs_built
            + self.unit_risk_rows
        )


# The legs each phase of the pass runs — a cut point
# `<namespace>:<leg>` names the phase to start at that entry next pass.
_PHASE_OF_LEG = {
    "commits": "commits",
    "bootstrap": "bootstrap",
    "walk": "walk",
    "rechunk": "walk",
    "heal": "walk",
}


def cut_phase(where: str) -> str:
    """The phase a cut point belongs to ('' for a shape this pass never
    writes): `code-a:commits@abc+3` → `commits`, `code-a:rechunk@700/22136`
    → `walk`."""
    _ns, _sep, rest = where.partition(":")
    return _PHASE_OF_LEG.get(rest.split("@", 1)[0].strip(), "") if _sep else ""


def cut_points(cursor: str) -> dict[str, str]:
    """{phase: namespace} from the cursor the previous pass
    left (`resume`, one cut per phase joined by `; `). Empty for no cursor,
    and for anything the parser does not recognise."""
    out: dict[str, str] = {}
    for seg in str(cursor or "").split(";"):
        seg = seg.strip()
        phase = cut_phase(seg)
        ns = seg.partition(":")[0].strip()
        if phase and ns and phase not in out:
            out[phase] = ns
    return out


def rotate_to(entries: list[Any], namespace: str, key: Any = None) -> list[Any]:
    """`entries` starting at the one named `namespace`, the
    ones before it moved to the end; unchanged when nothing is named or the
    name is not there (a retired entry). `key` reads an entry's namespace
    (default: `.namespace`)."""
    read = key or (lambda e: e.namespace)
    for i, e in enumerate(entries):
        if read(e) == namespace:
            return entries[i:] + entries[:i]
    return list(entries)


def git_text_source(git_dir: str, sha: str) -> Any:
    """Sibling texts the ingest reads come from the mirror at
    `sha` inside the block (the ingest module stays a lazy import here)."""
    from server.code.app.index.ingest_resolve import git_text_source as source

    return source(git_dir, sha)


@dataclass
class PassBudget:
    """The instant after which the pass starts no new unit —
    `code_ingest_pass_budget_fraction` of the worker's deadline from pass
    start. Checked between commits, between rounds of the bootstrap slice,
    the re-chunk and the heal, and between namespaces."""

    started: float
    stop_after: float

    def exhausted(self) -> bool:
        # Every unit boundary is also where a stopped pass stops writing.
        from server.workers import pass_context

        pass_context.check_stop()
        return time.monotonic() >= self.stop_after

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def seconds(self) -> float:
        return max(0.0, self.stop_after - self.started)

    def phase(self, share_left: float) -> PassBudget:
        """The budget a phase runs under when it must leave
        `share_left` of this one to the phases after it — the same start,
        an earlier stop. 0 leaves nothing (this budget itself)."""
        share_left = min(1.0, max(0.0, float(share_left)))
        if share_left <= 0.0:
            return self
        return PassBudget(self.started, self.started + self.seconds * (1.0 - share_left))


def pass_budget(started: float | None = None) -> PassBudget:
    started = time.monotonic() if started is None else started
    deadline = max(0.0, float(settings.code_ingest_max_pass_seconds))
    fraction = min(1.0, max(0.0, float(settings.code_ingest_pass_budget_fraction)))
    return PassBudget(started, started + deadline * fraction)


@dataclass
class RepoPass:
    """What loop one learned about one configured entry, for the legs that
    follow it in the pass."""

    cfg: Any
    repo: Any
    head: str
    watermark: str
    self_service: bool
    bootstrap: bool = False  # no watermark, self-service: a slice of the first index
    walk: bool = False  # a watermark: the reconcile and the re-chunk are owed
    commits: int = 0  # new commits this pass indexed for the entry (the table-count trigger)
    # The entry's new commits, oldest first, left for the
    # pass-wide scheduler instead of ingested in the entry's turn.
    pending: list[str] = field(default_factory=list)
