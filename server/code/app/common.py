"""Shared plumbing for the code.* handlers: kill switch, namespace
defaulting, calibration resolution and fire-and-forget trace logging. The
code plane's span helpers live in `server.code.app.spans`."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypedDict

from server.code.app.spans import (
    current_otel_ids,
    set_code_span_attributes,
)
from server.code.app.ttl_cache import TtlCache
from server.core.background import create_deferred_task
from server.core.errors import PreconditionFailedError
from server.core.relative_paths import without_dot_slashes
from server.observability.consumer import current_consumer
from server.settings import settings

logger = logging.getLogger(__name__)

_TRACE_TASKS: set[asyncio.Task[None]] = set()


def repo_relative(path: str) -> str:
    """A path as the index stores it: `./src/x` and `/src/x` → `src/x`, and
    `.github/workflows/ci.yml` KEPT. `str.lstrip("./")` would strip characters, not a
    prefix, and eat the dot of every dot-directory path."""
    p = path.strip().replace("\\", "/")
    p = without_dot_slashes(p)
    return p.lstrip("/")


def tier_of(rows: list[Any]) -> str:
    """Bounded tier summary for a result set: 'scip' when every row is
    compiler-indexed, 'treesitter' when none is, 'mixed' otherwise, '' for
    no rows. Rows may be dicts or objects with a `tier` attribute."""
    tiers: set[str] = set()
    for r in rows:
        t = r.get("tier") if isinstance(r, dict) else getattr(r, "tier", "")
        tiers.add(str(t or ""))
    tiers.discard("")
    if not tiers:
        return ""
    if tiers == {"scip"}:
        return "scip"
    if "scip" not in tiers:
        return "treesitter"
    return "mixed"


def risk_for(rows: list[Any]) -> str:
    """Retrieval-risk convention for structural (graph/symbol) answers:
    nothing -> 'abstain'; every row compiler-indexed -> 'low'; any
    parser-tier row -> 'medium' (name-resolved edges can be wrong)."""
    if not rows:
        return "abstain"
    return "low" if tier_of(rows) == "scip" else "medium"


if TYPE_CHECKING:
    from server.code.domain.index.repos import RepoConfig
    from server.code.domain.index.state import IndexState
    from server.code.store.index import CodeRepo


def require_code_intel() -> None:
    """Kill switch (rollback level 1): every code.* handler but `code.repo.remove`
    calls this first."""
    if not settings.code_intel_enabled:
        raise PreconditionFailedError(
            "code intelligence is disabled on this deployment (LOOM_CODE_INTEL_ENABLED=false)"
        )


def plane_org_for(op_name: str) -> str:
    """The org an op is authorized against — and its handler reads — when the
    body names none. code.* defaults to the code plane (its handler's
    `resolve_code_identity`), tracker.* to the tracker org (which falls back to
    the code org: the two planes mirror one repository). Every
    other op returns "", and an absent org is the verified token's own single
    org (`server.transport.gates.resolve_org`)."""
    if op_name.startswith("code."):
        return str(settings.code_org)
    if op_name.startswith("tracker."):
        return str(settings.tracker_org or settings.code_org or "")
    return ""


def plane_namespace_for(op_name: str) -> str:
    """The namespace a code.* op is authorized against — and its handler
    reads — when the body names none: the deployment's code namespace. ""
    for every other op."""
    return str(settings.code_namespace) if op_name.startswith("code.") else ""


def resolve_code_identity(org: str, namespace: str) -> tuple[str, str]:
    """Default empty org/namespace to the deployment's code namespace so a
    caller on a personal namespace lands on the indexed repo without knowing
    its name. Explicit values pass through (authz already ran on them). A
    deployment that sets no default (LOOM_CODE_ORG / LOOM_CODE_NAMESPACE empty)
    refuses a request naming none: a read of namespace "" answers nothing."""
    from server.core.errors import ValidationError

    org = org or settings.code_org
    namespace = namespace or settings.code_namespace
    if not org or not namespace:
        missing = " and ".join(n for n, v in (("org", org), ("namespace", namespace)) if not v)
        raise ValidationError(
            f"no code {missing} given and this deployment sets no default "
            f"(LOOM_CODE_{'ORG' if not org else 'NAMESPACE'}); name the code namespace "
            "to read (code.repos lists them)"
        )
    return org, namespace


async def resolve_code_thresholds(org: str, ns: str) -> tuple[float, float]:
    """(abstain_threshold, noise_floor) — per-namespace calibration when
    present (code.calibrate seeds it), scalar fallbacks otherwise."""
    from server.memory.app.embedder_calibration import (
        effective_abstain_threshold,
        effective_noise_floor,
        resolve_calibration,
    )

    calibration = await resolve_calibration(org, ns)
    return (
        effective_abstain_threshold(settings.memory_search_abstain_threshold, calibration),
        effective_noise_floor(settings.memory_search_min_similarity, calibration),
    )


def new_trace_run_id() -> str:
    return uuid.uuid4().hex


def freshness_status(lag: int, stale: bool) -> str:
    """One word an agent can read without arithmetic:
    `fresh` (at the tip), `behind` (trails by a few merged commits, under the
    stale threshold), `stale` (over it), `unknown` (no watermark or the clone
    was unreadable, lag -1)."""
    if lag < 0:
        return "unknown"
    if stale:
        return "stale"
    return "fresh" if lag == 0 else "behind"


# (org, ns) -> as_of block. The block is one point read of code_index_state plus a
# cached rev-list, and a code.search fan-out asks for it once per namespace per call.
# 30 s per namespace per worker; code.refresh and the ingest pass invalidate it when
# git_head moves.
_AS_OF_CACHE: TtlCache[tuple[str, str], dict[str, Any]] = TtlCache(30.0, maxsize=2000)


def invalidate_index_as_of(org: str, ns: str) -> None:
    _AS_OF_CACHE.pop((org, ns))


# (org, ns) -> note. Sixty seconds per namespace; the count behind it is
# `table_counts`' fingerprint-cached scan, held a minute while the entry is building and
# ten otherwise (code_context).
_COVERAGE_NOTE_CACHE: TtlCache[tuple[str, str], str] = TtlCache(60.0, maxsize=256)


def coverage_note_text(coverage: float) -> str:
    """The sentence code.search carries below the ready threshold; "" at or
    above it."""
    from server.code.app.index.bootstrap import is_searchable

    if is_searchable(coverage):
        return ""
    return f"lexical only until embeddings finish ({int(coverage * 100)}% embedded)"


async def embedding_coverage_note(repo: CodeRepo, org: str, ns: str) -> str:
    """The note "lexical only until embeddings finish (41% embedded)" while the
    namespace's vector leg covers less than the ready threshold of its chunks, ""
    otherwise. Fail-open to "": a note must never sink a served answer."""
    hit = _COVERAGE_NOTE_CACHE.get((org, ns))
    if hit is not None:
        return hit
    note = ""
    try:
        from server.code.app.index_status import _counts_max_age_s, coverage_fields
        from server.code.store.index import state_fingerprint

        state = await repo.all_state(org, ns)
        counts = await repo.table_counts(
            org,
            ns,
            fingerprint=state_fingerprint(state),
            max_age_s=_counts_max_age_s(state),
        )
        note = coverage_note_text(float(coverage_fields(counts)["embedding_coverage"]))
    except Exception as e:  # noqa: BLE001 — the note is advisory
        logger.debug("code coverage note unavailable for %s/%s: %s", org, ns, e)
    _COVERAGE_NOTE_CACHE.put((org, ns), note)
    return note


async def index_as_of(repo: CodeRepo, org: str, ns: str) -> dict[str, Any]:
    """Provenance + freshness block for conversational responses: which
    branch/commit the index reflects, when it got there, how many merged
    commits it trails the fetched tip by (`lag_commits`, -1 unknown), one
    `stale` bool an agent can gate on before trusting a hit, and `status`
    (fresh | behind | stale | unknown) — the freshness envelope in one word.
    Cached answers are hash-verified against THIS state on every hit, so
    'current as of git_head' is truthful for them too. Served from a 30 s
    per-namespace cache. Fail-open: provenance must never sink a served
    answer."""
    hit = _AS_OF_CACHE.get((org, ns))
    if hit is not None:
        block = dict(hit)
        if block:
            set_code_span_attributes(lag_commits=block["lag_commits"], stale=block["stale"])
        return block
    try:
        state = await repo.all_state(org, ns)
        head = state.get("git_head") or {}
        commit = str(head.get("value") or "")
        if not commit:
            return {}
        from server.code.app.index.mirrors import present_dir
        from server.code.domain.index.repos import repo_for_namespace

        cfg = repo_for_namespace(org, ns)
        updated = head.get("updated_at")
        if cfg is None:
            lag = -1
        elif await present_dir(cfg):
            lag = await _lag_commits_cached(cfg, commit)
        else:
            lag = _published_lag(state, cfg, commit)
        fetched = (state.get("clone_head") or {}).get("updated_at")
        unfetched = mirror_fetch_overdue(fetched)
        stale = lag > settings.code_stale_after_commits or unfetched
        # Freshness on the span wherever as_of is emitted (search, ask, grep):
        # "was the index stale when it served this" joins latency and quality.
        set_code_span_attributes(lag_commits=lag, stale=stale)
        block = {
            "branch": cfg.git_branch if cfg else "",
            "commit": commit,
            "indexed_at": updated.isoformat() if updated is not None else "",
            "lag_commits": lag,
            "stale": stale,
            "status": freshness_status(lag, stale),
        }
        if unfetched:  # the lag above is against a stale mirror
            block["stale_reason"] = "mirror_not_fetched"
            block["fetched_at"] = fetched.isoformat() if isinstance(fetched, datetime) else ""
        if failing := failing_tiers(state, cfg, ingest_lag=lag):
            # A failing tier stops moving: what it answers may trail the head, and why.
            block["failing_tiers"] = failing
        _AS_OF_CACHE.put((org, ns), block)
        return dict(block)
    except Exception:  # noqa: BLE001 — unknown freshness, not an error
        return {}


def failing_tiers(
    state: IndexState, cfg: RepoConfig | None, *, ingest_lag: int = -1
) -> list[dict[str, Any]]:
    """The entry's failing tiers from their health records: tier, kind, failing_since, the
    failed passes in a row, the fix, and for a tier with a watermark how many commits
    that watermark trails the fetched tip by: `ingest_lag` (the block's own count) for the
    parser tier, the published count for a compiler tier; -1 unknown."""
    from server.code.app.index_status import published_clone_head, published_lags
    from server.code.domain.index import tier_health

    tip = published_clone_head(state, cfg.git_branch) if cfg else ""
    out: list[dict[str, Any]] = []
    for tier, health in tier_health.from_state(state).items():
        if not health.failing:
            continue
        row: dict[str, Any] = {
            "tier": tier,
            "kind": health.kind,
            "failing_since": health.failing_since,
            "failed_passes": health.failed_passes,
            "fix": health.fix,
        }
        if tier == "ingest":
            row["lag_commits"] = ingest_lag
        elif key := tier_health.WATERMARK_FOR_TIER.get(tier):
            mark = str((state.get(key) or {}).get("value") or "")
            row["lag_commits"] = published_lags(state, {key: mark}, tip)[key] if mark else -1
        out.append(row)
    return out


def fetch_freshness(fetched_at: Any, now: datetime | None = None) -> tuple[str, bool]:
    """The mirror's last fetch as ISO-8601 UTC ('' when the `clone_head` row is missing)
    and whether it is overdue by `mirror_fetch_overdue`'s rule: what `code.index_status`
    and `code.repos` carry so a client's freshness verdict sees a stalled mirror."""
    if not isinstance(fetched_at, datetime):
        return "", False
    ts = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=UTC)
    return ts.isoformat(), mirror_fetch_overdue(fetched_at, now)


def mirror_fetch_overdue(fetched_at: Any, now: datetime | None = None) -> bool:
    """The entry's mirror has not fetched for longer than
    `code_fetch_stale_after_seconds`. `fetched_at` is the `clone_head` row's `updated_at`
    (UTC, naive from ClickHouse); unknown is never overdue, so a deployment without the
    row keeps the lag-only verdict."""
    limit = int(settings.code_fetch_stale_after_seconds)
    if limit <= 0 or not isinstance(fetched_at, datetime):
        return False
    ts = fetched_at if fetched_at.tzinfo else fetched_at.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - ts).total_seconds() > limit


# (git_dir, branch, git_head) -> (expires_monotonic, lag). One rev-list per
# minute per repo instead of one subprocess per search call.
_LAG_CACHE: TtlCache[tuple[str, str, str], int] = TtlCache(60.0, maxsize=64)


def _published_lag(state: Any, cfg: Any, git_head: str) -> int:
    """A host without the mirror has no clone to count with: the lag another process
    published for this same index head and clone tip, else -1 (unknown)."""
    from server.code.app.index_status import published_clone_head, published_lags

    tip = published_clone_head(state, cfg.git_branch)
    return published_lags(state, {"git_head": git_head}, tip)["git_head"]


async def _lag_commits_cached(cfg: Any, git_head: str) -> int:
    from server.code.app.index import gitrepo

    key = (cfg.git_dir, cfg.git_branch, git_head)
    hit = _LAG_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        tip = await gitrepo.head_sha(cfg.git_dir, cfg.git_branch)
        lag = await gitrepo.commit_count_between(cfg.git_dir, git_head, tip)
    except Exception:  # noqa: BLE001 — clone unreadable here: unknown, not stale
        lag = -1
    _LAG_CACHE.put(key, lag)
    return lag


def path_coverage(cfg: RepoConfig | None, path_prefix: str) -> tuple[bool, tuple[str, ...]]:
    """Is `path_prefix` inside (or an ancestor of) the indexed path prefixes?
    Returns (covered, indexed_prefixes). Empty prefix == whole index ==
    covered; an ancestor such as "src" of "src/" is covered too (the index
    simply holds more than was asked for)."""
    prefixes = tuple(cfg.path_prefixes) if cfg is not None else ()
    p = repo_relative(path_prefix or "")
    if not p or not prefixes:
        return True, prefixes
    p_slash = p if p.endswith("/") else p + "/"
    for pre in prefixes:
        if p_slash.startswith(pre) or pre.startswith(p_slash):
            return True, prefixes
    return False, prefixes


def schedule_code_trace(
    *,
    org: str,
    ns: str,
    op: str,
    query: str,
    route: str,
    returned_ids: list[str],
    returned_paths: list[str],
    returned_scores: list[float],
    top_k: int,
    tokens_returned: int,
    abstain: bool,
    retrieval_risk: str,
    embedding_model: str,
    started_monotonic: float,
    trace_run_id: str,
    consumer: str = "",
    agent: str = "-",
    user_id: str = "-",
    returned_via: list[str] | None = None,
    lexical_ranks: list[int] | None = None,
    vector_ranks: list[int] | None = None,
    chunks_matched: int = 0,
    path_prefix: str = "",
    verdict: str = "",
    stage_ms: dict[str, int] | None = None,
    reason: str = "",
    args: dict[str, Any] | None = None,
    error_code: str = "",
    error_text: str = "",
    as_of: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget insert into loom.code_search_trace (async_insert, never on the
    read path's latency).

    The row is decidable after the fact: `as_of_commit` / `lag_commits` are the index's
    git_head and its lag behind the fetched tip when the op answered, read off the 30 s
    per-namespace `index_as_of` cache INSIDE the write task, never on the request path
    (`as_of` passes a block the handler already holds; `{}` skips the read, for rows that
    name no index). `reason` is why a structural answer is empty beyond `verdict`
    (verify's claim reason, impact's and callers' epistemic causes). `args` is the argument
    set that changes the answer (`answer_args`), stored as compact JSON of at most
    `ARGS_JSON_CAP` bytes; path-shaped values become counts on a restricted namespace.
    `error_code` / `error_text` mark a request that failed (the 422 path writes one through
    `schedule_code_trace_validation_failure`), the text bounded and redacted the same way
    as `agent_events.args.error`.

    `stage_ms` is the op's stage wall: whole milliseconds per stage, the same numbers the
    response carries as `retrieval_work.stage_ms_<stage>`, so "why was THIS call slow" is
    answered from the row; `{}` when the op keeps no wall.

    `verdict` names why a structural op's answer is empty (`no_indexed_paths` /
    `no_seeds_in_hunks` / `no_static_callers` are answers; `not_covered` / `no_match` are
    the abstains), '' when rows were returned. `consumer` falls back to the request's
    resolved consumer (the request ContextVar: the explicit tag, else the token's subject
    class) so a caller that omits the field is attributable in this ledger the way it is
    in `agent_events`.

    chunks_matched / path_prefix are what code.grep's chunk statement returned before the
    line filter and the scope it ran under: abstain=1 with chunks_matched=0 is the
    statement finding nothing INSIDE path_prefix ('' = the whole namespace), the shape a
    recount over the same scope can call wrong. Other ops leave both at their defaults.

    returned_via / lexical_ranks / vector_ranks are aligned with returned_ids: which leg
    found each served unit and where each leg ranked it.

    The row carries the OTel trace/span ids of the request it describes and the span
    carries `loom.code.trace_run_id`, so a HyperDX waterfall and a code_search_trace row
    resolve to each other from either side."""
    otel_trace_id, otel_span_id = current_otel_ids()
    set_code_span_attributes(trace_run_id=trace_run_id)
    if not settings.code_trace_log_enabled:
        return

    # A read of a restricted namespace stores the query's digest and no returned paths /
    # scope; ids, scores and counts stay so the ledger still measures the plane.
    redacted = 0
    from server.identity.app.namespace_classification import is_restricted_cached

    if is_restricted_cached(org, ns):
        from server.identity.app.restricted_access import digest_text

        query = digest_text(query)
        returned_paths = []
        path_prefix = ""
        redacted = 1

    row = {
        "org": org,
        "namespace": ns,
        "agent": agent or "-",
        "user_id": user_id or "-",
        "op": op,
        "query": query[:2000],
        "route": route,
        "returned_ids": returned_ids[:50],
        "returned_paths": returned_paths[:50],
        "returned_scores": [round(s, 4) for s in returned_scores[:50]],
        "top_k": top_k,
        "result_count": len(returned_ids),
        "tokens_returned": tokens_returned,
        "abstain": 1 if abstain else 0,
        "retrieval_risk": retrieval_risk,
        "embedding_model": embedding_model,
        "latency_ms": int((time.monotonic() - started_monotonic) * 1000),
        "trace_run_id": trace_run_id,
        "consumer": consumer or current_consumer(),
        "otel_trace_id": otel_trace_id,
        "otel_span_id": otel_span_id,
        "returned_via": [str(v)[:16] for v in (returned_via or [])[:50]],
        "lexical_ranks": [max(0, min(int(r), 65535)) for r in (lexical_ranks or [])[:50]],
        "vector_ranks": [max(0, min(int(r), 65535)) for r in (vector_ranks or [])[:50]],
        "chunks_matched": max(0, min(int(chunks_matched or 0), 4_294_967_295)),
        "path_prefix": str(path_prefix or "")[:1000],
        "verdict": str(verdict or "")[:32],
        "redacted": redacted,
        "stage_ms": {
            str(k)[:32]: max(0, min(int(v), 4_294_967_295)) for k, v in (stage_ms or {}).items()
        },
        "reason": str(reason or "")[:64],
        "args_json": bounded_args_json(args, redacted=bool(redacted)),
        "error_code": str(error_code or "")[:32],
        "error_text": bounded_error_text(error_text),
    }
    if as_of is not None:
        row.update(as_of_columns(as_of))

    async def _write() -> None:
        try:
            if "as_of_commit" not in row:
                row.update(as_of_columns(await _trace_as_of(org, ns)))
            from server.core.ch import get_client

            cols = TRACE_COLUMNS
            await get_client().client.insert(
                "loom.code_search_trace",
                [[row[c] for c in cols]],
                column_names=cols,
                settings={"async_insert": 1, "wait_for_async_insert": 0},
            )
        except Exception as e:  # noqa: BLE001 — telemetry must never fail a request
            logger.debug("code trace write failed: %s", e)

    loop = asyncio.get_running_loop()
    # The set holds only live tasks of the running loop. A task whose loop is
    # gone (a test's asyncio.run() closing with the write still pending, an
    # unawaited call in a sync test) can never complete or be discarded by its
    # callback, and a later gather of the set from a new loop raises "future
    # belongs to a different loop" (random-order unit suite).
    for stale in [t for t in _TRACE_TASKS if t.done() or t.get_loop() is not loop]:
        _TRACE_TASKS.discard(stale)
    task = create_deferred_task(_write())
    _TRACE_TASKS.add(task)
    task.add_done_callback(_TRACE_TASKS.discard)


_TRACE_COLS = [
    "org",
    "namespace",
    "agent",
    "user_id",
    "op",
    "query",
    "route",
    "returned_ids",
    "returned_paths",
    "returned_scores",
    "top_k",
    "result_count",
    "tokens_returned",
    "abstain",
    "retrieval_risk",
    "embedding_model",
    "latency_ms",
    "trace_run_id",
    "consumer",
    "otel_trace_id",
    "otel_span_id",
]
# What code.grep's chunk statement returned and the scope it ran under.
_TRACE_GREP_COLS = ["chunks_matched", "path_prefix"]
# Why a structural op's answer is empty.
_TRACE_VERDICT_COLS = ["verdict"]
# Per-hit provenance aligned with returned_ids.
_TRACE_VIA_COLS = ["returned_via", "lexical_ranks", "vector_ranks"]
# The row was written for a restricted namespace (query digested, paths
# and scope dropped).
_TRACE_RESTRICTED_COLS = ["redacted"]
# The op's stage wall, whole ms per stage.
_TRACE_STAGE_COLS = ["stage_ms"]
# The index commit the op answered from and its lag, the abstain reason,
# the answer-changing arguments, the error.
_TRACE_DECIDABLE_COLS = [
    "as_of_commit",
    "lag_commits",
    "reason",
    "args_json",
    "error_code",
    "error_text",
]
# Every column the insert sends, in order.
TRACE_COLUMNS: list[str] = (
    _TRACE_COLS
    + _TRACE_GREP_COLS
    + _TRACE_VERDICT_COLS
    + _TRACE_VIA_COLS
    + _TRACE_RESTRICTED_COLS
    + _TRACE_STAGE_COLS
    + _TRACE_DECIDABLE_COLS
)


# ---------------------------------------------------------------------------
# The columns that make a trace row decidable after the fact.
# ---------------------------------------------------------------------------

# `args_json` bound: the argument set is a handful of scalars and short
# lists; a body that does not fit is trimmed largest-value-first, never cut
# mid-JSON, so `JSONExtract*` on the column always parses.
ARGS_JSON_CAP = 1024
# List arguments the row stores as a count: their entries are ids or paths
# the answer already carries (returned_ids / returned_paths), not knobs.
_COUNTED_LIST_ARGS = frozenset({"exclude_unit_ids", "files", "exclude_authors", "namespaces"})
# Path-shaped arguments: dropped to their count on a restricted namespace (those rows
# store no paths).
_PATH_ARGS = frozenset({"path_globs", "exclude_globs", "files", "path_prefix", "track_path"})
_ARG_STR_CAP = 200
_ARG_LIST_CAP = 20


def answer_args(inp: Any, *names: str, **extra: Any) -> dict[str, Any]:
    """The argument set that changes an op's answer, read off its validated
    input: `names` are attributes to copy (None, '' and [] = unset, dropped;
    a list of ids/paths in `_COUNTED_LIST_ARGS` becomes `<name>_n`), `extra`
    are effective values the handler resolved (a default the request left
    to the server). Bounded per value; never raises."""
    out: dict[str, Any] = {}
    for name in names:
        try:
            v = getattr(inp, name, None) if not isinstance(inp, dict) else inp.get(name)
        except Exception:  # noqa: BLE001 — trace args are best-effort
            continue
        if v is None or v == "" or (isinstance(v, (list, tuple, set)) and not v):
            continue
        if isinstance(v, (list, tuple, set)):
            if name in _COUNTED_LIST_ARGS:
                out[f"{name}_n"] = len(v)
            else:
                out[name] = [str(x)[:_ARG_STR_CAP] for x in list(v)[:_ARG_LIST_CAP]]
        elif isinstance(v, (bool, int, float)):
            out[name] = v
        else:
            out[name] = str(v)[:_ARG_STR_CAP]
    for k, v in extra.items():
        if v is not None:
            out[k] = v
    return out


def bounded_args_json(args: dict[str, Any] | None, *, redacted: bool = False) -> str:
    """`args` as compact, key-sorted JSON of at most ARGS_JSON_CAP bytes:
    '' for none; path-shaped values reduced to counts when `redacted`; an
    oversized body drops its largest values first and says so with
    `"_trimmed": n`. Always valid JSON or ''; never raises."""
    if not args:
        return ""
    import json

    try:
        body = dict(args)
        if redacted:
            for k in [k for k in body if k in _PATH_ARGS]:
                v = body.pop(k)
                body[f"{k}_n"] = len(v) if isinstance(v, (list, tuple)) else (1 if v else 0)
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        trimmed = 0
        while len(text.encode("utf-8")) > ARGS_JSON_CAP and body:
            biggest = max(body, key=lambda k: len(json.dumps(body[k], default=str)))
            body.pop(biggest)
            trimmed += 1
            body["_trimmed"] = trimmed
            text = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        return text if len(text.encode("utf-8")) <= ARGS_JSON_CAP else ""
    except Exception:  # noqa: BLE001 — a ledger column never fails a request
        return ""


def bounded_error_text(text: str) -> str:
    """The trace row's `error_text`: first line, quoted spans redacted, at most 200
    characters, the same bound as `agent_events.args.error`, so the two ledgers agree."""
    if not text:
        return ""
    from server.observability.event_args import bounded_error

    return bounded_error(str(text))[1]


def as_of_columns(block: dict[str, Any] | None) -> dict[str, Any]:
    """`{as_of_commit, lag_commits}` from an `index_as_of` block; the unknown
    row ('' / -1) when the block is empty or malformed."""
    try:
        commit = str((block or {}).get("commit") or "")[:64]
        lag = int((block or {}).get("lag_commits", -1))
    except Exception:  # noqa: BLE001 — the unknown row
        commit, lag = "", -1
    return {"as_of_commit": commit, "lag_commits": max(-1, min(lag, 2_147_483_647))}


async def _trace_as_of(org: str, ns: str) -> dict[str, Any]:
    """The index's as_of block for a trace row, from the 30 s per-namespace
    cache (`index_as_of`); one point read per namespace per 30 s at most,
    inside the write task. Fail-open to the unknown block."""
    try:
        from server.code.store.index import CodeRepo

        return await index_as_of(CodeRepo(), org, ns)
    except Exception:  # noqa: BLE001 — the unknown block
        return {}


# Body keys a 422 row copies into args_json: the knobs the validation could
# have refused, as the caller sent them (bounded by answer_args).
_VALIDATION_ARG_KEYS = (
    "mode",
    "regex",
    "case_sensitive",
    "path_globs",
    "exclude_globs",
    "langs",
    "exclude_unit_ids",
    "include_ambiguous",
    "depth",
    "top_k",
    "limit",
    "max_nodes",
    "kind",
    "per_hop_limit",
    "offset",
    "files",
    "summary_only",
    "path_prefix",
)


def schedule_code_trace_validation_failure(
    op_name: str, raw_body: dict[str, Any], errors: list[dict[str, Any]]
) -> None:
    """The 422 path's trace row: a code.* request pydantic refused leaves a row with
    `error_code = VALIDATION_FAILED`, the failing field in `error_text`, and the knobs it
    sent in `args_json`, so a request retried unchanged is diagnosable from the ledger.
    The query is stored as its digest (the request was never authorized). Never raises."""
    if not str(op_name).startswith("code.") or not isinstance(raw_body, dict):
        return
    try:
        from server.identity.app.restricted_access import digest_text
        from server.observability.event_args import validation_error_text

        query = str(
            raw_body.get("query")
            or raw_body.get("pattern")
            or raw_body.get("name")
            or raw_body.get("symbol_id")
            or raw_body.get("subject")
            or ""
        )
        diff = raw_body.get("diff")
        extra = {"diff_bytes": len(diff)} if isinstance(diff, str) and diff else {}
        schedule_code_trace(
            org=str(raw_body.get("org") or "-")[:200],
            ns=str(raw_body.get("namespace") or "-")[:200],
            op=str(op_name),
            query=digest_text(query) if query else "",
            route="validation_failed",
            returned_ids=[],
            returned_paths=[],
            returned_scores=[],
            top_k=0,
            tokens_returned=0,
            abstain=True,
            retrieval_risk="error",
            embedding_model="",
            started_monotonic=time.monotonic(),
            trace_run_id=new_trace_run_id(),
            consumer=str(raw_body.get("consumer") or ""),
            agent=str(raw_body.get("agent") or "-"),
            user_id=str(raw_body.get("user_id") or "-"),
            args=answer_args(raw_body, *_VALIDATION_ARG_KEYS, **extra),
            error_code="VALIDATION_FAILED",
            error_text=validation_error_text(errors),
            as_of={},
        )
    except Exception:
        logger.debug("code trace 422 row not written", exc_info=True)


class UnitProvenance(TypedDict):
    returned_via: list[str]
    lexical_ranks: list[int]
    vector_ranks: list[int]


def unit_provenance(units: list[Any]) -> UnitProvenance:
    """The three aligned provenance arrays for a served unit list — typed so
    `**unit_provenance(...)` binds exactly these keywords of
    `schedule_code_trace`."""
    return {
        "returned_via": [str(getattr(u, "via", "") or "") for u in units],
        "lexical_ranks": [int(getattr(u, "lexical_rank", 0) or 0) for u in units],
        "vector_ranks": [int(getattr(u, "vector_rank", 0) or 0) for u in units],
    }


# ---------------------------------------------------------------------------
# Shunt counters. The plugin's loom-code-shunt hook reports its decisions via
# code.outcome (actions shunt_hit / shunt_pass, tokens_saved); the table row
# is the durable counter, and these OTel counters put the same numbers next
# to latency in the metrics store. Instruments are created lazily on the
# global MeterProvider otel.py installs (a no-op meter when metrics are off)
# so this stays out of metrics.py's init path. Attributes: decision only.
# ---------------------------------------------------------------------------

_SHUNT_COUNTER: Any = None
_SHUNT_TOKENS_COUNTER: Any = None


def record_code_shunt(
    *,
    decision: str,
    count: int,
    tokens_saved: int,
    namespace: str,
    reason: str = "",
) -> None:
    """Add to `loom.code.shunt` (attrs decision=hit|pass, reason = the pass reason from
    the fixed vocabulary or '') and `loom.code.shunt_tokens_saved` (hit: kept out; pass:
    let in). Never raises."""
    global _SHUNT_COUNTER, _SHUNT_TOKENS_COUNTER
    try:
        from opentelemetry import metrics as _metrics

        if _SHUNT_COUNTER is None:
            meter = _metrics.get_meter("loom.code")
            _SHUNT_COUNTER = meter.create_counter(
                name="loom.code.shunt",
                description="loom-code-shunt hook decisions, by decision (hit|pass)",
                unit="1",
            )
            _SHUNT_TOKENS_COUNTER = meter.create_counter(
                name="loom.code.shunt_tokens_saved",
                description="Estimated tokens the shunt kept out of the model's context",
                unit="1",
            )
        attrs = {
            "decision": decision if decision in ("hit", "pass") else "other",
            "reason": (reason or "")[:32],  # closed set: SHUNT_PASS_REASONS | unattributed | ''
            "loom.namespace": (namespace or "-")[:64],
        }
        _SHUNT_COUNTER.add(max(1, int(count)), attributes=attrs)
        if tokens_saved > 0:
            _SHUNT_TOKENS_COUNTER.add(int(tokens_saved), attributes=attrs)
    except Exception:  # noqa: BLE001 — metrics never fail an op
        pass


def reset_shunt_counters_for_tests() -> None:
    global _SHUNT_COUNTER, _SHUNT_TOKENS_COUNTER, _OUTCOME_COUNTER
    _SHUNT_COUNTER = _SHUNT_TOKENS_COUNTER = _OUTCOME_COUNTER = None


_OUTCOME_COUNTER: Any = None


def record_code_outcome(*, action: str, reason: str, namespace: str) -> None:
    """Add to `loom.code.outcome` (attrs action = the retrieval outcome, reason = the
    fell_back_grep / ignored reason from OUTCOME_REASONS or ''). The table row stays the
    durable counter. Never raises."""
    global _OUTCOME_COUNTER
    try:
        from opentelemetry import metrics as _metrics

        if _OUTCOME_COUNTER is None:
            _OUTCOME_COUNTER = _metrics.get_meter("loom.code").create_counter(
                name="loom.code.outcome",
                description="code.outcome retrieval outcomes, by action and reason",
                unit="1",
            )
        _OUTCOME_COUNTER.add(
            1,
            attributes={
                "action": (action or "")[:32],
                "reason": (reason or "")[:32],  # closed set: OUTCOME_REASONS | ''
                "loom.namespace": (namespace or "-")[:64],
            },
        )
    except Exception:  # noqa: BLE001 — metrics never fail an op
        pass


_ASK_CACHE_COUNTERS: dict[str, Any] = {}


def cache_outcome_label(cache: str) -> str:
    """The bounded label of a code.ask cache outcome: `reused_from:<ns>`
    counts as `reused` (the namespace rides its own attribute)."""
    return "reused" if (cache or "").startswith("reused_from:") else (cache or "miss")[:16]


def record_code_ask_cache(
    *, cache: str, namespace: str, saved_ms: int = 0, saved_usd: float = 0.0
) -> None:
    """Add to `loom.code.ask_cache` (attr outcome = hit | miss | stale | guarded |
    key_mismatch | negative | reused | bypassed) and, on a served entry, to
    `loom.code.ask_cache_saved_ms` / `loom.code.ask_cache_saved_usd`: the stored synthesis
    wall and cost the hit did not pay. Never raises."""
    try:
        from opentelemetry import metrics as _metrics

        if not _ASK_CACHE_COUNTERS:
            meter = _metrics.get_meter("loom.code")
            _ASK_CACHE_COUNTERS["outcome"] = meter.create_counter(
                name="loom.code.ask_cache",
                description="code.ask answer-cache outcomes, by outcome",
                unit="1",
            )
            _ASK_CACHE_COUNTERS["ms"] = meter.create_counter(
                name="loom.code.ask_cache_saved_ms",
                description="synthesis wall a served code.ask cache entry saved",
                unit="ms",
            )
            _ASK_CACHE_COUNTERS["usd"] = meter.create_counter(
                name="loom.code.ask_cache_saved_usd",
                description="synthesis cost a served code.ask cache entry saved",
                unit="USD",
            )
        attrs = {
            "outcome": cache_outcome_label(cache),
            "loom.namespace": (namespace or "-")[:64],
        }
        _ASK_CACHE_COUNTERS["outcome"].add(1, attributes=attrs)
        if saved_ms > 0:
            _ASK_CACHE_COUNTERS["ms"].add(int(saved_ms), attributes=attrs)
        if saved_usd > 0:
            _ASK_CACHE_COUNTERS["usd"].add(float(saved_usd), attributes=attrs)
    except Exception:  # noqa: BLE001 — metrics never fail an op
        pass
