"""Precomputed per-unit risk: loom.code_unit_risk.

The risk block on a served code unit — churn, amp, tested, callees
(code/app/risk.py) — is five keyed reads in two rounds, and overlapping them with
the search has a ceiling: the symbol read is a bloom-filter skip-index scan of
code_symbols under FINAL at 43–77 ms whatever it returns, so the annotation keeps ~one
round trip on the critical path. This module is the table that removes it:

- `read_rows` — the search's one keyed statement for the ranked pool's head
  (unit ids on the ORDER BY), returning only rows fresher than
  `code_unit_risk_max_age_s`; a failed read is an empty dict (the live
  reads take over; nothing here fails a search).
- `build_rows` — the rows for the units of a set of paths, computed with
  the SAME staging function the live annotation uses, from the same
  statements read per file. A row's semantics: the annotation of the
  unit's file served alone — the live statements' flat limits (30 test
  rows, 1,000 caller rows, 500 callee rows) bind per file here, where the
  served-set read shares them across the served files; the callers read
  caps per target symbol so a hub in the same file cannot starve its
  siblings. Rows are written for every live chunk of the path and for its
  function symbols (a symbol-shaped search serves symbol units, id =
  symbol_id).
- `refresh_for_commit` — the ingest's write: the commit's changed files
  and the files their calls reach (a new or removed call moves the callee's
  amp), capped, built in the weekly pass's rounds (the reached set is a
  path-only read, and a set past one round keeps its symbols);
  `refresh_namespace` — the weekly pass's write, with churn
  and the tested set read once for the namespace. It is resumable and
  budgeted: one statement reads the newest stamp per path
  already in the table, only paths with no row or a row that would go
  stale before the next weekly pass are built (`rewrite_threshold_s`), and
  a monotonic `deadline` checked between rounds stops the leg cleanly with
  the resume point named — a pass cut short continues where it stopped.
  The two edge reads run once per round over the round's symbol ids, not
  once per file (per file, a large namespace's pass would issue three
  statements for every file).

An edited unit is a new key (chunk ids are content-addressed), so it
misses the table until its ingest writes the row: freshness for the unit's
own edit is one ingest cycle by construction; the max-age bound covers
the facts that move without the unit (churn with the PR backfill, callers
with other files' edits), which the weekly pass refreshes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from server.core.datetimes import naive_utc_now as _now
from server.settings import settings

if TYPE_CHECKING:
    from server.code.domain.units import CodeUnit
    from server.code.store.index import CodeRepo

logger = logging.getLogger("loom.code_unit_risk")

TABLE = "code_unit_risk"
# paths a commit's refresh may reach through its calls (changed files always run)
COMMIT_CALLEE_PATHS_MAX = 200
# the weekly builder's round: paths built, staged and inserted together; the chunk-span
# read takes the round in one statement
BUILD_PATHS_PER_ROUND = 200
# paths per symbol statement in the builder (the live read keeps 20: a skip-index scan
# costs the same whatever the IN list holds)
SYMBOL_PATHS_PER_STATEMENT = 100
INSERT_BATCH = 5000
# function symbols read per file when building (the served-set read cuts
# at max(400, 300 x files); a large source file can hold a few thousand)
SYMBOLS_PER_PATH_BUILD = 5000


@dataclass(frozen=True)
class RiskRow:
    churn: int
    amp: int
    tested: bool
    callees: list[dict[str, Any]]
    computed_at: datetime | None

    def as_staged(self, unit: CodeUnit) -> tuple[CodeUnit, int, int, bool, list[dict[str, Any]]]:
        return (unit, self.churn, self.amp, self.tested, list(self.callees))


def callees_json(callees: list[dict[str, Any]]) -> str:
    """Canonical JSON (sorted keys, no whitespace): two builds of the same
    facts serialise byte-identical."""
    return json.dumps(
        [{"name": str(c["name"]), "path": str(c["path"]), "line": int(c["line"])} for c in callees],
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_callees(text: Any) -> list[dict[str, Any]]:
    try:
        rows = json.loads(text) if isinstance(text, str) else list(text or [])
    except (TypeError, ValueError):
        return []
    out: list[dict[str, Any]] = []
    for c in rows if isinstance(rows, list) else []:
        if isinstance(c, dict) and "name" in c:
            # the live attach's key order: name, path, line
            out.append(
                {
                    "name": str(c.get("name") or ""),
                    "path": str(c.get("path") or ""),
                    "line": int(c.get("line") or 0),
                }
            )
    return out


def _as_datetime(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is not None else v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", ""))
        except ValueError:
            return None
    return None


def is_fresh(computed_at: datetime | None, *, now: datetime | None = None) -> bool:
    if computed_at is None:
        return False
    return (now or _now()) - computed_at <= _max_age()


def _max_age() -> timedelta:
    return timedelta(seconds=max(0, int(settings.code_unit_risk_max_age_s)))


def row_from(raw: dict[str, Any]) -> RiskRow:
    return RiskRow(
        churn=int(raw.get("churn") or 0),
        amp=int(raw.get("amp") or 0),
        tested=bool(int(raw.get("tested") or 0))
        if not isinstance(raw.get("tested"), bool)
        else bool(raw["tested"]),
        callees=parse_callees(raw.get("callees")),
        computed_at=_as_datetime(raw.get("computed_at")),
    )


# ---- the read -----------------------------------------------------------------


async def read_rows(repo: CodeRepo, org: str, ns: str, unit_ids: list[str]) -> dict[str, RiskRow]:
    """Fresh precomputed rows for `unit_ids` (missing and stale ids absent).
    Never raises: a failed read is an empty dict and the live reads serve."""
    ids = [u for u in dict.fromkeys(unit_ids) if u]
    if not ids:
        return {}
    try:
        raw = await repo.unit_risk_rows(org, ns, ids)
    except Exception:  # risk rows are advisory: none on a failed read
        logger.warning("code_unit_risk: table read failed", exc_info=True)
        return {}
    now = _now()
    out: dict[str, RiskRow] = {}
    for uid, r in raw.items():
        row = row_from(r)
        if is_fresh(row.computed_at, now=now):
            out[uid] = row
    return out


# ---- the builder ------------------------------------------------------------------


def _units_of_path(
    spans: list[dict[str, Any]], symbol_rows: list[dict[str, Any]]
) -> list[CodeUnit]:
    """The units a file's rows are computed for: every live chunk (id =
    chunk_id, matched to symbols by line overlap like the live read) and
    every function symbol of the file (id = symbol_id, kind "symbol")."""
    from server.code.domain.units import CodeUnit

    units: list[CodeUnit] = []
    seen: set[str] = set()
    for s in spans:
        uid = str(s.get("chunk_id") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        units.append(
            CodeUnit(
                unit_id=uid,
                path=str(s["path"]),
                start_line=int(s.get("start_line") or 0),
                end_line=int(s.get("end_line") or 0),
                kind=str(s.get("kind") or ""),
            )
        )
    for s in symbol_rows:
        uid = str(s.get("symbol_id") or "")
        if not uid or uid in seen:
            continue
        seen.add(uid)
        units.append(
            CodeUnit(
                unit_id=uid,
                path=str(s["path"]),
                start_line=int(s.get("start_line") or 0),
                end_line=int(s.get("end_line") or 0),
                kind="symbol",
                symbol=str(s.get("qualified_name") or ""),
            )
        )
    return units


async def _edge_facts(
    repo: CodeRepo, org: str, ns: str, symbol_ids: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The unit round's two edge reads for a set of symbols — a whole round's, not per
    file. Per-target capped (`LIMIT n BY <match column>`, ordered first), so a target's
    rows are the same whichever other ids share the statement and the flat `limit`
    (per_match x ids) can never cut a row; 500 ids per statement, the read's bound."""
    from server.code.app import risk as code_risk
    from server.code.store.index.sql import _batched

    callers: list[dict[str, Any]] = []
    callees: list[dict[str, Any]] = []
    for batch in _batched(symbol_ids, 500):
        a, b = await asyncio.gather(
            repo.edges_for_symbols(
                org,
                ns,
                batch,
                direction="callers",
                edge_kinds=code_risk.CALLER_KINDS,
                limit=code_risk.CALLERS_LIMIT * len(batch),
                per_match=code_risk.CALLERS_LIMIT,
            ),
            repo.edges_for_symbols(
                org,
                ns,
                batch,
                direction="callees",
                edge_kinds=["calls"],
                limit=code_risk.CALLEES_PER_SOURCE_READ * len(batch),
                per_match=code_risk.CALLEES_PER_SOURCE_READ,
            ),
        )
        callers += a
        callees += b
    return callers, callees


def _edges_by_path(
    symbol_rows: list[dict[str, Any]],
    caller_raw: list[dict[str, Any]],
    callee_raw: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Route a round's edge rows to the file(s) whose symbols they belong to
    — callers by `to_symbol_id`, callees by `from_symbol_id` — so each file
    is staged from exactly the rows its own per-file read would have
    returned (a symbol id defined in two files reaches both). Pure."""
    owners: dict[str, list[str]] = {}
    for srow in symbol_rows:
        sid, path = str(srow.get("symbol_id") or ""), str(srow.get("path") or "")
        if sid and path and path not in owners.setdefault(sid, []):
            owners[sid].append(path)
    callers: dict[str, list[dict[str, Any]]] = {}
    callees: dict[str, list[dict[str, Any]]] = {}
    for r in caller_raw:
        for path in owners.get(str(r.get("to_symbol_id") or ""), ()):
            callers.setdefault(path, []).append(r)
    for r in callee_raw:
        for path in owners.get(str(r.get("from_symbol_id") or ""), ()):
            callees.setdefault(path, []).append(r)
    return callers, callees


def _rows_for_path(
    org: str,
    ns: str,
    path: str,
    *,
    spans: list[dict[str, Any]],
    symbol_rows: list[dict[str, Any]],
    churn: int,
    tested: bool,
    stamp: datetime,
    version: int,
    caller_raw: list[dict[str, Any]],
    callee_raw: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One file's rows from facts already read: the live staging function
    over the file's units, its symbols and the edge rows routed to it."""
    from server.code.app import risk as code_risk
    from server.code.store.index import build_row

    units = _units_of_path(spans, symbol_rows)
    if not units:
        return []
    staged = code_risk.stage_units(
        units,
        churn={path: churn},
        symbol_rows=symbol_rows,
        tested_paths={path} if tested else set(),
        caller_raw=caller_raw,
        callee_raw=callee_raw,
    )
    return [
        build_row(
            TABLE,
            org=org,
            namespace=ns,
            unit_id=u.unit_id,
            path=path,
            churn=int(ch),
            amp=int(amp),
            tested=1 if ts else 0,
            callees=callees_json(hop),
            computed_at=stamp,
            version=version,
        )
        for u, ch, amp, ts, hop in staged
    ]


async def build_rows(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    paths: list[str],
    *,
    churn: dict[str, int] | None = None,
    tested: set[str] | None = None,
    computed_at: datetime | None = None,
    version: int | None = None,
) -> list[dict[str, Any]]:
    """code_unit_risk rows for every live unit of `paths`. `churn` / `tested` are the
    namespace-wide facts when the caller read them once (the weekly pass); otherwise
    both are read keyed for `paths`. For the whole set: the chunk spans and the function
    symbols (every one of the file, not the served-set read's 400-row cut: a hub
    header's smaller methods keep their callers), then the two edge reads over all of
    the set's symbol ids (per round, not per file), then the live staging function per
    file over the rows routed to it."""
    from server.code.app import risk as code_risk
    from server.code.store.index import version_now

    paths = [p for p in dict.fromkeys(paths) if p]
    if not paths:
        return []
    stamp = computed_at or _now()
    ver = version or version_now()
    spans_task = repo.chunk_units_for_paths(
        org, ns, model, paths, per_statement=BUILD_PATHS_PER_ROUND
    )
    churn_task = _churn_for(repo, org, ns, paths) if churn is None else _ready(dict(churn))
    tested_task = repo.tested_paths(org, ns, paths) if tested is None else _ready(set(tested))
    symbols_task = repo.functions_for_paths(
        org,
        ns,
        paths,
        limit=SYMBOLS_PER_PATH_BUILD * len(paths),
        per_statement=SYMBOL_PATHS_PER_STATEMENT,
    )
    spans, churn_by_path, tested_set, symbol_rows = await asyncio.gather(
        spans_task, churn_task, tested_task, symbols_task
    )
    spans_by_path: dict[str, list[dict[str, Any]]] = {}
    for sp in spans:
        spans_by_path.setdefault(str(sp["path"]), []).append(sp)
    syms_by_path: dict[str, list[dict[str, Any]]] = {}
    for srow in symbol_rows:
        syms_by_path.setdefault(str(srow.get("path") or ""), []).append(srow)

    ids: list[str] = []
    for path in paths:
        units = _units_of_path(spans_by_path.get(path, []), syms_by_path.get(path, []))
        for syms in code_risk.symbols_of(units, syms_by_path.get(path, [])):
            ids.extend(syms)
    ids = list(dict.fromkeys(ids))
    caller_raw, callee_raw = await _edge_facts(repo, org, ns, ids) if ids else ([], [])
    callers_by_path, callees_by_path = _edges_by_path(symbol_rows, caller_raw, callee_raw)

    out: list[dict[str, Any]] = []
    for path in paths:
        out += _rows_for_path(
            org,
            ns,
            path,
            spans=spans_by_path.get(path, []),
            symbol_rows=syms_by_path.get(path, []),
            churn=int(churn_by_path.get(path, 0)),
            tested=path in tested_set,
            stamp=stamp,
            version=ver,
            caller_raw=callers_by_path.get(path, []),
            callee_raw=callees_by_path.get(path, []),
        )
    return out


async def _ready(value: Any) -> Any:
    return value


async def _churn_for(repo: CodeRepo, org: str, ns: str, paths: list[str]) -> dict[str, int]:
    from server.code.app import risk as code_risk
    from server.code.store.index.sql import _batched

    out: dict[str, int] = {}
    for batch in _batched(paths, 50):
        out.update(await repo.pr_churn_for(org, ns, batch, days=code_risk.CHURN_WINDOW_DAYS))
    return out


# ---- the writes -------------------------------------------------------------------


async def _insert(repo: CodeRepo, rows: list[dict[str, Any]]) -> int:
    for start in range(0, len(rows), INSERT_BATCH):
        await repo.insert_rows(TABLE, rows[start : start + INSERT_BATCH])
    return len(rows)


async def commit_paths(
    repo: CodeRepo,
    org: str,
    ns: str,
    changed_paths: list[str],
    callee_symbol_ids: list[str],
    *,
    max_callee_paths: int = COMMIT_CALLEE_PATHS_MAX,
) -> list[str]:
    """The files a commit's refresh covers: its changed files, then the files defining
    the symbols its files call (their amp moved), sorted and capped — the weekly pass
    reaches the rest. The reached set is one path-only read per 500 ids
    (`symbol_paths_by_ids`; a `SELECT *` there would be 40% of the leg's bytes), and
    names every file defining an id, not the one row the two-tier collapse kept."""
    paths = [p for p in dict.fromkeys(changed_paths) if p]
    changed = set(paths)
    ids = [s for s in dict.fromkeys(callee_symbol_ids) if s]
    reached = (await repo.symbol_paths_by_ids(org, ns, ids)) if ids else set()
    return paths + sorted(reached - changed)[:max_callee_paths]


async def refresh_for_commit(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    changed_paths: list[str],
    callee_symbol_ids: list[str],
) -> int:
    """Rows for a commit's files and the files its calls reach; returns the rows
    written. Raises to the caller (the ingest records, never fails the commit — its
    index rows are durable already). The paths go through the weekly pass's rounds
    (`BUILD_PATHS_PER_ROUND`): a commit's set is one round almost always (changed files
    + up to 200 reached), and a larger one — a 500-file commit — is built 200 a round
    with round-wide statements, so the symbol read covers every path; every row of the
    commit carries one version and stamp."""
    if not changed_paths:
        return 0
    from server.code.store.index import version_now

    paths = await commit_paths(repo, org, ns, changed_paths, callee_symbol_ids)
    stamp, ver = _now(), version_now()
    written = 0
    for start in range(0, len(paths), BUILD_PATHS_PER_ROUND):
        rows = await build_rows(
            repo,
            org,
            ns,
            model,
            paths[start : start + BUILD_PATHS_PER_ROUND],
            computed_at=stamp,
            version=ver,
        )
        written += await _insert(repo, rows)
    return written


def rewrite_threshold_s(max_age_s: int, interval_s: int) -> int:
    """A weekly pass rewrites a row when it is older than (max age - interval), so no
    row the pass leaves alone can be stale before the next pass: with a 10-day max age
    and a 7-day interval a row younger than 3 days survives to the next pass fresh; a
    row of 3-7 days would not, and is rewritten now. Rows younger than the threshold are
    the ingest's per-commit refreshes and a resumed pass's own rows. A max age at or
    under the interval (misconfigured) rewrites everything."""
    return max(0, int(max_age_s) - int(interval_s))


def paths_to_build(
    paths: list[str],
    stamps: dict[str, datetime],
    *,
    threshold_s: int,
    now: datetime,
) -> list[str]:
    """Pure: the live paths with no precomputed row or a newest row older
    than `threshold_s` — what a pass (or a resumed pass) still has to write,
    in the same sorted order every time so the resume point is stable."""
    cut = now - timedelta(seconds=max(0, int(threshold_s)))
    out: list[str] = []
    for p in paths:
        at = stamps.get(p)
        if at is None or at < cut:
            out.append(p)
    return out


@dataclass
class NamespaceRefresh:
    """What one weekly refresh did for a namespace."""

    paths_live: int = 0
    paths_due: int = 0
    paths_built: int = 0
    rows_written: int = 0
    partial: bool = False  # the budget cut the leg before every due path was built
    resume_at: str = ""  # the first path the next pass builds, when partial
    skipped: list[str] = field(default_factory=list)


async def refresh_namespace(
    repo: CodeRepo,
    org: str,
    ns: str,
    model: str,
    *,
    deadline: float | None = None,
    threshold_s: int | None = None,
) -> NamespaceRefresh:
    """The weekly pass's write: the live files' units, churn and the tested set read
    once for the namespace, files built and inserted in rounds so memory stays bounded.
    Every row of a pass carries one version and computed_at. One statement reads the
    newest stamp per path already written, and only paths without a row — or with one
    older than `threshold_s` (default `rewrite_threshold_s` of the settings) — are
    built, sorted, so a pass cut short resumes where it stopped; `deadline`
    (time.monotonic) is checked between rounds, and when it has passed the leg returns
    `partial` with the resume point named instead of raising."""
    from server.code.app import risk as code_risk
    from server.code.store.index import version_now

    out = NamespaceRefresh()
    paths = sorted(await repo.live_file_hashes(org, ns))
    out.paths_live = len(paths)
    if not paths:
        return out
    if threshold_s is None:
        threshold_s = rewrite_threshold_s(
            int(settings.code_unit_risk_max_age_s), int(settings.code_graph_stats_interval_seconds)
        )
    stamps = await repo.unit_risk_path_stamps(org, ns)
    due = paths_to_build(paths, stamps, threshold_s=threshold_s, now=_now())
    out.paths_due = len(due)
    if not due:
        return out
    churn = {
        p: n
        for p, n in await repo.pr_churn_ranked(
            org, ns, days=code_risk.CHURN_WINDOW_DAYS, limit=1_000_000
        )
    }
    tested = await repo.tested_paths(org, ns)
    stamp, ver = _now(), version_now()
    for start in range(0, len(due), BUILD_PATHS_PER_ROUND):
        if deadline is not None and time.monotonic() >= deadline:
            out.partial, out.resume_at = True, due[start]
            logger.warning(
                "code_unit_risk: %s/%s budget reached after %d of %d due paths "
                "(%d live); the next pass resumes at %s",
                org,
                ns,
                out.paths_built,
                len(due),
                len(paths),
                due[start],
            )
            break
        batch = due[start : start + BUILD_PATHS_PER_ROUND]
        rows = await build_rows(
            repo,
            org,
            ns,
            model,
            batch,
            churn=churn,
            tested=tested,
            computed_at=stamp,
            version=ver,
        )
        out.rows_written += await _insert(repo, rows)
        out.paths_built += len(batch)
    return out
