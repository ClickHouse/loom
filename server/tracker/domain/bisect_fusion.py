"""Report-time bisect fuses every signal the report carries.

The diff scan only admits PRs whose files the report's paths or resolved
frames reach, and a culprit's files are often nowhere near them. The fusion
ranks every PR of the version window by reciprocal rank over:

- the report's meaning against each PR's vector (loom's own
  `qa_item_embeddings`, the tracker's upstream column is empty for PRs),
- the report's words against each PR's title, changelog line and body head
  (BM25 with the window as the corpus),
- the report's own paths, exact and by subtree,
- how recently the PR merged before the report,
- the diff scan's own order.

A report that names its suspect ("introduced in #N", "regression after #N",
an audit finding `h_prN_…`) is read as evidence on top, guarded against
negation ("the bisect boundary was rejected — PR #N only touches…").

Pure functions here; the handler does the reads.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime
from typing import Any

# (signal, weight) — reciprocal-rank fusion, k = 20; every signal earned its
# place on the dev half (dropping any one lowered dev top-1 or top-10)
WEIGHTS: tuple[tuple[str, float], ...] = (
    ("scan", 1.0),
    ("vector", 1.0),
    ("lexical", 1.0),
    ("exact_paths", 1.0),
    ("subtree", 0.6),
    ("recency", 0.5),
)
RRF_K = 20
# a PR the report mentions without a cue ("see #N", a bare link) — evidence,
# not a verdict
MENTION_WEIGHT = 2.0
SUBTREE_DEPTH = 4  # src/Processors/QueryPlan/Optimizations

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_STOP = frozenset(
    """the and for with that this from are was not but have has had you your can will would
    should could when then than into onto over under after before while which what where who
    how why all any some each every more most less least very just also only same other such
    like been being does did doing done get got make made use used using query queries select
    table tables clickhouse server client version versions error errors issue bug crash crashes
    fails failed fail work works working expected actual result results reproduce reproduces
    repro fiddle https http com www github see output input returns return value values column
    columns row rows data type types function functions setting settings fix fixed add
    added""".split()
)
# a PR reference in the report: `#123456`, a pull URL, "PR 123456", an audit
# finding id `h_pr123456_001`
_REF = re.compile(r"(?:h_pr|#|/pull/|\bPR\s*#?\s*|pull request\s*#?\s*)(\d{4,6})(?!\d)", re.I)
_CUE = re.compile(
    r"(introduc|regress|caused|\bsince\b|\bafter\b|\bbroke|bisect|culprit|breaking|h_pr\d)", re.I
)
_NEG = re.compile(
    r"(reject|ruled out|\bnot (?:the|a|caused|responsible)|unrelated|innocent"
    r"|only (?:modif|touch|chang))",
    re.I,
)


def sane_version(text: str, parse: Any) -> str:
    """`text` when it parses as a ClickHouse release (year ≥ 20), else "" — a
    `10.6` lifted from a report body is not a release (searching 10.7..12.6
    finds nothing)."""
    v = parse(text) if text else None
    return text if v and v[0] >= 20 else ""


_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def _norm(word: str) -> str:
    w = word.lower()
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith(("ss", "is", "us")) else w


def tokens(text: str) -> list[str]:
    """Words of `text`: every identifier whole, plus its snake_case and
    camelCase parts, plurals folded — a flaky test `03322_initial_query_start_
    time_check` meets the PR "Add function initialQueryStartTime" (live: the
    whole-identifier tokens never met on 2 of 8 text-only reports)."""
    out: list[str] = []
    for raw in _TOKEN.findall(text or ""):
        whole = raw.lower().strip("_")
        if whole and whole not in _STOP:
            out.append(whole)
        parts = [p for chunk in raw.split("_") for p in _CAMEL.findall(chunk)]
        if len(parts) > 1:
            out.extend(n for p in parts if len(p) >= 3 and (n := _norm(p)) not in _STOP)
    return out


def report_suspects(text: str, *, own_number: int = 0) -> tuple[list[int], list[int]]:
    """(cued, mentioned) PR numbers the report names, in order. Cued: the line
    says it introduced the bug and does not deny it. Mentioned: any other
    reference. The caller keeps those merged before the report."""
    cued: list[int] = []
    other: list[int] = []
    for line in (text or "").splitlines():
        refs = [int(m.group(1)) for m in _REF.finditer(line)]
        if not refs:
            continue
        is_cued = bool(_CUE.search(line)) and not _NEG.search(line)
        for n in refs:
            if n == own_number:
                continue
            (cued if is_cued else other).append(n)
    cued = list(dict.fromkeys(cued))
    return cued, [n for n in dict.fromkeys(other) if n not in cued]


def _prefix(path: str, depth: int) -> str:
    return "/".join(path.split("/")[:depth])


def _bm25_order(query: list[str], docs: dict[int, Counter[str]]) -> list[int]:
    if not query or not docs:
        return []
    df: Counter[str] = Counter()
    for d in docs.values():
        df.update(d.keys())
    n = len(docs)
    avg = sum(sum(d.values()) for d in docs.values()) / max(1, n)
    q = set(query)
    scored: list[tuple[float, int]] = []
    for num, d in docs.items():
        length = sum(d.values()) or 1
        s = 0.0
        for t in q:
            tf = d.get(t)
            if tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * length / max(avg, 1.0)))
        if s > 0:
            scored.append((s, num))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [num for _, num in scored]


def window_signals(
    rows: list[dict[str, Any]],
    *,
    text: str,
    paths: list[str],
    vector_order: list[int],
    scan_order: list[int],
    before: datetime | None,
) -> dict[str, list[int]]:
    """Each signal's ranked PR numbers over the window's rows (`pr_number`,
    `title`, `closed_at`, `affected_file_paths`, `cl`, `body`)."""
    report_paths = {str(p) for p in paths if p}
    subtrees = {_prefix(p, SUBTREE_DEPTH) for p in report_paths}
    docs: dict[int, Counter[str]] = {}
    exact: list[tuple[int, int]] = []
    subtree: list[tuple[int, int]] = []
    recency: list[tuple[float, int]] = []
    for r in rows:
        n = int(r.get("pr_number") or 0)
        if not n:
            continue
        files = [str(f) for f in (r.get("affected_file_paths") or [])]
        docs[n] = Counter(
            tokens(
                " ".join(
                    [str(r.get("title") or ""), str(r.get("cl") or ""), str(r.get("body") or "")]
                )
            )
        )
        if report_paths:
            e = len(set(files) & report_paths)
            if e:
                exact.append((e, n))
            s = sum(1 for f in files if _prefix(f, SUBTREE_DEPTH) in subtrees)
            if s:
                subtree.append((s, n))
        merged = _as_dt(r.get("closed_at"))
        if before is not None and merged is not None:
            recency.append(((before - merged).total_seconds(), n))
    in_window = set(docs)
    return {
        "scan": [n for n in scan_order if n in in_window],
        "vector": [n for n in vector_order if n in in_window],
        "lexical": _bm25_order(tokens(text), docs),
        "exact_paths": [n for _, n in sorted(exact, key=lambda x: (-x[0], -x[1]))],
        "subtree": [n for _, n in sorted(subtree, key=lambda x: (-x[0], -x[1]))],
        "recency": [n for _, n in sorted(recency, key=lambda x: (x[0], -x[1])) if _ >= 0],
    }


def _rrf_scores(
    signals: dict[str, list[int]], mentioned: list[int] | None
) -> tuple[dict[int, float], dict[int, list[str]]]:
    score: dict[int, float] = {}
    why: dict[int, list[str]] = {}
    for name, weight in WEIGHTS:
        for i, n in enumerate(signals.get(name) or []):
            score[n] = score.get(n, 0.0) + weight / (RRF_K + i)
            if i < 10:
                why.setdefault(n, []).append(name)
    for i, n in enumerate(mentioned or []):
        score[n] = score.get(n, 0.0) + MENTION_WEIGHT / (RRF_K + i)
        why.setdefault(n, []).append("mentioned_by_report")
    return score, why


def fuse(
    signals: dict[str, list[int]],
    *,
    mentioned: list[int] | None = None,
    cued: list[int] | None = None,
    limit: int = 200,
) -> tuple[list[int], dict[int, list[str]]]:
    """(order, why): reciprocal-rank fusion of the signals; mentioned PRs add
    evidence, cued suspects lead. `why[n]` lists the signals that ranked `n`
    in their top 10 (the answer says where each candidate came from)."""
    score, why = _rrf_scores(signals, mentioned)
    order = [n for n, _ in sorted(score.items(), key=lambda x: (-x[1], -x[0]))]
    for n in cued or []:
        why.setdefault(n, []).insert(0, "named_by_report")
    # the caller passes only cued PRs inside the window and merged before the report
    order = list(dict.fromkeys([*(cued or []), *order]))
    return order[:limit], why


def keep_fused_order(pool: list[Any], marks: dict[int, Any]) -> list[Any]:
    """The marks stage after a fusion: hard-demoted candidates last, the fused
    order otherwise kept. The marks' symbol bonus re-sorted the fused list
    (live: a report-named culprit fell from #1 to #2–#14 behind a PR whose diff
    touched a generic word the report listed as a symbol)."""

    def demoted(c: Any) -> bool:
        m = marks.get(c.pr_number)
        return bool(m is not None and m.demoted)

    return [c for c in pool if not demoted(c)] + [c for c in pool if demoted(c)]


def lead_suspects(pool: list[Any], cued: list[int]) -> list[Any]:
    """The report's cued suspects first after the later stages, unless a known
    fix's own evidence (blamed lines, its "Caused by") already leads."""
    if not cued or (pool and (pool[0].blamed_by_fix or pool[0].named_cause_by)):
        return pool
    lead = [c for n in cued for c in pool if c.pr_number == n]
    return lead + [c for c in pool if c not in lead]


def _as_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s[:19].replace("T", " "))
    except ValueError:
        return None


# ---- the weak path -----------------------------------------------------------

# A fused top is weak when its RRF margin over #2 is under WEAK_MARGIN and the
# report does not name it. Only then do the rerank and the wider window run.
# Over the bisect benchmark, reports as filed, it fires on 14 of 70; held-out
# top-1 12 → 15, top-3 13 → 16; dev top-1 28 → 29, top-3 30 → 32. Stripped of
# PR numbers: held-out top-1 6 → 11, top-3 10 → 14. An extra "no shared file"
# clause fires on 43 of 70 runs for no gain beyond the margin's.
WEAK_MARGIN = 0.1
# an out-of-window candidate leads an in-window one only on the model's
# confidence AND shared evidence (a file, or an identifier the report names)
OUTSIDE_MIN_P = 0.5
WIDE_EXTRAS = 10
IDENT_MAX_WINDOW_DF = 30

_BACKTICK = re.compile(r"`([^`\n]{3,60})`")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CAMEL_ID = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b|\b[a-z]+[A-Z][A-Za-z0-9]+\b")
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{3,})\(")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


def top_margin(signals: dict[str, list[int]], mentioned: list[int] | None = None) -> float:
    """(s1 - s2) / s1 of the fused scores; 1.0 when fewer than two score."""
    score, _ = _rrf_scores(signals, mentioned)
    top = sorted(score.values(), reverse=True)[:2]
    if len(top) < 2 or top[0] <= 0:
        return 1.0
    return (top[0] - top[1]) / top[0]


def is_weak(*, margin: float, top_cued: bool) -> bool:
    """Whether the fused top needs a second reading (see WEAK_MARGIN)."""
    return not top_cued and margin < WEAK_MARGIN


def report_identifiers(text: str) -> set[str]:
    """The settings, functions, classes and formats a report names: backticked
    words, snake_case, CamelCase and call-shaped names."""
    out: set[str] = set()
    for quoted in _BACKTICK.findall(text or ""):
        out.update(_WORD.findall(quoted))
    for rx in (_SNAKE, _CAMEL_ID):
        out.update(rx.findall(text or ""))
    out.update(_CALL.findall(text or ""))
    return {w for w in out if len(w) >= 4 and w.lower() not in _STOP}


def rare_identifiers(idents: set[str], texts: dict[int, str]) -> set[str]:
    """The report's identifiers that at most IDENT_MAX_WINDOW_DF window PRs
    carry: words that single a PR out, not ones every PR shares."""
    return {
        i for i in idents if 0 < sum(1 for t in texts.values() if i in t) <= IDENT_MAX_WINDOW_DF
    }


def pr_text(row: dict[str, Any]) -> str:
    """What a PR says about itself: title, changelog line, body head, files."""
    files = row.get("affected_file_paths") or row.get("files") or []
    return " ".join(
        [
            str(row.get("title") or ""),
            str(row.get("cl") or ""),
            str(row.get("body") or ""),
            " ".join(str(f) for f in files),
        ]
    )


def merge_outside(
    ranked: list[tuple[int, float]],
    *,
    outside: set[int],
    evidence: set[int],
    in_window: list[int],
) -> list[int]:
    """The model's ranking with the wider window's candidates held back: an
    out-of-window PR keeps its place only at p ≥ OUTSIDE_MIN_P with evidence;
    otherwise it goes after every in-window candidate."""
    lead: list[int] = []
    held: list[int] = []
    for n, p in ranked:
        if n in outside and not (p >= OUTSIDE_MIN_P and n in evidence):
            held.append(n)
        else:
            lead.append(n)
    rest = [n for n in in_window if n not in lead]
    tail = [n for n in dict.fromkeys(held) if n not in lead]
    return list(dict.fromkeys([*lead, *rest, *tail]))
