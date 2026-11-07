"""The commit walk: each namespace's commits as a stream, walked oldest first across
namespaces under a per-namespace share of the pass budget."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from server.auth.context import current_caller
from server.code.app.index.pass_budget import (
    CodeIngestResult,
    PassBudget,
    RepoPass,
    git_text_source,
)
from server.code.app.index.pass_commit import _ingest_commit
from server.llm import customer
from server.settings import settings
from server.workers import pass_context
from server.workers.status import set_worker_identity

logger = logging.getLogger("loom.workers.code_ingest")


# `<base>..<sha>:<files durable>` — how far a commit too big
# for one pass has been ingested. One per entry: commits land in order.
SLICE_KEY = "commit_slice"


async def _slice_done(repo: Any, org: str, ns: str, slice_key: str) -> int:
    """Files of `slice_key`'s commit an earlier pass already made durable; 0
    when the cursor names another commit (a force-push moved the base)."""
    try:
        raw = await repo.get_state(org, ns, SLICE_KEY)
    except Exception:  # noqa: BLE001 — no cursor reads as a fresh commit
        return 0
    key, _, n = str(raw or "").rpartition(":")
    return int(n) if key == slice_key and n.isdigit() else 0


class CommitStream:
    """One entry's pending commits, ingested one `step` at a time: the
    per-entry setup — the embedder, the describe gate, the
    denylist — happens once, on the first step, so a scheduler can
    interleave entries without paying it per commit."""

    def __init__(
        self,
        cfg: Any,
        repo: Any,
        head: str,
        watermark: str,
        commits: list[str],
        result: CodeIngestResult,
    ) -> None:
        self.cfg, self.repo, self.head, self.result = cfg, repo, head, result
        self.base = watermark
        self.commits = list(commits)
        self.index = 0
        self.landed = 0
        # committer unix seconds per sha: the scheduler's order
        self.times: dict[str, int] = {}
        self._ctx: dict[str, Any] | None = None

    @property
    def remaining(self) -> int:
        return len(self.commits) - self.index

    @property
    def next_sha(self) -> str:
        return self.commits[self.index]

    @property
    def next_time(self) -> int:
        return self.times.get(self.next_sha, 0)

    async def next_files(self) -> int:
        """Changed files of the next commit (its cost driver); 0 unknown."""
        from server.code.app.index import gitrepo

        try:
            return len(await gitrepo.diff_name_only(self.cfg.git_dir, self.base, self.next_sha))
        except Exception:  # noqa: BLE001 — an unknown size never blocks a commit
            return 0

    async def _prepare(self) -> dict[str, Any]:
        from server.code.app.index import gitrepo
        from server.code.app.index.denylist import load_denylist
        from server.code.app.index.ingest import ingest_file, tombstone_file
        from server.code.app.index.packs import build_pr_pack, estimate_tokens
        from server.code.store.index import build_row
        from server.embedding.embedder import get_embedder
        from server.observability.budget import namespace_over_cap

        cfg = self.cfg
        describe = settings.code_context_description_enabled
        llm = None
        if describe:
            if await namespace_over_cap(cfg.org, cfg.namespace):
                describe = False
                self.result.note = "describe skipped: namespace over budget cap"
            else:
                from server.llm.client import get_llm_client

                llm = get_llm_client()
        return {
            "embedder": get_embedder(),
            "llm": llm,
            "describe": describe,
            "prefixes": cfg.path_prefixes,
            "result": self.result,
            "build_pack": build_pr_pack,
            "estimate_tokens": estimate_tokens,
            "build_row": build_row,
            "gitrepo": gitrepo,
            "ingest_file": ingest_file,
            "tombstone_file": tombstone_file,
            "embed_reuse_from": cfg.embed_reuse_from,
            "denylist": await load_denylist(cfg, self.head, gitrepo.show_file),
            "build_brief": _brief_builder(cfg),
        }

    async def step(self, clock: Any = None) -> bool | None:
        """Ingest the next commit and advance the watermark; False when it
        failed (the watermark stays, the entry is done for this pass); None
        when `clock` stopped it between rounds: the slice's
        files are durable, the slice cursor names how many, and the next pass
        resumes the same commit there — the watermark moves only when the
        commit's last file lands."""
        from server.observability import cost_accumulator
        from server.workers.cost_event import emit_worker_cost_event_for

        if self._ctx is None:
            self._ctx = await self._prepare()
        cfg, sha = self.cfg, self.next_sha
        org, ns = cfg.org, cfg.namespace
        slice_key = f"{self.base}..{sha}"
        skip = await _slice_done(self.repo, org, ns, slice_key)
        commit_started = time.monotonic()
        cost_accumulator.begin_op()
        try:

            async def _saved(n: int) -> None:
                wrote.append(n)
                await self.repo.set_state(org, ns, SLICE_KEY, f"{slice_key}:{n}")

            wrote: list[int] = []

            with git_text_source(cfg.git_dir, sha):
                done = await _ingest_commit(
                    self.repo,
                    org,
                    ns,
                    cfg.git_dir,
                    self.base,
                    sha,
                    skip=skip,
                    clock=clock,
                    on_round=_saved,
                    **self._ctx,
                )
        except customer.HOLDS:
            self.index = len(self.commits)  # held, no failure: the watermark stays
            self.result.held(cfg)
            return False
        except Exception as e:
            self.result.errors.append(f"{sha[:12]}: {type(e).__name__}: {e}")
            logger.exception("code_ingest: commit %s failed", sha[:12])
            pass_context.note_failure(e, ns)
            self.result.tier_failed(cfg, "ingest", f"commit {sha[:12]}: {type(e).__name__}: {e}")
            self.index = len(self.commits)
            return False
        finally:
            emit_worker_cost_event_for(
                org=org,
                namespace=ns,
                operation="worker.code_ingest",
                started=commit_started,
            )
        if done is not None:  # the cursor was saved after its last round
            self.result.cut(f"{ns}:slice@{sha[:12]}+{done}", cfg=cfg)
            logger.warning(
                "code_ingest: %s commit %s ingested to file %d this pass "
                "— the next pass continues it",
                cfg.label,
                sha[:12],
                done,
            )
            return None
        if skip or wrote:
            await self.repo.set_state(org, ns, SLICE_KEY, "")
        await self.repo.set_state(org, ns, "git_head", sha, detail=f"base {self.base[:12]}")
        self.result.commits += 1
        self.result.commits_done += 1
        self.landed += 1
        self.index += 1
        self.base = sha
        return True

    def cut(self, budget: PassBudget) -> None:
        self.result.cut(
            f"{self.cfg.namespace}:commits@{self.base[:12]}+{self.remaining}", cfg=self.cfg
        )
        logger.warning(
            "code_ingest: %s pass budget (%.0f s) reached with %d of %d commits ingested "
            "— the next pass continues from %s",
            self.cfg.label,
            budget.seconds,
            self.index,
            len(self.commits),
            self.base[:12],
        )


# Commits older than this sort as if committed then — an
# old-dated backlog (a branch re-pointed at old history) ties, in entry
# order, instead of holding the phase.
_OLDEST_FIRST_HORIZON_S = 86_400


async def ingest_oldest_first(
    plans: list[RepoPass],
    result: CodeIngestResult,
    budget: PassBudget,
) -> None:
    """The pass's pending commits across EVERY entry, the one
    that has waited longest first (committer time; merges land with the merge
    time). Walking each entry's whole backlog in its turn would let one
    release tree's backlog hold the phase pass after pass while another
    entry's single merge waits behind it. Oldest
    first bounds every commit's wait by the work committed before it, which
    is the least worst-case staleness a budget-bound pass can give; per entry
    the order stays oldest first, so the watermark contract holds. At the
    budget every entry still waiting is cut, the longest-waiting first (the
    next pass starts at its entry). `code_ingest_lanes` lanes pop the same
    order at once, each a different entry."""
    import heapq

    heap, streams = await _commit_streams(plans, result)
    pace = _Pace(budget)
    deferred: list[CommitStream] = []

    async def lane() -> None:
        # A stream is off the heap while a lane steps it, so no two lanes
        # ever run one entry's commits: each entry stays oldest first.
        while heap and not budget.exhausted():
            _, _, order, stream = heapq.heappop(heap)
            try:
                await lane_step(order, stream)
            except Exception as e:  # one entry never ends the phase for the rest
                # `step` isolates the commit itself; this is its setup or a
                # cursor write. The entry is done for this pass, its watermark
                # where the last landed commit left it.
                result.errors.append(f"{stream.cfg.label}: commits: {type(e).__name__}: {e}")
                logger.exception("code_ingest: commit phase of %s failed", stream.cfg.label)
                pass_context.note_failure(e, stream.cfg.namespace)
                result.tier_failed(stream.cfg, "ingest", f"commits: {type(e).__name__}: {e}")

    async def lane_step(order: int, stream: CommitStream) -> None:
        files = await stream.next_files()
        if not pace.fits(files):
            deferred.append(stream)  # too big for what is left: first next pass
            return
        # A commit bigger than a round shares the phase while
        # other entries wait — it gets `code_ingest_slice_share` of the budget
        # this pass, then yields; alone it may use all of it.
        clock: Any = pace
        if heap and files > (_round_files() or files):
            clock = _ShareClock(pace, time.monotonic() + budget.seconds * _slice_share())
        # Attribution is ContextVar state, so each lane task tags its own entry.
        pass_context.set_namespace(stream.cfg.namespace)
        set_worker_identity("code_ingest", org=stream.cfg.org, namespace=stream.cfg.namespace)
        # The caller's model calls when code.refresh runs the pass (its token's subject).
        with customer.for_org(
            stream.cfg.org, namespace=stream.cfg.namespace, subject=current_caller()[0]
        ):
            landed = await stream.step(clock=clock)
        if landed is None:
            deferred.append(stream)  # sliced: continues next pass
        elif landed and stream.remaining:
            heapq.heappush(heap, (stream.next_time, stream.landed, order, stream))

    lanes = min(int(settings.code_ingest_lanes), len(heap))
    if lanes > 1:
        # A task group: a lane that still raises cancels its siblings instead
        # of leaving them stepping entries after the pass has returned.
        async with asyncio.TaskGroup() as tg:
            for _ in range(lanes):
                tg.create_task(lane())
    else:
        await lane()
    waiting = deferred + [h[3] for h in heap]
    for stream in sorted(waiting, key=lambda st: st.next_time):
        stream.cut(budget)
    for plan, stream in streams:
        plan.commits = stream.landed


async def _commit_streams(
    plans: list[RepoPass], result: CodeIngestResult
) -> tuple[list[tuple[int, int, int, CommitStream]], list[tuple[RepoPass, CommitStream]]]:
    """One stream per entry with pending commits, and the heap of them in
    oldest-first order."""
    import heapq

    from server.code.app.index import gitrepo

    floor = int(time.time()) - _OLDEST_FIRST_HORIZON_S
    # (time, landed, entry order): commits older than the horizon share the
    # floor time, and `landed` breaks that tie round-robin — entry order alone
    # would serve the first old backlog to exhaustion before the next one.
    heap: list[tuple[int, int, int, CommitStream]] = []
    streams: list[tuple[RepoPass, CommitStream]] = []
    for plan in plans:
        if not plan.pending:
            continue
        stream = CommitStream(plan.cfg, plan.repo, plan.head, plan.watermark, plan.pending, result)
        result.commits_planned += len(plan.pending)
        try:
            meta = await gitrepo.commit_meta_many(plan.cfg.git_dir, plan.pending)
        except Exception:  # noqa: BLE001 — unknown times sort first, in entry order
            meta = {}
        stream.times = {sha: max(floor, int(meta.get(sha, ("", 0))[1])) for sha in plan.pending}
        heapq.heappush(heap, (stream.next_time, 0, len(streams), stream))
        streams.append((plan, stream))
    return heap, streams


def _round_files() -> int:
    from server.code.app.index.round_reads import round_size

    return round_size()


def _slice_share() -> float:
    return min(1.0, max(0.05, float(settings.code_ingest_slice_share)))


class _ShareClock:
    """The pass clock with an earlier stop for one big commit,
    so its slice leaves the rest of the phase to the entries waiting."""

    def __init__(self, pace: _Pace, until: float) -> None:
        self.pace, self.until = pace, until

    def can_start(self, files: int) -> bool:
        if not self.pace.can_start(files):
            return False
        started_any = self.pace.landed > 0
        return not started_any or time.monotonic() + max(0, files) * self.pace.rate <= self.until

    def observe(self, files: int, seconds: float) -> None:
        self.pace.observe(files, seconds)


class _Pace:
    """Whether a commit of `files` changed files can finish
    before the pass deadline, at the seconds per file this pass has seen
    (the setting's prior until a commit lands). The first commit of the pass
    always starts — a commit bigger than any pass must still make progress."""

    def __init__(self, budget: PassBudget) -> None:
        self.budget = budget
        self.deadline = budget.started + float(settings.code_ingest_max_pass_seconds) * 0.95
        self.rate = float(settings.code_ingest_seconds_per_file)
        self.landed = 0

    def fits(self, files: int) -> bool:
        """Whether the next commit may START: a big commit is ingested in
        rounds, so the unit that must fit is one round, not the
        whole commit — a 315-file commit starts, lands what fits, and the
        next pass resumes it."""
        if self.landed == 0 or files <= 0:
            return True
        from server.code.app.index.round_reads import round_size

        unit = min(files, round_size() or files)
        return time.monotonic() + unit * self.rate <= self.deadline

    def can_start(self, files: int) -> bool:
        """Whether a round of `files` can finish before the
        deadline. The pass's first unit always starts (progress); after it a
        round starts only inside the phase budget and the predicted finish."""
        if self.landed == 0:
            return True
        if self.budget.exhausted():
            return False
        return time.monotonic() + max(0, files) * self.rate <= self.deadline

    def observe(self, files: int, seconds: float) -> None:
        self.landed += 1
        if files > 0 and seconds > 0:
            self.rate = 0.7 * self.rate + 0.3 * (seconds / files)


def _brief_builder(cfg: Any) -> Any:
    """The review-brief builder the commit step calls after the pack. Bound to the
    repo entry so the brief classifies paths against the same indexed prefixes as
    code.impact."""
    from server.code.app.index.review_brief_build import build_for_merge
    from server.code.app.review_brief import call_code_op

    async def _build(repo: Any, org: str, ns: str, **kw: Any) -> None:
        await build_for_merge(
            repo, org, ns, path_prefixes=tuple(cfg.path_prefixes), call=call_code_op, **kw
        )

    return _build
