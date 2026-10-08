"""The tracker record's own vector index — what the write side (the `tracker_embed`
pass, `server/tracker/workers/embed.py`) and the read side (tracker.similar's vector
leg) share: the embedded text's recipe and digest, the per-kind window and cursor, the
batch embed, the row builder, and the query-time catch-up that embeds the
items changed upstream since the pass's cursor so an issue filed a minute
ago is in the leg on the very next call.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.hashing import sha256_text as text_sha256
from server.settings import settings

log = logging.getLogger("loom.tracker.embed_index")

KINDS = ("issue", "pr")
CURSOR_KEY = "qa_embed_cursor"  # storage key
# the catch-up reads at most this many changed items per kind and embeds at
# most `hard_cap` of the newest in one call: the pass owns the rest
CATCH_UP_READ = 200
CATCH_UP_EMBED = 50


class EmbedCursor:
    """The per-kind cursor in `loom.code_index_state` (key
    `qa_embed_cursor:<kind>`, value `<ISO 8601 updated_at>|<source_id>`: the
    keyset of the last row embedded). A value with no `|<source_id>` reads as
    id '': the first resumed page re-reads that second — digest skips —
    rather than skipping its remainder."""

    def __init__(self, code_repo: Any = None) -> None:
        self._repo = code_repo

    def _code(self) -> Any:
        if self._repo is None:
            from server.code.app.reads import code_index

            self._repo = code_index()
        return self._repo

    async def get(self, org: str, ns: str, kind: str) -> datetime | None:
        """The cursor's timestamp alone (the leg's catch-up reads `> since`)."""
        return (await self.get_cursor(org, ns, kind))[0]

    async def get_cursor(self, org: str, ns: str, kind: str) -> tuple[datetime | None, str]:
        raw = await self._code().get_state(org, ns, f"{CURSOR_KEY}:{kind}")
        ts, _, source_id = raw.partition("|")
        try:
            return (datetime.fromisoformat(ts), source_id) if ts else (None, "")
        except ValueError:
            return None, ""

    async def set(self, org: str, ns: str, kind: str, value: datetime, source_id: str = "") -> None:
        await self._code().set_state(
            org,
            ns,
            f"{CURSOR_KEY}:{kind}",
            f"{value.replace(tzinfo=None).isoformat(sep=' ')}|{source_id}",
            detail="the (updated_at, source_id) of the last row the tracker_embed pass embedded",
        )

    async def clear(self, org: str, ns: str, kind: str) -> None:
        """Forget the cursor so the next pass walks the
        whole window again — every item whose embedded text changed (a new
        recipe, `tracker_embed_body_chars`) is re-embedded; unchanged digests are
        skipped."""
        await self._code().set_state(
            org,
            ns,
            f"{CURSOR_KEY}:{kind}",
            "",
            detail="cursor cleared: the next pass re-walks the window",
        )


def cursor_state(code_repo: Any = None) -> EmbedCursor:
    """The per-kind cursor reader the leg's catch-up shares with the pass."""
    return EmbedCursor(code_repo)


def window_days(kind: str) -> int:
    """The embedding window per kind: issues stay for
    `tracker_embed_issue_closed_days` after closing — a duplicate's target is an
    older issue, often years older — PRs for `tracker_embed_closed_days`."""
    if kind == "issue":
        return int(settings.tracker_embed_issue_closed_days)
    return int(settings.tracker_embed_closed_days)


def window_days_by_kind() -> dict[str, int]:
    return {k: window_days(k) for k in KINDS}


def embed_text(title: str, body: str, *, body_chars: int) -> str:
    """The text one item is embedded over — the same recipe on the write
    side (the pass) and the read side (a `text` query, an `issue_number`
    query whose item is not in the table, the catch-up)."""
    return f"{(title or '').strip()}\n{(body or '')[: max(0, int(body_chars))].strip()}"


async def embed_many(emb: Any, texts: list[str]) -> list[list[float]]:
    """The embedder's batch API when it has one (the provider's batch limit
    and retry live inside it), else one call per text."""
    if not texts:
        return []
    many = getattr(emb, "embed_many", None)
    if many is not None:
        return list(await many(texts))
    return [await emb.embed(t) for t in texts]


def build_rows(
    org: str,
    ns: str,
    items: list[dict[str, Any]],
    shas: list[str],
    vectors: list[list[float]],
    *,
    model: str,
    version: int | None = None,
) -> list[dict[str, Any]]:
    """Full table rows for embedded items (every column, `build_embed_row`)."""
    from server.tracker.store.embed import build_embed_row

    if not (len(items) == len(shas) == len(vectors)):
        raise RuntimeError(
            f"embed_index: {len(items)} items, {len(shas)} digests, {len(vectors)} vectors"
        )
    ver = version or int(time.time_ns())
    return [
        build_embed_row(
            org=org,
            namespace=ns,
            kind=it["kind"],
            number=int(it["number"]),
            source_id=it["source_id"],
            component=str(it.get("component") or ""),
            is_open=0 if it.get("closed_at") else 1,
            created_at=it.get("created_at"),
            updated_at=it.get("updated_at"),
            embedding=[float(x) for x in vec],
            embedding_dim=len(vec),
            embedding_model=model,
            text_sha256=sha,
            tombstoned_at=None,
            version=ver,
        )
        for it, sha, vec in zip(items, shas, vectors, strict=True)
    ]


def cosine_distance(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


@dataclass
class CatchUp:
    """What the query-time catch-up did: the fresh candidates (with their
    vectors), how many it embedded now, how many changed items it left to
    the pass (beyond the cap), how many were already stored."""

    hits: list[dict[str, Any]] = field(default_factory=list)
    embedded: int = 0
    left_to_pass: int = 0
    status: str = "skipped"  # skipped | served | tracker_unavailable


def _days_for(closed_days: int | Mapping[str, int], kind: str) -> int:
    """One window for every kind, or one per kind."""
    if isinstance(closed_days, Mapping):
        return int(closed_days.get(kind, closed_days.get("pr", 365)))
    return int(closed_days)


async def catch_up(
    src: Any,
    repo: Any,
    emb: Any,
    org: str,
    ns: str,
    *,
    kinds: list[str],
    cursors: dict[str, datetime | None],
    closed_days: int | Mapping[str, int],
    body_chars: int,
    read_cap: int = CATCH_UP_READ,
    embed_cap: int = CATCH_UP_EMBED,
) -> CatchUp:
    """The items of `kinds` changed upstream since the pass's
    per-kind cursor (same privacy predicate, the source layer), their text
    embedded in ONE batched call (the newest `embed_cap` when more changed;
    the pass owns the rest), inserted so the next call does not pay again,
    and returned as candidates with their vectors for the in-process
    cosine. A kind without a cursor is skipped: the pass has never run for
    it and 200 arbitrary items would not be an answer. Any upstream failure
    is `tracker_unavailable` on the catch-up alone — the table's answer stands."""
    from server.tracker.domain.errors import TrackerUnavailable

    out = CatchUp()
    changed: list[dict[str, Any]] = []
    reads = [
        src.embed_items(
            org,
            ns,
            kind=kind,
            since=cursors[kind],
            closed_days=_days_for(closed_days, kind),
            body_chars=body_chars,
            limit=read_cap,
        )
        for kind in KINDS
        if kind in kinds and cursors.get(kind) is not None
    ]
    try:
        for page in await asyncio.gather(*reads):  # the kinds side by side
            changed.extend(page)
    except TrackerUnavailable as e:
        log.info("tracker.similar/vector catch-up on %s: tracker_unavailable (%s)", ns, e)
        out.status = "tracker_unavailable"
        return out
    if not changed:
        out.status = "served"
        return out
    changed.sort(key=lambda it: it.get("updated_at") or datetime.min, reverse=True)
    picked, out.left_to_pass = changed[:embed_cap], max(0, len(changed) - embed_cap)
    texts = [embed_text(it["title"], it["body"], body_chars=body_chars) for it in picked]
    shas = [text_sha256(t) for t in texts]
    model = str(getattr(emb, "model_name", ""))
    stored: dict[tuple[str, int], tuple[str, str]] = {}
    by_kind = {
        k: [int(it["number"]) for it in picked if it["kind"] == k]
        for k in sorted({it["kind"] for it in picked})
    }
    for got in await asyncio.gather(
        *(repo.stored_digests(org, ns, k, nums) for k, nums in by_kind.items())
    ):
        stored.update(got)
    todo = [
        i
        for i, it in enumerate(picked)
        if stored.get((it["kind"], int(it["number"]))) != (shas[i], model)
    ]
    if not todo:
        out.status = "served"
        return out
    vectors = await embed_many(emb, [texts[i] for i in todo])
    items = [picked[i] for i in todo]
    rows = build_rows(org, ns, items, [shas[i] for i in todo], vectors, model=model)
    try:
        await repo.insert_rows(rows)
    except Exception as e:  # noqa: BLE001 — the answer stands; the pass writes the row
        log.warning("tracker.similar/vector catch-up on %s: insert failed (%s)", ns, e)
    out.embedded = len(rows)
    out.hits = [
        {
            "kind": it["kind"],
            "number": int(it["number"]),
            "source_id": it["source_id"],
            "component": str(it.get("component") or ""),
            "is_open": not it.get("closed_at"),
            "created_at": it.get("created_at"),
            "embedding": vec,
        }
        for it, vec in zip(items, vectors, strict=True)
    ]
    out.status = "served"
    return out


def rank_fresh(
    hits: list[dict[str, Any]],
    query: list[float],
    *,
    before: datetime | None,
    exclude: tuple[str, int] | None,
) -> list[dict[str, Any]]:
    """The catch-up's candidates under the same rules the table scan
    applies: the dim guard, the wall, the query item left out; distance in
    process."""
    out: list[dict[str, Any]] = []
    for h in hits:
        vec = h.get("embedding") or []
        if len(vec) != len(query):
            continue
        created = h.get("created_at")
        if before is not None and isinstance(created, datetime) and created >= before:
            continue
        if exclude is not None and (h["kind"], int(h["number"])) == (exclude[0], int(exclude[1])):
            continue
        out.append(
            {k: v for k, v in h.items() if k != "embedding"}
            | {"distance": cosine_distance(query, vec)}
        )
    return out


def merge_fresh(
    rows: list[dict[str, Any]],
    extra: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """The table's hits and the catch-up's fresh candidates as one list by
    distance: a fresh row wins over the table's older row for the same
    key (its text is the newer), cut to `limit`."""
    fresh = {(str(h["kind"]), int(h["number"])): h for h in extra}
    out = [r for r in rows if (str(r["kind"]), int(r["number"])) not in fresh]
    out.extend(fresh.values())
    out.sort(key=lambda h: (float(h["distance"]), str(h["kind"]), int(h["number"])))
    return out[: max(1, int(limit))]


__all__ = [
    "CATCH_UP_EMBED",
    "CATCH_UP_READ",
    "KINDS",
    "CatchUp",
    "build_rows",
    "catch_up",
    "cosine_distance",
    "embed_many",
    "embed_text",
    "merge_fresh",
    "rank_fresh",
    "text_sha256",
]
