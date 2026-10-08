"""The report-time fusion stage of `code.bisect_candidates`.

The diff scan's candidates, fused with the window's vector, lexical, path,
subtree and recency ranks, and the suspects the report names
(`server/tracker/domain/bisect_fusion.py` holds the pure parts). Every read
fails open: an error leaves the scan's candidates as they were.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from server.core.background import create_deferred_task
from server.settings import settings
from server.tracker.domain import bisect as bi
from server.tracker.domain import bisect_fusion as fu

logger = logging.getLogger(__name__)

_POOL = 60  # fused candidates kept for the answer's ranking (the caller's limit ≤ 25)
_VECTOR_K = 200


async def _embed(text: str) -> list[float]:
    from server.embedding.embedder import get_embedder

    try:
        return list(await get_embedder().embed(text[:4000]))
    except Exception as e:  # noqa: BLE001 — no vector leg, the others still rank
        logger.info("bisect fusion: embed failed: %s", e)
        return []


async def _vector_order(
    org: str, tracker_ns: str, vector: list[float], numbers: list[int]
) -> list[int]:
    if not vector or not numbers:
        return []
    from server.tracker.app.source import nearest_items

    try:
        hits = await nearest_items(
            org,
            tracker_ns,
            vector=vector,
            dim=len(vector),
            limit=_VECTOR_K,
            kinds=["pr"],
            numbers=numbers,
        )
    except Exception as e:  # noqa: BLE001 — the vector leg is optional
        logger.info("bisect fusion: vector read failed: %s", e)
        return []
    return [int(h["number"]) for h in hits]


def _window_bounds(
    versions: list[str], before: datetime | None, report_at: datetime | None
) -> tuple[datetime | None, datetime | None] | None:
    """(before, after) of the window read, or None when nothing bounds it: the
    releases with the report as the upper wall, else the merge-time window."""
    wall = report_at or before
    if versions:
        return wall, None
    if wall is None:
        return None
    days = int(settings.code_bisect_window_days)
    return wall, wall - timedelta(days=days)


def _candidate(row: dict[str, Any], report_paths: set[str]) -> bi.Candidate:
    merged = row.get("closed_at")
    return bi.Candidate(
        pr_number=int(row.get("pr_number") or 0),
        title=str(row.get("title") or ""),
        author=str(row.get("author") or ""),
        merged_at=merged.isoformat() if isinstance(merged, datetime) else str(merged or ""),
        minor_version=str(row.get("minor_version") or ""),
        matched_paths=[f for f in row.get("affected_file_paths") or [] if f in report_paths][:10],
        url=str(row.get("url") or ""),
    )


async def _read_window(
    source: Any,
    org: str,
    tracker_ns: str,
    *,
    versions: list[str],
    bounds: tuple[datetime | None, datetime | None],
    text: str,
) -> tuple[dict[int, dict[str, Any]], list[int], str]:
    """(rows by number, vector order, status): the window's PRs and the
    report's nearest among them. Status "" when read."""
    wall, floor = bounds
    try:
        rows, vector = await asyncio.gather(
            source.window_prs(org, tracker_ns, versions, before=wall, after=floor),
            _embed(text) if text.strip() else asyncio.sleep(0, result=[]),
        )
    except Exception as e:  # noqa: BLE001 — the scan's candidates stand
        logger.info("bisect fusion: window read failed: %s", e)
        return {}, [], "window_unavailable"
    by_number = {int(r["pr_number"]): r for r in rows if r.get("pr_number")}
    if not by_number:
        return {}, [], "empty_window"
    return by_number, await _vector_order(org, tracker_ns, vector, list(by_number)), ""


def start_prefetch(
    source: Any,
    org: str,
    tracker_ns: str,
    *,
    versions: list[str],
    before: datetime | None,
    report_at: datetime | None,
    text: str,
) -> asyncio.Task[tuple[dict[int, dict[str, Any]], list[int], str]] | None:
    """The fusion's reads as a task that runs beside the diff scan (they need
    only the window, not the scan's answer); None when there is no source or
    nothing bounds the window."""
    if source is None:
        return None
    bounds = _window_bounds(versions, before, report_at)
    if bounds is None:
        return None
    return create_deferred_task(
        _read_window(source, org, tracker_ns, versions=versions, bounds=bounds, text=text)
    )


async def fusion_stage(
    prefetch: asyncio.Task[tuple[dict[int, dict[str, Any]], list[int], str]] | None,
    pool: list[bi.Candidate],
    *,
    report_at: datetime | None,
    before: datetime | None,
    text: str,
    paths: list[str],
    issue_number: int = 0,
) -> tuple[list[bi.Candidate], dict[str, Any], dict[int, dict[str, Any]]]:
    """(pool, summary, window rows). Each signal ranks the window's PRs;
    reciprocal-rank fusion orders the union with the scan's own candidates.
    Empty summary when the stage did not run."""
    if prefetch is None:
        return pool, {}, {}
    by_number, vector_order, status = await prefetch
    if status:
        return pool, {"status": status}, {}
    c, m = fu.report_suspects(text, own_number=int(issue_number or 0))
    cued = [n for n in c if n in by_number]
    mentioned = [n for n in m if n in by_number]
    signals = fu.window_signals(
        list(by_number.values()),
        text=text,
        paths=paths,
        vector_order=vector_order,
        scan_order=[c.pr_number for c in pool],
        before=report_at or before,
    )
    order, why = fu.fuse(signals, mentioned=mentioned, cued=cued, limit=_POOL)
    known = {c.pr_number: c for c in pool}
    report_paths = {str(p) for p in paths if p}
    fused: list[bi.Candidate] = []
    for i, n in enumerate(order):
        cand = known.get(n) or (_candidate(by_number[n], report_paths) if n in by_number else None)
        if cand is None:
            continue
        cand.why = why.get(n, [])
        cand.score = round(1.0 - i / max(1, len(order)), 3)
        fused.append(cand)
    summary = {
        "window_prs": len(by_number),
        "signals": {k: len(v) for k, v in signals.items()},
        "suspects": {"named": cued, "mentioned": mentioned[:10]},
        **_weakness(fused, cued=cued, signals=signals, mentioned=mentioned),
    }
    return fused or pool, summary, by_number


def _weakness(
    fused: list[bi.Candidate],
    *,
    cued: list[int],
    signals: dict[str, list[int]],
    mentioned: list[int],
) -> dict[str, Any]:
    """Whether the fused top needs the second reading."""
    if not fused:
        return {}
    margin = fu.top_margin(signals, mentioned)
    weak = fu.is_weak(margin=margin, top_cued=fused[0].pr_number in cued)
    return {"margin": round(margin, 3), "weak": weak}


_RERANK_SYSTEM = (
    "You help bisect a ClickHouse regression. Given a bug report and numbered candidate pull "
    "requests merged before it (title, first release, changed source files, summary), rank the "
    "candidates by how likely each INTRODUCED the bug. Reason from the mechanism: which change "
    "could produce this exact symptom in the code it touched. Evidence that counts, strongest "
    "first: the report itself names the PR as the suspect; the PR changed a file the report's "
    "stack or paths name; the PR's title or summary names the setting, function, format or code "
    "path the symptom exercises. Words shared by chance do not count. Most candidates are "
    "unrelated; a fix to an unrelated bug is never the cause. "
    'Answer JSON only: {"ranking":[{"i":<number>,"p":<0..1>,"why":"<=12 words"}]} with at most '
    "10 entries, most likely first; probabilities are calibrated and low when nothing fits."
)
_RERANK_TOP = 40  # the rerank's gain was measured over the fused top 40


def _candidate_line(
    i: int,
    c: bi.Candidate,
    row: dict[str, Any],
    *,
    idents: set[str],
    cued: set[int],
    outside: set[int],
) -> str:
    files = [
        f
        for f in row.get("affected_file_paths") or c.matched_paths
        if not f.startswith(("tests/", "docs/"))
    ]
    summary = str(row.get("cl") or row.get("body") or "")[:260]
    text = fu.pr_text(row) if row else c.title
    shares = [
        *(["named by the report"] if c.pr_number in cued else []),
        *c.matched_paths[:3],
        *sorted(i for i in idents if i in text)[:4],
    ]
    line = (
        f"[{i}] #{c.pr_number} {c.title} (first release {c.minor_version or '?'})\n"
        f"    files: {', '.join(files[:8]) or '-'}\n"
        f"    summary: {summary.strip() or '-'}"
    )
    if shares:
        line += f"\n    shares with the report: {', '.join(shares)}"
    if c.pr_number in outside:
        line += (
            "\n    NOTE: merged BEFORE the release range the report supports; "
            "rank it high only with strong evidence"
        )
    return line


async def rerank_stage(
    pool: list[bi.Candidate],
    *,
    text: str,
    rows_by_number: dict[int, dict[str, Any]] | None = None,
    top: int = _RERANK_TOP,
    force: bool = False,
    weak: bool = False,
    extras: list[bi.Candidate] | None = None,
    cued: list[int] | None = None,
    timeout_s: float | None = None,
) -> tuple[list[bi.Candidate], dict[str, Any]]:
    """One LLM call over the fused top `top`, waiting at most `timeout_s`
    (default `code_bisect_rerank_timeout_s`). It runs when the caller asks (`rerank`) or
    when the fused top is weak (see `fu.WEAK_MARGIN` for the measured gain). The wider
    window's `extras` ride along, held back unless the model is confident and they share
    evidence. Any failure keeps the fused order."""
    if not (force or weak) or not pool or not text.strip():
        return pool, {}
    from server.llm.client import get_llm_client_for_model

    rows = rows_by_number or {}
    head = [*pool[:top], *(extras or [])]
    outside = {c.pr_number for c in extras or []}
    texts = {c.pr_number: fu.pr_text(rows.get(c.pr_number, {})) for c in head}
    idents = fu.rare_identifiers(fu.report_identifiers(text), texts)
    lines = [f"BUG REPORT:\n{text[:1800]}\n", "CANDIDATES:"]
    for i, c in enumerate(head, 1):
        lines.append(
            _candidate_line(
                i,
                c,
                rows.get(c.pr_number, {}),
                idents=idents,
                cued=set(cued or []),
                outside=outside,
            )
        )
    model = str(settings.code_bisect_llm_rerank_model)
    try:
        result = await asyncio.wait_for(
            get_llm_client_for_model(model).complete(
                system=_RERANK_SYSTEM,
                user="\n".join(lines),
                max_tokens=700,
                response_format={"type": "json_object"},
            ),
            timeout=float(
                settings.code_bisect_rerank_timeout_s if timeout_s is None else timeout_s
            ),
        )
        body = result.text[result.text.find("{") : result.text.rfind("}") + 1]
        ranking = [x for x in json.loads(body).get("ranking", []) if isinstance(x, dict)]
    except Exception as e:  # noqa: BLE001 — the fused order stands
        logger.info("bisect rerank failed: %s", e)
        trigger = "weak" if weak else "asked"
        return [*pool, *(extras or [])], {"status": "failed", "trigger": trigger}
    picked = [
        (head[int(x["i"]) - 1].pr_number, float(x.get("p") or 0.0))
        for x in ranking
        if str(x.get("i", "")).isdigit() and 0 < int(x["i"]) <= len(head)
    ]
    evidence = {
        c.pr_number for c in head if c.matched_paths or any(i in texts[c.pr_number] for i in idents)
    }
    by_number = {c.pr_number: c for c in [*pool, *(extras or [])]}
    order = fu.merge_outside(
        picked,
        outside=outside,
        evidence=evidence,
        in_window=[c.pr_number for c in pool],
    )
    lead = {n for n, _ in picked}
    for n in lead:
        by_number[n].why = ["llm_rerank", *by_number[n].why]
    top_p = picked[0][1] if picked else 0.0
    return [by_number[n] for n in order if n in by_number], {
        "model": model,
        "reranked": len(picked),
        "trigger": "weak" if weak else "asked",
        "top_p": round(top_p, 2),
        "wider_window": len(outside),
    }


async def wide_extras(
    source: Any,
    org: str,
    tracker_ns: str,
    *,
    good: str,
    bad: str,
    master: bool,
    versions: list[str],
    report_at: datetime | None,
    in_window: set[int],
    text: str,
    paths: list[str],
) -> tuple[list[bi.Candidate], dict[int, dict[str, Any]]]:
    """The weak path's second window — 12 releases below the failing one (or 180 days with
    none named), minus what the first window already held — fused on its own; its top
    `fu.WIDE_EXTRAS` ride into the rerank, never ahead of the first window without evidence.
    `([], {})` on any failure or with no source."""
    if source is None:
        return [], {}
    wide = bi.versions_between(good, bad, master=master, lookback=12) if versions else []
    days = int(settings.code_bisect_wide_days)
    floor = None if wide else (report_at - timedelta(days=days) if report_at else None)
    if not wide and floor is None:
        return [], {}
    try:
        rows, vector = await asyncio.gather(
            source.window_prs(org, tracker_ns, wide, before=report_at, after=floor),
            _embed(text) if text.strip() else asyncio.sleep(0, result=[]),
        )
    except Exception as e:  # noqa: BLE001 — the first window stands
        logger.info("bisect wide window read failed: %s", e)
        return [], {}
    extra = {
        int(r["pr_number"]): r
        for r in rows
        if r.get("pr_number") and int(r["pr_number"]) not in in_window
    }
    if not extra:
        return [], {}
    signals = fu.window_signals(
        list(extra.values()),
        text=text,
        paths=paths,
        vector_order=await _vector_order(org, tracker_ns, vector, list(extra)),
        scan_order=[],
        before=report_at,
    )
    order, why = fu.fuse(signals, limit=fu.WIDE_EXTRAS)
    report_paths = {str(p) for p in paths if p}
    out: list[bi.Candidate] = []
    for n in order:
        cand = _candidate(extra[n], report_paths)
        cand.why = ["wider_window", *why.get(n, [])]
        out.append(cand)
    return out, extra
