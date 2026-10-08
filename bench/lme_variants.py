"""LongMemEval variants for robustness: evidence position and context scale.

Two families of LongMemEval variants probe robustness: memory architectures
tend to lose F1 as the history grows from 50% to 200%, and when the evidence
sessions sit in the first third of the history rather than the last. These
variants measure both for loom. These are pure functions over a LongMemEval item's
parallel lists (`haystack_sessions`, `haystack_dates`, `haystack_session_ids`,
`answer_session_ids`) so `bench/longmemeval.py` can apply them before ingest
and `tests/unit` can pin them without a server.

    position = "early" | "middle" | "late"   evidence sessions moved, as a
                                              block, into that third of the
                                              haystack order (dates and ids
                                              travel with their sessions)
    scale    = 0.5 .. 2.0                    < 1: drop non-evidence sessions
                                              at random down to round(n*scale);
                                              > 1: append distractor sessions
                                              drawn from OTHER items' non-
                                              evidence sessions, ids prefixed
                                              so recall@k cannot count them as
                                              evidence

Evidence sessions are never dropped, so a variant changes where and among how
much noise the answer sits, never whether it is there.
"""

from __future__ import annotations

import random
from typing import Any


def _triples(item: dict[str, Any]) -> list[tuple[Any, Any, Any]]:
    sessions = list(item.get("haystack_sessions", []) or [])
    dates = list(item.get("haystack_dates", []) or [])
    ids = list(item.get("haystack_session_ids", []) or [])
    n = len(sessions)
    dates += [""] * (n - len(dates))
    ids += [""] * (n - len(ids))
    return [(sessions[i], dates[i], ids[i]) for i in range(n)]


def _split(
    item: dict[str, Any], triples: list[tuple] | None = None
) -> tuple[list[tuple], list[tuple]]:
    """(evidence triples in original order, other triples in original order)."""
    answer_ids = {str(s) for s in (item.get("answer_session_ids") or []) if s}
    ev, other = [], []
    for t in _triples(item) if triples is None else triples:
        (ev if str(t[2]) in answer_ids else other).append(t)
    return ev, other


def _rebuild(item: dict[str, Any], triples: list[tuple]) -> dict[str, Any]:
    out = dict(item)
    out["haystack_sessions"] = [t[0] for t in triples]
    out["haystack_dates"] = [t[1] for t in triples]
    out["haystack_session_ids"] = [t[2] for t in triples]
    return out


def distractor_pool(dataset: list[dict[str, Any]]) -> dict[str, list[tuple]]:
    """question_id → that item's NON-evidence session triples, for padding
    other items. Ids are prefixed with the donor's question id so a padded
    session can never match a recipient's `answer_session_ids`."""
    pool: dict[str, list[tuple]] = {}
    for it in dataset:
        qid = str(it.get("question_id", ""))
        _, other = _split(it)
        pool[qid] = [(s, d, f"pad:{qid}:{sid}") for (s, d, sid) in other]
    return pool


def apply_variant(
    item: dict[str, Any],
    *,
    position: str = "",
    scale: float = 1.0,
    pool: dict[str, list[tuple]] | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """A copy of `item` with the variant applied and `item["variant"]` set to
    a short label ("" when nothing changed). Evidence sessions are kept."""
    rng = rng or random.Random(0)
    original = _triples(item)
    ev, other = _split(item, original)
    label: list[str] = []

    if scale and abs(scale - 1.0) > 1e-9:
        n_total = len(ev) + len(other)
        target_other = max(0, int(round(n_total * scale)) - len(ev))
        if target_other < len(other):
            keep = sorted(rng.sample(range(len(other)), target_other))
            other = [other[i] for i in keep]
        elif target_other > len(other):
            need = target_other - len(other)
            qid = str(item.get("question_id", ""))
            donors = [t for k, ts in (pool or {}).items() if k != qid for t in ts]
            if donors:
                rng.shuffle(donors)
                other = other + donors[:need]
        if len(ev) + len(other) != n_total:  # no donors or a rounding no-op: unchanged
            label.append(f"scale={scale:g}")

    if position in ("early", "middle", "late") and ev:
        n_other = len(other)
        if position == "early":
            at = 0
        elif position == "late":
            at = n_other
        else:
            at = n_other // 2
        triples = other[:at] + ev + other[at:]
        label.append(f"position={position}")
    elif not label:
        return {**item, "variant": ""}
    else:
        # Scale only: keep the evidence where it was relative to the survivors.
        # `ev` and `other` hold the very tuples of `original`, so identity is the
        # original position; padding sorts last, in draw order.
        order = {id(t): i for i, t in enumerate(original)}
        triples = sorted(ev + other, key=lambda t: order.get(id(t), len(order)))

    out = _rebuild(item, triples)
    out["variant"] = ",".join(label)
    return out


def evidence_third(item: dict[str, Any]) -> str:
    """Which third of the haystack the FIRST evidence session sits in; the
    position label for an unmodified item."""
    ids = [str(s) for s in (item.get("haystack_session_ids") or [])]
    answer = {str(s) for s in (item.get("answer_session_ids") or [])}
    n = len(ids)
    for i, sid in enumerate(ids):
        if sid in answer and n:
            frac = i / n
            return "early" if frac < 1 / 3 else ("middle" if frac < 2 / 3 else "late")
    return ""
