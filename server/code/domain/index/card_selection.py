"""Which files get an orientation card.

A card pass is bounded (`code_summary_max_scopes_per_pass`), so the ORDER
in which files are considered decides which files ever get a card. The order
is merged-PR churn per path over a trailing window, restricted to the source
tree (``base/`` and ``contrib/`` are vendored and never selected): files that
change are the files people ask about. Once the churn list is exhausted the
include/call-graph rank the weekly graph-stats pass stores on every
``code_summaries`` row fills the pass, so a namespace whose PR history is
thin still gets cards.

Pure functions; the worker feeds them rows.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def split_csv(raw: str) -> tuple[str, ...]:
    """`"src/, programs/"` -> ("src/", "programs/"); empty -> ()."""
    return tuple(p.strip() for p in (raw or "").split(",") if p.strip())


def churn_eligible(
    path: str,
    *,
    prefixes: Sequence[str] = (),
    exclude: Sequence[str] = (),
) -> bool:
    """A path the churn order may propose: under one of `prefixes` (any
    path when empty), and under none of `exclude` at any depth
    (``src/x/contrib/y`` is vendored too)."""
    if prefixes and not any(path.startswith(p) for p in prefixes):
        return False
    for ex in exclude:
        if path.startswith(ex) or f"/{ex}" in path:
            return False
    return True


def select_card_paths(
    *,
    pagerank_order: Sequence[str],
    churn_order: Sequence[str],
    limit: int,
) -> list[str]:
    """The first `limit` paths a card pass considers, in order: the churn list,
    then the pagerank order once it is exhausted."""
    if limit <= 0:
        return []
    churn = set(churn_order)
    order = list(churn_order) + [p for p in pagerank_order if p not in churn]
    out: list[str] = []
    seen: set[str] = set()
    for p in order:
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def twin_rows(rows: Iterable[dict[str, Any]], *, active_model: str) -> list[dict[str, Any]]:
    """Live ``code_summaries`` rows written under a model other than the
    active one whose (scope_type, scope_path) also has a live row under the
    active model — the rows the dedupe tombstones. Rows with no active
    twin are left for the graph-stats pass to twin first, so a namespace
    never loses its only copy of a file's rank."""
    by_model: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for r in rows:
        key = (str(r.get("scope_type") or "file"), str(r.get("scope_path") or ""))
        by_model.setdefault(str(r.get("embedding_model") or ""), {})[key] = r
    active = by_model.get(active_model, {})
    out: list[dict[str, Any]] = []
    for model, keyed in by_model.items():
        if model == active_model:
            continue
        for key, r in keyed.items():
            if key in active:
                out.append(r)
    return out


def coverage(
    selected: Iterable[str], gold_prefixes_per_question: Sequence[Sequence[str]]
) -> tuple[int, int]:
    """(questions with at least one gold prefix covered, questions). A gold
    prefix is covered when a selected path starts with it — gold paths are
    extension-stripped (``src/Access/QuotaCache`` covers both the header and
    the source) or directories (``src/Access``)."""
    paths = list(selected)
    hit = 0
    for golds in gold_prefixes_per_question:
        if any(p.startswith(g) for g in golds for p in paths):
            hit += 1
    return hit, len(gold_prefixes_per_question)
