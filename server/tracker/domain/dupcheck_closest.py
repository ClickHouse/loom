"""A `none` that has a same-titled record says so.

The calibrated fit decides the verdict and nothing here changes it. But the
fit rightly weighs a title match alone low: generic `LOGICAL_ERROR` titles
("Bad cast from type DB::ColumnNullable to DB::ColumnString") recur across
distinct defects, so the issue that carries the draft's exact title can
score under the related line. A bare `none` is what an agent asking
"already reported?" reads as "file it" while an identical title sits in the
record.

`closest` is at most three rows the fit scored under the related line whose
title is near-identical to the draft's (normalized character similarity at
least `TITLE_SIMILARITY_MIN`), each with its confidence and evidence.
`needs` names the draft inputs the stored fit rewards and the draft left
empty, read from the fit's own coefficients, never a fixed list: a fit that
pins `frames` at 0 never asks for frames. Pure: no reads.

`nearest` is at most three more scored-out rows, the fit's
best under the related line, kept only when their title shares a distinctive
term of the draft (an identifier such as `toStartOfInterval`, else a word of
five letters or more). They are labelled as below the line, never matches:
without them "IllegalTypeOfArgument in toStartOfInterval" can answer `none`
while open `toStartOfInterval` issues sit under the line, leaving the reader
no candidate to check by eye.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

# normalized title similarity at which a scored-out row is shown (0..1)
TITLE_SIMILARITY_MIN = 0.9
CLOSEST_MAX = 3
# Nearest rows shown on `none` (on `related`, served items count toward it)
NEAREST_MAX = 3
_NEAREST_KEEP = ("namespace", "number", "title", "state", "url", "confidence")
# a CamelCase / snake_case / C++-qualified name in the draft: the term a
# nearest row must share when the draft has one
_IDENT_RE = re.compile(
    r"\b(?:[A-Za-z]+[a-z0-9][A-Z][A-Za-z0-9]*|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+|"
    r"[A-Za-z_]\w*(?:::[A-Za-z_~]\w*)+)\b"
)
_WORD_RE = re.compile(r"[a-z0-9]{5,}")
# words of five letters or more that say nothing about which defect it is
_GENERIC = frozenset(
    "error errors issue about after before cannot could should would which there where while "
    "query queries table tables value values using works working wrong since still returns "
    "return result results first second already reported report exception illegal argument "
    "arguments function functions logical unexpected incorrect invalid unknown server "
    "crash crashes crashed reading writing happens fails failed failing".split()
)
# a fit coefficient at least this large makes its missing input worth naming
NEED_COEF_MIN = 0.5
# (feature the fit weighs, the draft input that feeds it, what to pass)
_NEEDS: tuple[tuple[str, str, str], ...] = (
    ("closeness", "body", "the full report text (error message, stack trace)"),
    ("fix_path", "paths", "the source paths the failure touches"),
    ("frames", "frames", "the stack frames"),
    ("error_code", "body", "the error code (`Code: N`)"),
    ("version", "version", "the version it was seen on"),
    ("component", "component", "the component"),
)
_PUNCT_RE = re.compile(r"[\"'`“”‘’.,;!?()\[\]{}]")
_SPACE_RE = re.compile(r"\s+")
_CLOSEST_KEEP = ("namespace", "kind", "number", "title", "state", "url", "confidence")


def normalize_title(text: str) -> str:
    """Lower case, quotes and sentence punctuation dropped (`::` and `<>`
    kept: they are part of a C++ name), whitespace collapsed."""
    t = _PUNCT_RE.sub(" ", str(text or "").lower())
    return _SPACE_RE.sub(" ", t).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    return round(SequenceMatcher(None, na, nb, autojunk=False).ratio(), 4)


def closest_rows(
    unserved: list[dict[str, Any]],
    draft_title: str,
    *,
    exclude: set[int] | None = None,
    limit: int = CLOSEST_MAX,
) -> list[dict[str, Any]]:
    """The scored-out rows whose title is near-identical to the draft's,
    most similar first, then by confidence; each trimmed to what a reader
    acts on plus its evidence and the similarity that admitted it."""
    if not str(draft_title or "").strip():
        return []
    skip = exclude or set()
    picked: list[tuple[float, float, dict[str, Any]]] = []
    for it in unserved:
        number = int(it.get("number") or 0)
        if number in skip:
            continue
        sim = title_similarity(draft_title, str(it.get("title") or ""))
        if sim < TITLE_SIMILARITY_MIN:
            continue
        picked.append((sim, float(it.get("confidence") or 0.0), it))
    picked.sort(key=lambda t: (-t[0], -t[1]))
    out: list[dict[str, Any]] = []
    for sim, _conf, it in picked[: max(0, int(limit))]:
        row = {k: it.get(k) for k in _CLOSEST_KEEP if it.get(k) not in (None, "", [], {})}
        row["title_similarity"] = sim
        row["evidence"] = list(it.get("evidence") or [])[:3]
        out.append(row)
    return out


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def draft_terms(draft_title: str) -> tuple[list[str], list[str]]:
    """The draft's identifiers (squashed: `ILLEGAL_TYPE_OF_ARGUMENT` and
    `IllegalTypeOfArgument` are one term) and its distinctive words."""
    idents: list[str] = []
    for m in _IDENT_RE.findall(str(draft_title or "")):
        # `DB::ColumnNullable` also stands as `ColumnNullable` in a title
        for part in (m, *m.split("::")[1:]):
            sq = _squash(part)
            if len(sq) >= 6 and sq not in idents:
                idents.append(sq)
    words = [w for w in _WORD_RE.findall(normalize_title(draft_title)) if w not in _GENERIC]
    return idents, list(dict.fromkeys(words))


def on_topic(draft_title: str, title: str) -> bool:
    """A candidate shares a distinctive term of the draft: one of its
    identifiers, or its distinctive words — one when the draft names no
    identifier, two when it does (a draft quoting
    `UNKNOWN_IDENTIFIER` dropped the fused #1 "Cannot DROP COLUMN if there's
    a complex ALIAS in table", whose title repeats the defect, not the code)."""
    idents, words = draft_terms(draft_title)
    if idents and any(i in _squash(title) for i in idents):
        return True
    have = set(_WORD_RE.findall(normalize_title(title)))
    shared = [w for w in words if w in have]
    return len(shared) >= (2 if idents else 1)


# The legs whose first row keeps a nearest slot even when it
# shares no word with the draft — a semantic or a stack-trace match IS the
# topic (on the benchmark's dev half the vector leg's #1 was the filed
# duplicate in 5 of the misses the word filter dropped)
_LEAD_LEGS = ("lexical", "vector", "frames")
# the leaders ride beside the on-topic rows; the whole list stays this short
NEAREST_TOTAL_MAX = 6


def nearest_rows(
    unserved: list[dict[str, Any]],
    draft_title: str,
    *,
    exclude: set[int] | None = None,
    limit: int = NEAREST_MAX,
    legs_top: dict[str, list[int]] | None = None,
) -> list[dict[str, Any]]:
    """The fit's best scored-out issues that are on topic, highest
    confidence first, trimmed to one line's worth each: number, title, state,
    url, confidence and the legs that found it. The first row of the vector
    and frames legs is kept ahead of the rest when it was scored out, on
    topic by words or not."""
    if limit <= 0 or not str(draft_title or "").strip():
        return []
    skip = exclude or set()
    lead = [int(top[0]) for leg in _LEAD_LEGS if (top := list((legs_top or {}).get(leg) or []))]
    issues = [
        it
        for it in unserved
        if str(it.get("kind") or "issue") == "issue" and int(it.get("number") or 0) not in skip
    ]
    by_number = {int(it.get("number") or 0): it for it in issues}
    led = [by_number[n] for n in dict.fromkeys(lead) if n in by_number]
    rows = [
        it for it in issues if it not in led and on_topic(draft_title, str(it.get("title") or ""))
    ]
    rows.sort(key=lambda it: -float(it.get("confidence") or 0.0))
    rows = led + rows
    out: list[dict[str, Any]] = []
    lead_leg = {
        int(top[0]): leg
        for leg in reversed(_LEAD_LEGS)
        if (top := list((legs_top or {}).get(leg) or []))
    }
    # the leaders ride beside the on-topic rows, never instead of one
    for it in rows[: min(int(limit) + len(led), NEAREST_TOTAL_MAX)]:
        row = {k: it.get(k) for k in _NEAREST_KEEP if it.get(k) not in (None, "", [], {})}
        if it in led:  # A rerank's picks never push a leader out
            row["lead"] = lead_leg.get(int(it.get("number") or 0), "")
        conf = row.get("confidence")
        if isinstance(conf, float):
            row["confidence"] = round(conf, 4)
        why = [w for w in (it.get("why") or []) if isinstance(w, str)]
        if why:
            row["why"] = why[:3]
        out.append(row)
    return out


# Leg leaders a cut left out ride after the capped rows,
# at most this many — beside the rerank's picks, never instead of one
LEADERS_EXTRA_MAX = 2


def keep_leaders(rows: list[dict[str, Any]], cap: int) -> list[dict[str, Any]]:
    """`rows` cut to `cap`, then every leg leader (a row with
    `lead`) the cut left out, up to LEADERS_EXTRA_MAX more
    rows. On the benchmark the rerank's picks had pushed the vector leg's #1,
    the filed duplicate, out of the six shown rows; making a leader displace
    the last pick lost three other true issues, so a leader is added instead."""
    if cap <= 0:
        return []
    out = list(rows[:cap])
    extra = [r for r in rows[cap:] if r.get("lead")][:LEADERS_EXTRA_MAX]
    return out + extra


def draft_inputs(q: Any, *, body: str, paths: list[str]) -> dict[str, bool]:
    """Which inputs the draft carried, from the parsed query and the input."""
    return {
        "body": bool(str(body or "").strip()),
        "paths": bool(paths),
        "frames": bool(getattr(q, "frames", None)),
        "version": bool(getattr(q, "versions", None)),
        "component": bool(str(getattr(q, "component", "") or "").strip()),
    }


def needs(fit: dict[str, Any] | None, has: dict[str, bool]) -> list[dict[str, str]]:
    """The inputs the fit rewards (coefficient >= NEED_COEF_MIN) that the
    draft left empty, strongest first; one entry per input."""
    if not fit:
        return []
    coef = dict(fit.get("coef") or {})
    ranked = sorted(_NEEDS, key=lambda n: -float(coef.get(n[0]) or 0.0))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for feature, field, what in ranked:
        if float(coef.get(feature) or 0.0) < NEED_COEF_MIN or has.get(field) or field in seen:
            continue
        seen.add(field)
        out.append({"input": field, "what": what, "feature": feature})
    return out


def note(closest: list[dict[str, Any]], need: list[dict[str, str]], related: float) -> str:
    """One sentence for the reader: the same-titled record, why the fit did
    not call it, and what would let it decide."""
    if not closest:
        return ""
    top = closest[0]
    state = f", {top['state']}" if top.get("state") else ""
    conf = top.get("confidence")
    score = (
        f"confidence {conf:.3f} < related {related:.2f}" if isinstance(conf, int | float) else ""
    )
    head = f"same title as #{top.get('number')}{state}; the fit did not call it a duplicate"
    head += f" ({score})" if score else ""
    if need:
        head += (
            " — a title match alone is weak evidence here; pass "
            + ", ".join(f"`{n['input']}`" for n in need)
            + " so the fit can decide"
        )
    return head
