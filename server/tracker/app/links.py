"""tracker.links — the pure half of issue–PR link recovery: the
query an issue makes (terms against the PR vocabulary, frames out of a
trace-shaped body), the features of an
(issue, PR) pair, the weighted score and its `why`, the ranking of one
issue's candidates into table rows (the closing-mention pattern and the
reader's "latest pass only" filter are `server/tracker/domain/links.py`).
Nothing here reads anything; the live reads are
`TrackerSource.link_*` / `prs_facts` and the pass in `server/tracker/workers/links.py`
joins the two.

Why a candidate list and not an edge: the tracker writes a `fixes` edge only where a
PR's description closes the issue by keyword, and many closed issues and merged fixes have
no edge at all. Candidates are (issue, PR) pairs in a time window ranked by BM25F, with the
issue's stack frames intersected with the PR's changed symbols and files recorded beside
the score. Every row carries its features so a reader says `why` and a consumer decides on
a threshold; the verdict word is always `candidate`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from server.tracker.domain import similar as sim
from server.tracker.store.links import LinkCandidate

# a fix opened before its report (6.6% of the gold's fixes) still merges
# after it; the window opens a week early so a PR merged the day the issue
# was filed is a candidate, and closes `window_days` after the report
WINDOW_BEFORE_DAYS = 7
MENTION_CLOSES = 2
MENTION_NAMED = 1
COMPONENT_BOOST = 0.05
MAX_SYMBOLS = 12
EPISTEMIC = "candidate"
# what the mention feature is worth relative to a closing keyword
_NAMED_SHARE = 0.5


@dataclass(frozen=True)
class Weights:
    mention: float = 1.0
    component: float = COMPONENT_BOOST


def weights_from_settings() -> Weights:
    from server.settings import settings

    return Weights(mention=float(settings.tracker_links_weight_mention))


@dataclass
class IssueQuery:
    number: int
    title: str = ""
    component: str = ""
    created_at: datetime | None = None
    tokens: int = 0
    terms: list[str] = field(default_factory=list)
    idf: list[float] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)  # method names of the frames
    files: list[str] = field(default_factory=list)  # basenames of the path frames

    @property
    def frames_total(self) -> int:
        return len(self.symbols) + len(self.files)


def issue_query(
    number: int,
    title: str,
    body: str,
    stats: sim.TokenStats,
    *,
    component: str = "",
    created_at: datetime | None = None,
) -> IssueQuery:
    """The query one issue makes: tracker.similar's tokens and term selection
    against the PR vocabulary (title tokens first, rarest next, camelCase
    pieces in the slots left), the frames of a trace-shaped body (their
    method names meet the PR's diff, their file basenames its file list),
    common frames dropped."""
    text = f"{title}\n{body or ''}"
    tokens = sim.query_tokens(text)
    terms = sim.select_terms(
        tokens, stats, prefer=sim.query_tokens(title), defer=sim.camel_pieces(text)
    )
    frames = sim.select_frames(sim.frames_from_text(text), stats)
    symbols = list(dict.fromkeys(f.token for f in frames if f.kind == "symbol"))[:MAX_SYMBOLS]
    files = list(
        dict.fromkeys(f.path.rsplit("/", 1)[-1] for f in frames if f.kind == "file" and f.path)
    )[:MAX_SYMBOLS]
    return IssueQuery(
        number=int(number),
        title=title,
        component=component,
        created_at=created_at,
        tokens=len(tokens),
        terms=terms,
        idf=[stats.idf(t) for t in terms],
        symbols=symbols,
        files=files,
    )


def window(
    created_at: datetime, *, window_days: int, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """[since, until) of the PR merges that are candidates for an issue
    opened at `created_at`: a week before it to `window_days` after, never
    past `now`."""
    since = created_at - timedelta(days=WINDOW_BEFORE_DAYS)
    until = created_at + timedelta(days=max(1, int(window_days)))
    if now is not None and now < until:
        until = now
    return since, until


@dataclass
class Features:
    bm25_title: float = 0.0
    bm25_body: float = 0.0
    bm25_norm: float = 0.0
    window_hours: int = 0
    component_match: int = 0
    frames_symbols: int = 0
    frames_files: int = 0
    frames_total: int = 0
    closes_mention: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "bm25_title": round(self.bm25_title, 4),
            "bm25_body": round(self.bm25_body, 4),
            "bm25_norm": round(self.bm25_norm, 4),
            "window_hours": self.window_hours,
            "component_match": self.component_match,
            "frames_symbols": self.frames_symbols,
            "frames_files": self.frames_files,
            "frames_total": self.frames_total,
            "closes_mention": self.closes_mention,
        }


def score(f: Features, w: Weights) -> float:
    """bm25_norm + mention weight × (1 closing / 0.5 named) + component
    boost. Additive so a reader can see which term carried a pair; every
    term is in [0, its weight]. The frames features ride the row unscored."""
    mention = (
        1.0
        if f.closes_mention >= MENTION_CLOSES
        else (_NAMED_SHARE if f.closes_mention >= MENTION_NAMED else 0.0)
    )
    return (
        float(f.bm25_norm) + w.mention * mention + w.component * (1.0 if f.component_match else 0.0)
    )


def why(f: Features) -> list[str]:
    out: list[str] = []
    if f.closes_mention >= MENTION_CLOSES:
        out.append("closes")
    elif f.closes_mention >= MENTION_NAMED:
        out.append("mentions")
    if f.bm25_norm > 0:
        out.append("lexical")
    if f.frames_symbols or f.frames_files:
        out.append("frames")
    if f.component_match:
        out.append("component")
    return out


def _component_match(issue_component: str, row: dict[str, Any]) -> int:
    comp = str(issue_component or "")
    if not comp:
        return 0
    if comp == str(row.get("primary_component") or ""):
        return 1
    return 1 if comp in {str(x) for x in (row.get("src_components") or [])} else 0


def features_of(
    q: IssueQuery,
    row: dict[str, Any],
    *,
    top_bm25: float,
    symbol_hits: int = 0,
) -> Features:
    """One pair's features from a `link_pr_candidates` row (bm25_title,
    bm25_body, mention, file_hits, merged_at, components) and the symbol
    hits of `prs_facts`."""
    bm25 = float(row.get("bm25_title") or 0.0) + float(row.get("bm25_body") or 0.0)
    merged = row.get("merged_at")
    hours = 0
    if isinstance(merged, datetime) and q.created_at is not None:
        hours = int((merged - q.created_at).total_seconds() // 3600)
    return Features(
        bm25_title=float(row.get("bm25_title") or 0.0),
        bm25_body=float(row.get("bm25_body") or 0.0),
        bm25_norm=(bm25 / top_bm25) if top_bm25 > 0 else 0.0,
        window_hours=hours,
        component_match=_component_match(q.component, row),
        frames_symbols=min(int(symbol_hits or 0), 255),
        frames_files=min(int(row.get("file_hits") or 0), 255),
        frames_total=min(q.frames_total, 255),
        closes_mention=int(row.get("mention") or 0),
    )


def rank(
    q: IssueQuery,
    rows: list[dict[str, Any]],
    *,
    weights: Weights,
    symbol_hits: dict[int, int] | None = None,
    per_issue: int = 20,
) -> list[dict[str, Any]]:
    """One issue's candidates scored and ranked: [{pr_number, score, rank,
    features, why, title, url, merged_at, minor_version}], best first, ties
    by the newer merge then the lower number; at most `per_issue`."""
    uniq: list[dict[str, Any]] = []
    seen: set[int] = set()
    for r in rows:
        n = int(r.get("pr_number") or 0)
        if n > 0 and n not in seen:
            seen.add(n)
            uniq.append(r)
    top = max(
        (float(r.get("bm25_title") or 0.0) + float(r.get("bm25_body") or 0.0) for r in uniq),
        default=0.0,
    )
    hits = symbol_hits or {}
    out: list[dict[str, Any]] = []
    for r in uniq:
        n = int(r.get("pr_number") or 0)
        f = features_of(q, r, top_bm25=top, symbol_hits=hits.get(n, 0))
        out.append(
            {
                "pr_number": n,
                "score": round(score(f, weights), 4),
                "features": f,
                "why": why(f),
                "title": str(r.get("title") or "")[:200],
                "url": str(r.get("url") or ""),
                "merged_at": r.get("merged_at"),
                "minor_version": str(r.get("minor_version") or ""),
            }
        )
    out.sort(key=lambda c: (-c["score"], -(c["features"].window_hours), c["pr_number"]))
    for i, c in enumerate(out[:per_issue], start=1):
        c["rank"] = i
    return out[:per_issue]


def table_rows(
    org: str,
    tracker_namespace: str,
    q: IssueQuery,
    ranked: list[dict[str, Any]],
    *,
    computed_at: datetime,
    version: int,
) -> list[dict[str, Any]]:
    """Full loom.qa_link_candidates rows for one issue's ranked list; every
    row of a pass carries the same computed_at and version so the readers'
    "latest pass" filter keeps a whole list or none of it."""
    from server.tracker.store.links import build_link_row

    rows: list[dict[str, Any]] = []
    for c in ranked:
        f: Features = c["features"]
        merged = c.get("merged_at")
        rows.append(
            build_link_row(
                org=org,
                qa_namespace=tracker_namespace,
                issue_number=q.number,
                pr_number=int(c["pr_number"]),
                rank=int(c["rank"]),
                score=float(c["score"]),
                bm25_title=f.bm25_title,
                bm25_body=f.bm25_body,
                bm25_norm=f.bm25_norm,
                window_hours=f.window_hours,
                component_match=f.component_match,
                frames_symbols=f.frames_symbols,
                frames_files=f.frames_files,
                frames_total=f.frames_total,
                closes_mention=f.closes_mention,
                terms=list(q.terms),
                pr_title=str(c.get("title") or "")[:200],
                pr_url=str(c.get("url") or ""),
                pr_merged_at=merged if isinstance(merged, datetime) else datetime(1970, 1, 1),
                pr_minor_version=str(c.get("minor_version") or ""),
                issue_title=str(q.title or "")[:200],
                issue_created_at=q.created_at or datetime(1970, 1, 1),
                computed_at=computed_at,
                version=version,
            )
        )
    return rows


def passes_floor(top: dict[str, Any], *, min_score: float) -> bool:
    """The consumers' rule on a list's head: the score floor and a closing
    keyword naming the issue. A text-only top is never taken as the fix."""
    return (
        float(top.get("score") or 0.0) >= min_score
        and int(top.get("closes_mention") or 0) >= MENTION_CLOSES
    )


async def fix_candidate(
    org: str,
    tracker_namespace: str,
    issue_number: int,
    *,
    repo: Any = None,
) -> dict[str, Any] | None:
    """The consumers' read (code.backport_gap's issue path, the bisect's fix
    candidate): the issue's top candidate when it passes `passes_floor`
    (LOOM_TRACKER_LINKS_MIN_SCORE), in the wire shape with `source: candidate`
    and the BM25F margin it led the runner-up by; None under the rule,
    with no list, or when the read fails — a consumer then adds nothing and
    says `no_fix`."""
    from server.settings import settings

    if int(issue_number) <= 0 or not tracker_namespace:
        return None
    try:
        if repo is None:
            from server.tracker.store.links import TrackerLinksRepo

            repo = TrackerLinksRepo()
        rows = await repo.candidates_for_issue(org, tracker_namespace, int(issue_number), limit=2)
    except Exception as e:  # noqa: BLE001 — a candidate is a bonus, never the answer
        logging.getLogger("loom.tracker.links").info("link candidate read failed: %s", e)
        return None
    if not rows:
        return None
    top, second = rows[0], rows[1] if len(rows) > 1 else None
    if not passes_floor(top, min_score=float(settings.tracker_links_min_score)):
        return None
    item = as_item(top)
    lead = float(top.get("bm25_title") or 0.0) + float(top.get("bm25_body") or 0.0)
    sec = 0.0
    if second is not None:
        sec = float(second.get("bm25_title") or 0.0) + float(second.get("bm25_body") or 0.0)
    item["margin"] = round(lead / sec, 3) if sec > 0 else None
    return item


def as_item(r: LinkCandidate) -> dict[str, Any]:
    """One table row in the wire shape of tracker.links."""
    f = Features(
        bm25_title=float(r.get("bm25_title") or 0.0),
        bm25_body=float(r.get("bm25_body") or 0.0),
        bm25_norm=float(r.get("bm25_norm") or 0.0),
        window_hours=int(r.get("window_hours") or 0),
        component_match=int(r.get("component_match") or 0),
        frames_symbols=int(r.get("frames_symbols") or 0),
        frames_files=int(r.get("frames_files") or 0),
        frames_total=int(r.get("frames_total") or 0),
        closes_mention=int(r.get("closes_mention") or 0),
    )
    merged = r.get("pr_merged_at")
    created = r.get("issue_created_at")
    computed = r.get("computed_at")
    return {
        "issue_number": int(r.get("issue_number") or 0),
        "pr_number": int(r.get("pr_number") or 0),
        "rank": int(r.get("rank") or 0),
        "score": round(float(r.get("score") or 0.0), 4),
        "why": why(f),
        "features": f.as_dict(),
        "terms": [str(t) for t in (r.get("terms") or [])],
        "pr_title": str(r.get("pr_title") or ""),
        "pr_url": str(r.get("pr_url") or ""),
        "pr_merged_at": merged.isoformat() if isinstance(merged, datetime) else "",
        "pr_minor_version": str(r.get("pr_minor_version") or ""),
        "issue_title": str(r.get("issue_title") or ""),
        "issue_created_at": created.isoformat() if isinstance(created, datetime) else "",
        "computed_at": computed.isoformat() if isinstance(computed, datetime) else "",
        "epistemic": EPISTEMIC,
        "source": "candidate",
    }


__all__ = [
    "COMPONENT_BOOST",
    "EPISTEMIC",
    "MENTION_CLOSES",
    "MENTION_NAMED",
    "WINDOW_BEFORE_DAYS",
    "Features",
    "IssueQuery",
    "Weights",
    "as_item",
    "features_of",
    "fix_candidate",
    "issue_query",
    "passes_floor",
    "rank",
    "score",
    "table_rows",
    "weights_from_settings",
    "why",
    "window",
]
