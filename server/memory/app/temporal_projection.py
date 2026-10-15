"""Catching the projections up with the journal.

The publisher applies each commit to the assertion projection inline; when that
fails, the watermark stays behind and this replays from it, in sequence, without
skipping a gap. It also fills the historical-search vectors.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from itertools import takewhile
from typing import Any

import server.memory.store as memory_repo
from server.core.datetimes import naive_utc_now
from server.embedding import get_embedder
from server.erasure.app import fence
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.app import temporal_reads as reads
from server.memory.app.high_fence import fence_high
from server.memory.app.namespace_rollback import stub_embedding_row
from server.memory.app.temporal_rows import (
    Ident,
    Served,
    ident_of,
    projection_row,
    served_from_row,
)
from server.memory.domain import temporal_model as tm
from server.memory.domain.embed_text import stored_embedding_texts
from server.memory.domain.memory_types import is_embedded
from server.memory.domain.temporal_capture import WriteIntent, write_intent
from server.memory.domain.temporal_reads import Axes
from server.memory.store import temporal as store

log = logging.getLogger(__name__)
_BATCH = 1000
# Past the publisher lease (60 s): no insert of an older sequence can still land.
_SETTLED = timedelta(minutes=2)


async def replay(
    org: str, ns: str, applied: int, head: int, *, published_at: datetime | None = None
) -> int:
    """Apply commits (applied, head] to the projection; returns the new watermark.
    Stops below a missing sequence until it settles (then steps over it), and
    never passes an integrity break."""
    _, _, broken = await store.head(org, ns)
    if broken:
        head = min(head, broken - 1)
    upto = min(head, applied + _BATCH)
    commits = await store.commits_between(org, ns, applied, upto)
    if not commits:
        if not _settled(published_at):
            return applied
        await store.advance_watermark(org, ns, upto)
        return upto
    rows: list[dict[str, Any]] = []
    known: dict[str, Served] = {}
    # Each applied commit's id and the assertion ids its rows carry.
    touched: dict[str, list[str]] = {}
    seq = applied
    for c in commits:
        s = int(c["commit_seq"])
        at = tm.utc(c["recorded_at"]) or c["recorded_at"]
        if s != seq + 1 and at > datetime.now(UTC) - _SETTLED:
            break
        seq = s
        ident = ident_of(c)
        retired = [str(x) for x in c["retired_assertion_ids"]]
        missing = [i for i in retired if i not in known]
        for r in await store.assertions_by_id(org, ns, missing):
            known[str(r["assertion_id"])] = served_from_row(r)
        scope = str(c.get("scope") or "private")
        ids = touched.setdefault(str(c["commit_id"]), list(retired))
        for aid in retired:
            prior = known.pop(aid, None)
            if prior is not None:
                rows.append(projection_row(org, ns, scope, ident, prior, s, retired=(s, at)))
        for d in json.loads(c["assertions"]):
            a = tm.Assertion.from_json(d)
            ids.append(a.assertion_id)
            known[a.assertion_id] = Served(a, str(c["memory_id"]), s, at)
            rows.append(projection_row(org, ns, scope, ident, known[a.assertion_id], s))
    if seq > applied:
        await store.insert_assertions(rows)
        await _undo_erased(org, ns, touched)
        await store.advance_watermark(org, ns, seq)
    return seq


async def _undo_erased(org: str, ns: str, touched: dict[str, list[str]]) -> None:
    """The commits read are re-checked once their rows are in: an erasure that
    deleted one meanwhile (it deletes the journal first) has its rows removed here.
    Cleanup only: should this not run, reads still never serve them (the erasure
    cut)."""
    present = {str(r["commit_id"]) for r in await store.commits_by_id(org, ns, list(touched))}
    gone = [aid for cid, ids in touched.items() if cid not in present for aid in ids]
    await store.delete_assertions(org, ns, gone)


async def fill_vectors(model: str, embedder: Any, *, limit: int = 200) -> int:
    """Give each searchable assertion a vector under `model`: the one its
    memory_embeddings row still carries when the version matches, else a fresh
    embed of the same text memory.set embeds."""
    missing = await store.missing_vectors(model, limit=limit)
    by_ns: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in missing:
        by_ns.setdefault((str(r["org"]), str(r["namespace"])), []).append(r)
    written = 0
    for (org, ns), rows in by_ns.items():
        try:
            written += await _fill_namespace(org, ns, rows, model, embedder)
        except Exception as e:  # noqa: BLE001 — one namespace must not stop the others
            log.warning("temporal vectors %s/%s failed, retried next pass: %r", org, ns, e)
    return written


async def _fill_namespace(
    org: str, ns: str, rows: list[dict[str, Any]], model: str, embedder: Any
) -> int:
    current = {
        (str(v["memory_key"]), int(v["version"])): v
        for v in await store.current_vectors(
            org, ns, model, [(str(r["memory_key"]), int(r["source_version"])) for r in rows]
        )
    }
    out: list[dict[str, Any]] = []
    to_embed: list[tuple[dict[str, Any], str]] = []
    for r in rows:
        v = current.get((str(r["memory_key"]), int(r["source_version"])))
        if v is not None:
            out.append(
                _vector_row(org, ns, model, r, list(v["embedding"]), int(v["embedding_dim"]))
            )
        else:
            _, text, _ = stored_embedding_texts(
                str(r["memory_type"]), str(r["value"]), str(r["memory_key"])
            )
            to_embed.append((r, text))
    if to_embed:
        for r, vec in await _embed_each(embedder, to_embed):
            out.append(_vector_row(org, ns, model, r, list(vec), len(vec)))
    await store.insert_vectors(out)
    # An erasure may have removed the assertions while they were embedded.
    await store.drop_unbacked_vectors(org, ns, [str(v["embed_input_sha256"]) for v in out])
    return len(out)


async def _embed_each(
    embedder: Any, items: list[tuple[dict[str, Any], str]]
) -> list[tuple[dict[str, Any], Any]]:
    """One batch; when it fails, one at a time, so a row the embedder refuses
    leaves only itself for the next pass."""
    try:
        vecs = await embedder.embed_many([t for _, t in items])
        return list(zip((r for r, _ in items), vecs, strict=True))
    except Exception as e:  # noqa: BLE001 — retried row by row below
        log.info("temporal vectors: batch embed failed, embedding one by one: %r", e)
    out = []
    for r, text in items:
        try:
            (vec,) = await embedder.embed_many([text])
        except Exception as e:  # noqa: BLE001 — this row is retried next pass
            log.warning("temporal vectors: %s not embedded: %r", r["memory_key"], e)
            continue
        out.append((r, vec))
    return out


def _vector_row(
    org: str, ns: str, model: str, r: dict[str, Any], vec: list[float], dim: int
) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": ns,
        "embedding_model": model,
        "embed_input_sha256": str(r["embed_input_sha256"]),
        "embedding_dim": dim,
        "embedding": vec,
    }


# The reconciler's cursors: per namespace, the sequence its current rows were
# checked up to; and one global valid-time cursor for boundaries coming due.
_CHECKED = "current"
_BOUNDARY = "current_boundary"


def _settled(at: datetime | None) -> bool:
    """Older than any publisher lease: nothing allocated then can still land."""
    return at is not None and (tm.utc(at) or at) < datetime.now(UTC) - _SETTLED


async def reconcile_current(
    *, limit: int = 200, settle_s: int = 120, only: tuple[str, str] | None = None
) -> int:
    """Bring loom.memory rows in line with what the journal says applies now, a
    page per pass from durable cursors; a commit younger than `settle_s` may still
    be landing its row and is left alone. `only` scopes the commit cursor to one
    (org, namespace). Returns rows rewritten."""
    fixed = 0
    # recorded_at is ClickHouse's clock: the cutoff is too.
    settled = await store.server_now() - timedelta(seconds=settle_s)
    for r in await store.unchecked_namespaces(_CHECKED, only=only):
        org, ns = str(r["org"]), str(r["namespace"])
        page = await store.commits_after(org, ns, int(r["applied"]), limit=limit)
        if not page and _settled(r.get("published_at")):
            # Only failed allocations past the cursor: nothing to check.
            await store.advance_watermark(org, ns, int(r["head"]), _CHECKED)
            continue
        done = list(takewhile(lambda c: (tm.utc(c["recorded_at"]) or settled) < settled, page))
        if not done:
            continue
        n = await reconcile_identities(org, ns, sorted({ident_of(c) for c in done}))
        if n is not None:
            fixed += n
            await store.advance_watermark(org, ns, int(done[-1]["commit_seq"]), _CHECKED)
    now = datetime.now(UTC)
    since = _from_us(await store.watermark("", "", _BOUNDARY))
    due = await store.boundaries_after(since, now, limit=limit)
    # A full page stops at its last (whole) instant; the rest comes next pass.
    upto = (tm.utc(due[-1]["boundary"]) or now) if len(due) >= limit else now
    fixed += (await _boundaries(due))[0]
    await store.advance_watermark("", "", _to_us(upto), _BOUNDARY)
    # Namespaces held back earlier catch up from their own place, up to the global
    # cursor, without holding it.
    for p in await store.pending_boundaries():
        org, ns = str(p["org"]), str(p["namespace"])
        rows = await store.boundaries_after(
            _from_us(int(p["since"])), upto, limit=limit, org=org, namespace=ns
        )
        if not rows:
            await store.release_boundary(org, ns)
            continue
        n, held = await _boundaries(rows)
        fixed += n
        if held:
            continue  # still held: `_boundaries` kept its earliest place
        await store.release_boundary(org, ns)
        if len(rows) >= limit:  # a full page: the rest from its last instant
            await store.hold_boundary(org, ns, _to_us(tm.utc(rows[-1]["boundary"]) or now))
    return fixed


async def _boundaries(due: list[dict[str, Any]]) -> tuple[int, set[tuple[str, str]]]:
    """Reconcile the identities whose boundaries came due; returns the rows fixed
    and the namespaces that could not be checked now, each of which keeps its
    earliest boundary as its own pending place."""
    by_ns: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in due:
        by_ns.setdefault((str(r["org"]), str(r["namespace"])), []).append(r)
    fixed, held = 0, set()
    for (org, ns), rows in by_ns.items():
        n = await reconcile_identities(org, ns, [ident_of(r) for r in rows])
        if n is None:
            first = min(tm.utc(r["boundary"]) or datetime.now(UTC) for r in rows)
            await store.hold_boundary(org, ns, _to_us(first) - 1)
            held.add((org, ns))
        fixed += n or 0
    return fixed, held


def _to_us(at: datetime) -> int:
    return int(at.timestamp() * 1_000_000)


def _from_us(us: int) -> datetime:
    return datetime.fromtimestamp(us / 1_000_000, tz=UTC)


# Cutoff gaps that leave what applies now unknown: the reconciler waits them out.
_HEAD_GAPS = ("integrity_break_at", "projection_not_ready", "read_token", "allocation_in_doubt")


async def reconcile_identities(org: str, ns: str, idents: list[Ident]) -> int | None:
    """Rewrite the identities' current rows that diverge; None when the namespace
    cannot be checked now (an erasure holds it, the projection lags, or a write
    failed: it keeps its place and the next pass retries)."""
    try:
        return await _reconcile_ns(org, ns, idents)
    except Exception as e:  # noqa: BLE001 — one namespace must not stop the others
        log.warning("temporal reconcile: %s/%s failed, retried next pass: %r", org, ns, e)
        return None


async def _reconcile_ns(org: str, ns: str, idents: list[Ident]) -> int | None:
    # An erasure deletes the journal, then the current rows: never write back what
    # it is removing.
    if await fence.scope_held(org, ns, sorted({i[1] for i in idents})):
        log.info("temporal reconcile: %s/%s skipped while an erasure runs", org, ns)
        return None
    cut = await reads.cutoff(org, ns, Axes(valid=datetime.now(UTC), known=None))
    # Compared against the projection: only once it covers every commit. A gap about
    # history (data older than the journal, a truncated page) does not touch the head.
    if cut.watermark < cut.seq or any(g.startswith(_HEAD_GAPS) for g in cut.gaps):
        return None
    diverged = [ident_of(r) for r in await store.divergent_rows(org, ns, idents, erased=cut.erased)]
    if not diverged:
        return 0
    fixed = 0
    for ident in diverged:
        # Read by the exact identity: a page cut short by other users' rows of the
        # same key name must never look like "the journal serves nothing".
        agent, uid, sid, key = ident
        seen = len(cut.gaps)
        mine = await reads.served(
            org,
            cut,
            scope=WHOLE_NAMESPACE,
            memory_keys=[key],
            agent=agent,
            user_id=uid,
            session_id=sid,
            include_high=True,
        )
        if any(g.startswith("truncated") for g in cut.gaps[seen:]):
            # ponytail: an identity past the read cap is skipped, not reduced in SQL;
            # its row stays as it is while the namespace's others are repaired.
            log.warning("temporal reconcile: %s/%s %r serves past the read cap", org, ns, ident)
            del cut.gaps[seen:]
            continue
        want = reads.applies_now(mine, cut, key)
        if await _rewrite_current(org, ns, ident, want, cut_seq=cut.seq, plans=mine):
            fixed += 1
    return fixed


async def _rewrite_current(
    org: str,
    ns: str,
    ident: tuple[str, str, str, str],
    want: list[reads.Held],
    *,
    cut_seq: int | None,
    plans: Sequence[reads.Held] = (),
    recheck: bool = True,
) -> bool:
    """One identity's current row onto `want` (newest first; empty = tombstone),
    what applied at journal cutoff `cut_seq`. A commit to the key past the cut is
    newer than `want` and is never overwritten: the next pass decides against a
    later cut. (`cut_seq=None`: an erasure's undo, which removes the row whatever.)"""
    agent, uid, sid, key = ident
    identity = memory_repo.Identity(org=org, namespace=ns, agent=agent, user_id=uid, session_id=sid)
    head = await memory_repo.get_by_key(
        identity, key, scope=WHOLE_NAMESPACE, fresh=True, strong=True
    )
    if cut_seq is not None:
        later = await store.commits_between(org, ns, cut_seq, 2**63 - 1, memory_keys=[key])
        if any(ident_of(c) == ident for c in later):
            return False
    if head is not None and (head.status not in _LIVE or head.tombstoned_at is not None):
        head = None
    if not want:
        # Only plans remain: a row still showing one of them, as a plan, stays until it
        # applies; one showing anything else goes.
        if head is None or _state_of_row(head) in {_state_of(h.a) for h in plans}:
            return False
    elif head is not None and _state_of_row(head) in {_state_of(h.a) for h in want}:
        return False
    now = naive_utc_now()
    if head is not None:
        version = head.version + 1
    else:  # just above the key's tombstone or last committed row: a write drawn since wins
        raw = await store.memory_heads(org, ns, [ident])
        committed = await store.newest_row_versions(org, ns, [ident])
        version = max(
            [1, *(int(r["version"]) + 1 for r in raw), *(v + 1 for v in committed.values())]
        )
    if not want:
        assert head is not None
        base, row = (
            head,
            memory_repo.build_memory_version_row(
                head, version=version, tombstoned_at=now, updated_at=now
            ),
        )
    else:
        h = want[0]
        base = head or _row_of(org, ns, ident, h)
        row = memory_repo.build_memory_version_row(
            base, version=version, updated_at=now, **_columns(h)
        )
    embedder = get_embedder()
    embedded = is_embedded(str(row["memory_type"]), str(row["sensitivity"]))
    stub = stub_embedding_row(
        base.model_copy(update={c: row[c] for c in _STUB_COLS}),
        embedder,
        version=version,
        tombstoned_at=row["tombstoned_at"],
        created_at=now,
    )
    with write_intent(WriteIntent(memory_key=key, journaled=True)):
        await memory_repo.insert_memory_versions(
            [row],
            alongside=[lambda _kept: memory_repo.insert_embedding_rows([stub])] if embedded else [],
        )
    if not embedded:
        retire = [(memory_repo.embedding_key(base), version)]
        await memory_repo.retire_vectors_for_keys(retire)
        if row["sensitivity"] == "high":
            await fence_high(retire)
    log.info("temporal reconcile: %s/%s %s rewritten to the journal", org, ns, key)
    if recheck and want and not await store.last_commits(org, ns, [ident]):
        # An erasure deleted the journal while the row was being written (it
        # deletes the journal first): the row follows it out.
        await _rewrite_current(org, ns, ident, [], cut_seq=None, recheck=False)
    return True


_LIVE = frozenset({"active", "conflicted", "duplicate", "proposed"})


# The row columns its stub embedding row carries.
_STUB_COLS = (
    "memory_type",
    "value",
    "tags",
    "entities",
    "status",
    "confidence",
    "valid_at",
    "valid_to",
    "expires_at",
    "sensitivity",
    "content_sha256",
    "verified_by",
    "verified_sha256",
    "source_class",
)


def _epoch(d: datetime | None) -> int:
    return int((tm.utc(d) or d).timestamp()) if d is not None else 0


def _state_of(a: tm.Assertion) -> tuple[Any, ...]:
    """What a current row must carry to agree with an assertion (the tuple
    `store.divergent_rows` compares in SQL)."""
    return (
        a.content_sha256,
        a.memory_type,
        a.status,
        tuple(a.tags),
        tuple(a.entities),
        int(a.pinned),
        a.lock_mode,
        a.sensitivity,
        a.verified_by,
        a.verified_sha256,
        _epoch(a.verified_at),
        _epoch(tm.utc(tm.row_start(a))),
        _epoch(a.valid_to),
        _epoch(a.expires_at),
        round(a.confidence, 3),
        _epoch(a.lock_until),
    )


def _state_of_row(r: memory_repo.MemoryRow) -> tuple[Any, ...]:
    return (
        r.content_sha256,
        str(r.memory_type),
        str(r.status or "active"),
        tuple(r.tags or ()),
        tuple(r.entities or ()),
        int(r.pinned),
        str(getattr(r, "lock_mode", "") or ""),
        str(getattr(r, "sensitivity", "") or ""),
        str(getattr(r, "verified_by", "") or ""),
        str(getattr(r, "verified_sha256", "") or ""),
        _epoch(r.verified_at),
        _epoch(r.valid_at or r.temporal_anchor),
        _epoch(r.valid_to),
        _epoch(r.expires_at),
        round(float(r.confidence), 3),
        _epoch(getattr(r, "lock_until", None)),
    )


def _columns(h: reads.Held) -> dict[str, Any]:
    return reads.columns_of(h.a)


def _row_of(
    org: str, ns: str, ident: tuple[str, str, str, str], h: reads.Held
) -> memory_repo.MemoryRow:
    """A base row for a key loom.memory never got: the journal's identity and id."""

    agent, uid, sid, key = ident
    a = h.a
    born = tm.utc(h.born_at) or datetime.now(UTC)
    return memory_repo.MemoryRow(
        memory_id=h.memory_id,
        org=org,
        namespace=ns,
        agent=agent,
        user_id=uid,
        session_id=sid,
        memory_key=key,
        memory_type=a.memory_type,
        value=reads.decode_value(a.value),
        value_text=a.value,
        confidence=a.confidence,
        version=0,
        pinned=a.pinned,
        created_at=born,
        last_reinforced_at=born,
        expires_at=tm.utc(a.expires_at),
    )
