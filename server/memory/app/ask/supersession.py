"""Which pool rows are current: superseded and templated-conflict rows leave
the synthesis pool, the chunk and summary sentences that restate them are
masked."""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any

from server.memory.app.ask.identity import memory_identity_where

logger = logging.getLogger(__name__)


# A superseded fact row is not current, and neither is the
# sentence in a verbatim chunk or a session summary that restated it. The
# search returns all three (status IN active, superseded, conflicted; the
# chunk and summary planes never hear about supersession), the reader line
# carries no state, and on questions over updated facts every one of
# twelve single-hop misses answered the retired value from one of them.
_STALE_STOP = frozenset(
    "the a an of in on at to for with by as is are was were be been and or it its this "
    "that these those his her their he she they user users has have had from into".split()
)


_KEY_TAG_RE = re.compile(r"\s*\(#[^)]*\)\s*$")


def _statement_text(text: str) -> str:
    """A fact excerpt on the ask path reads `"<statement>" (#<key>)`; the key
    tag is a citation, not part of the statement, and its tokens would make
    two values look alike (`(#capital_france)` / `(#capital_france_harare)`)."""
    return _KEY_TAG_RE.sub("", text or "").strip().strip('"').strip()


def _stale_tokens(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9]+", _statement_text(text).lower())
        if w not in _STALE_STOP and (len(w) >= 3 or w.isdigit())
    }


def _with_excerpt(h: Any, text: str) -> Any:
    """A copy of the hit with a new excerpt: a pydantic wire hit, a frozen
    dataclass, or any object with a __dict__ (tests)."""
    copier = getattr(h, "model_copy", None)
    if callable(copier):
        return copier(update={"content_excerpt": text})
    try:
        return replace(h, content_excerpt=text)
    except Exception:  # noqa: BLE001 — not a dataclass
        import copy as _copy

        c = _copy.copy(h)
        try:
            c.content_excerpt = text
        except Exception:  # noqa: BLE001 — immutable; leave as it was
            return h
        return c


async def superseded_rows_for_pool(
    search_input: Any, hits: list[Any], *, limit: int = 200
) -> list[Any]:
    """The retired rows behind the pool's fact rows: the
    successor is what ranks and the retired row rarely reaches the top ten,
    so the mask cannot wait for it. A retired row is found by the successor's
    key (same-key supersession) or by its `superseded_by` link to a pool row
    (the judge's cross-key supersession). One FINAL read; fails open."""
    # Only a chunk or a summary can restate a retired value; a fact-only
    # window needs no mask and costs no read.
    if not any(str(getattr(h, "source", "") or "") in ("chunk", "summary") for h in hits):
        return []
    keys = sorted(
        {
            str(getattr(h, "memory_key", "") or "")
            for h in hits
            if str(getattr(h, "source", "fact") or "fact") == "fact"
            and str(getattr(h, "memory_key", "") or "")
        }
    )
    # The successor subquery reads under the same identity as the retired row.
    identity = memory_identity_where(search_input, alias="m")
    successor = memory_identity_where(search_input)
    if not keys or identity is None or successor is None:
        return []
    where, params = identity
    inner = successor[0]
    try:
        from server.core.ch import get_client

        ch = get_client()
        rows = await ch.query_all(
            f"""
            SELECT m.memory_key AS memory_key, m.value AS value,
                   s.value AS successor_value
            FROM loom.memory AS m FINAL
            LEFT JOIN (
                SELECT toString(memory_id) AS mid, value
                FROM loom.memory FINAL
                WHERE {" AND ".join(inner)}
                  AND memory_key IN {{keys:Array(String)}}
                  AND tombstoned_at IS NULL
            ) AS s ON s.mid = m.superseded_by
            WHERE {" AND ".join(where)}
              AND m.status = 'superseded'
              AND m.tombstoned_at IS NULL
              AND (m.memory_key IN {{keys:Array(String)}} OR s.mid != '')
            LIMIT {{lim:UInt32}}
            """,
            params={**params, "keys": keys, "lim": int(limit)},
        )
    except Exception as e:  # noqa: BLE001 — a mask read never blocks an answer
        logger.warning("memory.ask superseded-row read failed (open): %s", e)
        return []
    from types import SimpleNamespace

    return [
        SimpleNamespace(
            memory_key=str(r.get("memory_key") or ""),
            content_excerpt=str(r.get("value") or "").strip('"'),
            source="fact",
            status="superseded",
            successor_text=str(r.get("successor_value") or "").strip('"'),
        )
        for r in rows
    ]


# Two ACTIVE fact rows that state the same single-valued
# attribute with different values — the extractor wrote the update under
# a key the store could not match (`capital_france` beside
# `capital_france_harare`) and the judge did not fold them. The reader
# then sees both and its world-knowledge prior picks. The pool applies the
# store's own newest_wins rule: same template, an attribute noun from the
# single-valued list in the shared tokens, each side a token the other
# lacks → the later-written row stands, the earlier becomes a mask.
_ATTRIBUTE_NOUNS = frozenset(
    "capital ceo chief executive officer author language position citizen citizenship "
    "spouse married marriage president director chairperson head headquarter headquarters "
    "born birthplace religion continent currency employer employed works lives located "
    "role status prime minister chancellor governor mayor coach manager owner nationality "
    "occupation profession official founded founder founders creator created developer "
    "developed city country continent genre music sport associated famous notable known "
    "educated university school died death origin affiliated native speaks state".split()
)


def _ordered_tokens(text: str) -> list[str]:
    return [
        w
        for w in re.findall(r"[a-z0-9]+", _statement_text(text).lower())
        if w not in _STALE_STOP and (len(w) >= 3 or w.isdigit())
    ]


_TEMPLATED_VALUE_MAX = 6


def _write_order(h: Any) -> tuple[str, int]:
    """Write order of a fact row: `created_at` (a date on the wire, so rows
    from one extraction batch tie) then `version`, the row's nanosecond write
    stamp from the ReplacingMergeTree — the store's own newest-wins clock."""
    return (str(getattr(h, "created_at", "") or ""), int(getattr(h, "version", 0) or 0))


def templated_conflict(a_text: str, b_text: str) -> bool:
    """True when two statements are one single-valued attribute of one
    subject with two values: the ordered content tokens agree up to a common
    prefix that names both the attribute (a noun from `_ATTRIBUTE_NOUNS`) and
    the subject (a token that is not such a noun), and the remainders are two
    short, disjoint values. "Hal Needham is a citizen of the USA" and "Robert
    Heinlein is a citizen of the USA" share no prefix; "The CEO of Apple is
    Tim Cook" and "The CEO of Twitter is Bernard Arnault" share only the
    attribute; neither is a conflict. Pure."""
    ta, tb = _ordered_tokens(a_text), _ordered_tokens(b_text)
    if not ta or not tb or ta == tb:
        return False
    p = 0
    while p < len(ta) and p < len(tb) and ta[p] == tb[p]:
        p += 1
    if p < 2:
        return False
    prefix = set(ta[:p])
    if not (prefix & _ATTRIBUTE_NOUNS) or not (prefix - _ATTRIBUTE_NOUNS):
        return False
    s = 0
    while s < len(ta) - p and s < len(tb) - p and ta[len(ta) - 1 - s] == tb[len(tb) - 1 - s]:
        s += 1
    va, vb = ta[p : len(ta) - s], tb[p : len(tb) - s]
    if not va or not vb:
        return False
    if len(va) > _TEMPLATED_VALUE_MAX or len(vb) > _TEMPLATED_VALUE_MAX:
        return False
    return not (set(va) & set(vb))


def resolve_templated_conflicts(hits: list[Any]) -> tuple[list[Any], list[Any]]:
    """(kept, retired): among ACTIVE fact hits, a pair that `templated_conflict`
    reads as one attribute of one subject stated twice; the row written later
    stands. Each retired row carries `successor_text` (the winner's statement)
    so the mask can tell the old value from the shared template. Pure."""
    facts = [
        h
        for h in hits
        if str(getattr(h, "source", "fact") or "fact") == "fact"
        # A `duplicate` row whose value differs from its peer's is a mis-filed
        # update (the Darwin row filed under Dickens); it competes like an
        # active one and, written later, wins.
        and str(getattr(h, "status", "") or "active") in ("active", "duplicate")
    ]
    retired: dict[int, str] = {}
    for i, a in enumerate(facts):
        for b in facts[i + 1 :]:
            if id(a) in retired or id(b) in retired:
                continue
            xa = str(getattr(a, "content_excerpt", "") or "")
            xb = str(getattr(b, "content_excerpt", "") or "")
            if not templated_conflict(xa, xb):
                continue
            older, newer = sorted((a, b), key=_write_order)
            retired[id(older)] = str(getattr(newer, "content_excerpt", "") or "")
    if not retired:
        return hits, []
    from types import SimpleNamespace

    gone = [
        SimpleNamespace(
            memory_key=str(getattr(h, "memory_key", "") or ""),
            content_excerpt=str(getattr(h, "content_excerpt", "") or ""),
            source="fact",
            status="superseded",
            successor_text=retired[id(h)],
        )
        for h in hits
        if id(h) in retired
    ]
    return [h for h in hits if id(h) not in retired], gone


def _drop_superseded(search_input: Any) -> bool:
    """Whether superseded rows leave the pool: yes, unless the call asked for
    the retired rows (`include_superseded`): then they stay in the window,
    labelled, so the answer can say what a value used to be."""
    return not bool(getattr(search_input, "include_superseded", False))


def split_pool_by_state(hits: list[Any]) -> tuple[list[Any], list[Any]]:
    """(current, superseded) — rows whose status is `superseded` leave the
    synthesis pool; they still serve as the masks below. Pure."""
    cur: list[Any] = []
    old: list[Any] = []
    for h in hits:
        (old if str(getattr(h, "status", "") or "") == "superseded" else cur).append(h)
    return cur, old


def mask_stale_sentences(hits: list[Any], superseded: list[Any]) -> tuple[list[Any], int]:
    """Drop from chunk and summary excerpts the sentences that restate a
    superseded row. A sentence restates one when it shares at least three of
    the row's content tokens and at least one token the row's successor
    (the current row under the same key, when in the pool) does not carry —
    the distinguishing value. Fact rows are left alone (they carry state).
    Returns the hits (wire objects copied, not mutated) and the mask count.
    Pure."""
    if not superseded:
        return hits, 0
    current_by_key: dict[str, str] = {}
    for h in hits:
        if str(getattr(h, "source", "fact") or "fact") == "fact":
            current_by_key.setdefault(
                str(getattr(h, "memory_key", "")), str(getattr(h, "content_excerpt", "") or "")
            )
    masks: list[tuple[set[str], set[str], int]] = []
    for o in superseded:
        toks = _stale_tokens(str(getattr(o, "content_excerpt", "") or ""))
        succ_text = str(getattr(o, "successor_text", "") or "") or current_by_key.get(
            str(getattr(o, "memory_key", "")), ""
        )
        succ = _stale_tokens(succ_text)
        # The old value is what the retired row says and its successor does
        # not. A retired row whose successor restates it word for word (a
        # same-key re-extraction) has no old value and masks nothing; without
        # a known successor the whole statement must be restated (four of
        # five tokens), so an update the extractor missed is not masked by
        # the row it should have replaced.
        if succ:
            distinct = toks - succ
            need = 3
        else:
            distinct = toks
            need = max(3, -(-len(toks) * 4 // 5))
        if len(toks) >= 3 and distinct:
            masks.append((toks, distinct, need))
    if not masks:
        return hits, 0
    out: list[Any] = []
    masked = 0
    for h in hits:
        src = str(getattr(h, "source", "fact") or "fact")
        text = str(getattr(h, "content_excerpt", "") or "")
        if src not in ("chunk", "summary") or not text:
            out.append(h)
            continue
        kept: list[str] = []
        changed = False
        for sent in re.split(r"(?<=[.!?])\s+", text):
            st = _stale_tokens(sent)
            stale = any(
                len(st & toks) >= need and (st & distinct) for toks, distinct, need in masks
            )
            if stale:
                changed = True
                masked += 1
                logger.debug(
                    "stale-mask dropped from %s: %s", getattr(h, "memory_key", ""), sent[:160]
                )
                continue
            kept.append(sent)
        if not changed:
            out.append(h)
            continue
        new_text = " ".join(kept).strip() or "[statements superseded by later memories removed]"
        out.append(_with_excerpt(h, new_text))
    return out, masked
