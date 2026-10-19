"""The triple plane following row supersessions: a retired row's facts move to the
row that retired it, along supersession chains."""

from __future__ import annotations

import logging
import uuid
from typing import Any, NamedTuple

from server.memory.app.graph_holders import _carried_by, _text, stated_by_live_rows
from server.memory.app.projections import (
    end_triples_now,
)
from server.memory.app.triple_projections import (
    _after_the_close,
    _write_follow_plan,
    linked_triples,
)
from server.memory.domain import triples_follow_rows as tfr
from server.memory.domain.temporal_sql import (
    GRAPH_HOLDER_SQL,
)

log = logging.getLogger(__name__)


def _next_link(row: dict[str, Any]) -> str:
    """The id a retired, undeleted row's chain continues at; "" for any other row,
    and for one retired behind no UUID."""
    if str(row.get("status")) != "superseded" or row.get("tombstoned_at") is not None:
        return ""
    return _canonical_id(str(row.get("superseded_by") or "")) or ""


def _canonical_id(value: str) -> str | None:
    """`value` as `toString(memory_id)` spells it, or None when it is not a UUID in
    the dashed or bare hex form ClickHouse parses."""
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return None
    # Python also reads braces, a urn prefix and moved dashes, which ClickHouse does not.
    return str(parsed) if value.lower() in (str(parsed), parsed.hex) else None


# Supersession pairs one read of the rows and their open triples covers.
_FOLLOW_CHUNK = 64
# Links followed past a pair's winner toward the live end of its chain; the repair,
# off the write's path, reads to the end of any chain that ends.
_CHAIN_HOPS = 8
REPAIR_CHAIN_HOPS = 1024


async def _rows_along_chains(
    input: Any, ids: list[str], *, ch: Any, hops: int = _CHAIN_HOPS
) -> dict[str, dict[str, Any]]:
    """The rows `ids` name, and those their `superseded_by` chains lead to, by id. An
    id no row answers (erased) maps to `{}`: it states nothing."""
    by_id: dict[str, dict[str, Any]] = {}
    for _ in range(hops + 1):
        rows = await ch.query_all(
            f"""
            SELECT toString(memory_id) AS id, memory_key, value, user_id,
                   toString(content_sha256) AS content_sha256, valid_at, updated_at,
                   status, superseded_by, tombstoned_at, {GRAPH_HOLDER_SQL} AS holds_facts
            FROM loom.memory FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND memory_id IN {{ids:Array(UUID)}}
            SETTINGS select_sequential_consistency = 1
            """,
            params={"org": input.org, "ns": input.namespace, "ids": ids},
        )
        by_id.update({str(r["id"]): r for r in rows})
        for i in ids:
            by_id.setdefault(i, {})
        ids = sorted({_next_link(r) for r in by_id.values()} - {""} - by_id.keys())
        if not ids:
            break
    return by_id


def _chain_head(row: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Where a chain of retirements starting at `row` ends: a row not retired, a
    deleted one, one retired behind no UUID, or `{}` for one no longer stored. None
    for a cycle, or a chain longer than was read."""
    seen: set[str] = set()
    while link := _next_link(row):
        seen.add(str(row["id"]))
        if link in seen:
            log.warning("triples follow rows: a cycle of retirements through %s", link)
            return None
        if link not in by_id:
            log.warning("triples follow rows: chain longer than was read, at %s", link)
            return None
        row = by_id[link]
    return row


class FollowOutcome(NamedTuple):
    closed: int
    inserted: int
    # Pairs whose winner's chain could not be read to its end (a cycle, or longer
    # than the hops read): not followed.
    unfollowed: list[tuple[str, str]]


async def follow_row_supersessions(
    input: Any, pairs: list[tuple[str, str]], *, ch: Any, hops: int = _CHAIN_HOPS
) -> FollowOutcome:
    """Triples follow rows: for every (superseded row, successor row) pair, close
    the open triples the superseded row attests and the successor does not, and
    write the successor triple under the predecessor's subject and relation when
    the two rows differ by one substituted span. The successor is the live end of
    the winner's chain, so a value retired since never comes back as current and a
    value a live row restates never closes. A pair is followed only while the
    loser is still retired behind that winner (an Undo or a delete may come in
    between when the repair replays it). The rows and open triples are read
    `_FOLLOW_CHUNK` pairs at a time; the successors are then inserted and the
    closes are one UPDATE for the lot. Following a pair again is harmless: a closed
    triple is no longer open, and a successor already open is not repeated."""
    canonical_pairs = [(_canonical_id(lo), _canonical_id(wi)) for lo, wi in pairs]
    followed = [(lo, wi) for lo, wi in canonical_pairs if lo and wi]
    plans: list[tfr.FollowPlan] = []
    unfollowed: list[tuple[str, str]] = []
    for i in range(0, len(followed), _FOLLOW_CHUNK):
        chunk = followed[i : i + _FOLLOW_CHUNK]
        plan, unread = await _plan_chunk(input, chunk, ch=ch, hops=hops)
        plans.append(plan)
        unfollowed += unread
    closing = sorted({t for plan in plans for t in plan.closes})
    plan = tfr.merge_plans(
        plans, kept=await stated_by_live_rows(input.org, input.namespace, closing, ch=ch)
    )
    closed, inserted = await _write_follow_plan(input, plan, ch=ch)
    # Read again, not taken from the plan: a replay plans no close for a fact an
    # attempt closed, and must still end what came after it.
    losers = sorted({lo for lo, _wi in followed})
    linked = sorted({*await linked_triples(input, losers, ch=ch), *plan.closes})
    # A fact the winner still says word for word is held too, linked or not.
    held = await stated_by_live_rows(input.org, input.namespace, linked, ch=ch) | plan.kept
    gone = [t for t in linked if t not in held]
    # A fact the loser stated until later ends now, or when a winner dated ahead
    # starts, as an open one closes; a close's own later end stays.
    await end_triples_now(
        ch,
        org=input.org,
        namespace=input.namespace,
        triples=gone,
        stated_ends_only=True,
        ends=plan.ends,
    )
    await _after_the_close(input, gone, held, ch=ch)
    if closed or inserted:
        log.info(
            "triples follow rows: of %d pair(s), closed %d and wrote %d successor triple(s)",
            len(followed),
            closed,
            inserted,
        )
    return FollowOutcome(closed, inserted, unfollowed)


class _Held(NamedTuple):
    """A pair still retired behind its winner; `head` is the live end of the
    winner's chain, None when that end is deleted or erased."""

    loser: dict[str, Any]
    winner: dict[str, Any]
    head: dict[str, Any] | None


async def _plan_chunk(
    input: Any, pairs: list[tuple[str, str]], *, ch: Any, hops: int = _CHAIN_HOPS
) -> tuple[tfr.FollowPlan, list[tuple[str, str]]]:
    """The `FollowPlan` for `pairs`, and the pairs whose chain could not be read to
    its end: one SELECT per chain link for the rows, one for the rows' links, one
    for the candidate open triples (bounded to the namespace and to the triples
    the rows link or whose subject and object both occur in the pair texts)."""
    ids = sorted({i for pair in pairs for i in pair})
    by_id = await _rows_along_chains(input, ids, ch=ch, hops=hops)
    held: list[_Held] = []
    unread: list[tuple[str, str]] = []
    for loser_id, winner_id in pairs:
        # Still retired behind that winner: not undone, deleted or re-pointed since.
        lo = by_id[loser_id]
        if not lo or _next_link(lo) != winner_id:
            continue
        winner = by_id[winner_id]  # `{}` when erased: nothing holds in the loser's place
        head = _chain_head(winner, by_id)
        if head is None:
            unread.append((loser_id, winner_id))
            continue
        # The loser is judged against what holds now: the live end of its chain. An
        # end that may hold no graph facts (deleted, erased, ended or 'high') attests
        # nothing, so the loser's triples close and none follow.
        held.append(_Held(lo, winner, head if head.get("holds_facts") else None))
    if not held:
        return tfr.FollowPlan(closes=[], inserts=[]), unread
    rows = [r for h in held for r in (h.loser, h.head) if r]
    stated, hashes = await _triples_stated(input, rows, ch=ch)
    holds = await _carried_by(
        input.org, input.namespace, [h.head for h in held if h.head], stated, hashes, ch=ch
    )
    sups = [_supersession(h, stated, hashes, holds) for h in held]
    text = " ".join(f"{s.loser_text} {s.winner_text}" for s in sups)
    linked = sorted(
        {
            t
            for s in sups
            for t in s.loser_triples | s.winner_triples | s.loser_inferred | s.winner_inferred
        }
    )
    open_triples = await ch.query_all(
        """
        SELECT subject, relation, object, confidence, provenance
        FROM loom.entity_triples
        WHERE org = {org:String} AND namespace = {ns:String}
          AND valid_to IS NULL AND invalid_at IS NULL
          -- Lowercase matching, not casefolding: "STRASSE" misses "straße" here
          -- unless a row links it.
          AND ((positionCaseInsensitiveUTF8({txt:String}, subject) > 0
                AND positionCaseInsensitiveUTF8({txt:String}, object) > 0)
               OR (subject, relation, object)
                  IN {linked:Array(Tuple(String, String, String))})
        SETTINGS select_sequential_consistency = 1
        """,
        params={"org": input.org, "ns": input.namespace, "txt": text, "linked": linked},
    )
    return tfr.plan_follow(list(open_triples or []), sups), unread


def _supersession(
    held: _Held,
    stated: dict[str, frozenset[tfr.Triple]],
    hashes: dict[str, dict[tfr.Triple, set[str]]],
    holds: dict[str, frozenset[tfr.Triple]],
) -> tfr.RowSupersession:
    """The pair as the domain planner reads it: the loser attests every successor
    written for it, the winner those it still holds (`holds`)."""
    lo, head = held.loser, held.head or {}
    head_id = str(head.get("id") or "")
    return tfr.RowSupersession(
        loser_text=_text(lo.get("value")),
        winner_text=_text(head.get("value")),
        winner_key=str(head.get("memory_key") or ""),
        winner_valid_from=head.get("valid_at"),
        # Undated: when the winner was written. A replay reads its latest
        # write, so a lock or revision since moves the date.
        loser_valid_to=held.winner.get("valid_at") or held.winner.get("updated_at"),
        winner_id=head_id,
        winner_sha256=str(head.get("content_sha256") or ""),
        winner_user_id=str(head.get("user_id") or ""),
        loser_triples=stated.get(str(lo["id"]), frozenset()),
        winner_triples=stated.get(head_id, frozenset()),
        loser_inferred=frozenset(hashes.get(str(lo["id"]), {})),
        winner_inferred=holds.get(head_id, frozenset()),
    )


async def _triples_stated(
    input: Any, rows: list[dict[str, Any]], *, ch: Any
) -> tuple[dict[str, frozenset[tfr.Triple]], dict[str, dict[tfr.Triple, set[str]]]]:
    """By row id: what the extractor stated for its current value, and the
    successors written for it with the hashes of the values each was written for
    (a revision keeps the id, so they are read whatever value it holds now). A row
    written before rows were linked has neither."""
    keys = sorted({(str(r["id"]), str(r.get("content_sha256") or "")) for r in rows})
    if not keys:
        return {}, {}
    found = await ch.query_all(
        """
        SELECT toString(memory_id) AS id, toString(content_sha256) AS sha, inferred,
               subject, relation, object
        FROM loom.entity_triple_rows
        WHERE org = {org:String} AND namespace = {ns:String}
          AND ((NOT inferred AND (memory_id, content_sha256) IN {keys:Array(Tuple(UUID, String))})
               OR (inferred AND memory_id IN {ids:Array(UUID)}))
        SETTINGS select_sequential_consistency = 1
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "keys": keys,
            "ids": sorted({k[0] for k in keys}),
        },
    )
    stated: dict[str, set[tfr.Triple]] = {}
    hashes: dict[str, dict[tfr.Triple, set[str]]] = {}
    for f in found:
        mid, triple = str(f["id"]), (str(f["subject"]), str(f["relation"]), str(f["object"]))
        if f["inferred"]:
            hashes.setdefault(mid, {}).setdefault(triple, set()).add(str(f["sha"]))
        else:
            stated.setdefault(mid, set()).add(triple)
    return {i: frozenset(t) for i, t in stated.items()}, hashes
