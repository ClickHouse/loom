"""loom.qa_item_embeddings — the vectors loom computes itself for the QA
record's issues and PRs. Written by the `tracker_embed`
pass, read by tracker.similar's vector leg.

Keyed (org, namespace, kind, number), ReplacingMergeTree by version: a pass
re-inserts a row when its text changed (a new `text_sha256`) or when the
item left the window (`tombstoned_at` set), so every reader carries FINAL
and `tombstoned_at IS NULL`, and every writer carries every column (a
version row that omits one zeros it for all readers under FINAL). A
derived index, not a mirror: keys, filter facts and vectors — no title, no
body text; the leg hydrates its hits from the upstream item read. No
vector index: the nearest read is a brute-force `cosineDistance` over the
namespace's live rows with a dim guard and a row cap (~40k rows for the
public ClickHouse record). The column carries the coded layout, so that scan
reads the one-bit codes and rescores a shortlist instead of every full vector.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, TypedDict

from server.core.ch import CHClient, get_client
from server.core.datetimes import naive_utc_now_seconds as now_naive

TABLE = "loom.qa_item_embeddings"
ZERO_SHA = "0" * 64

TRACKER_EMBED_COLS: dict[str, Any] = {
    "org": "",
    "namespace": "",
    "kind": "",
    "number": 0,
    "source_id": "",
    "component": "",
    "is_open": 0,
    "created_at": None,
    "updated_at": None,
    "embedding": [],
    "embedding_dim": 0,
    "embedding_model": "",
    "text_sha256": ZERO_SHA,
    "tombstoned_at": None,
    "version": 0,
}

_EPOCH = datetime(1970, 1, 1)
_COLS = ", ".join(TRACKER_EMBED_COLS)
# the columns a candidate row carries (never the vector: 12 KB a row); the
# title and state of a hit come from the upstream item read, live
_HIT_COLS = "kind, number, source_id, component, is_open, created_at"


class StoredEmbedding(TypedDict):
    """A namespace row without its vector, as the pass compares it."""

    text_sha256: str
    tombstoned_at: datetime | None
    updated_at: datetime | None
    embedding_dim: int
    embedding_model: str


class StoredVector(TypedDict):
    """An item's own stored vector."""

    embedding: list[float]
    embedding_dim: int
    embedding_model: str


class EmbedHit(TypedDict):
    """A nearest-read candidate: the key and filter facts, never the vector."""

    kind: str
    number: int
    source_id: str
    component: str
    is_open: bool
    created_at: datetime | None
    distance: float


class EmbedStats(TypedDict):
    """How much of the table a namespace holds."""

    live: int
    dead: int
    last_updated: datetime | None
    dims: list[int]


def build_embed_row(**overrides: Any) -> dict[str, Any]:
    """A full row with every persisted column present; an unknown key is a
    loud failure (builder / schema drift)."""
    unknown = set(overrides) - set(TRACKER_EMBED_COLS)
    if unknown:
        raise ValueError(f"qa_item_embeddings: unknown columns {sorted(unknown)}")
    row = {k: (list(v) if isinstance(v, list) else v) for k, v in TRACKER_EMBED_COLS.items()}
    row.update(overrides)
    for k in ("created_at", "updated_at"):
        if row.get(k) is None:
            row[k] = _EPOCH
    if not row.get("version"):
        row["version"] = int(time.time_ns())
    if not row.get("embedding_dim"):
        row["embedding_dim"] = len(row.get("embedding") or [])
    return row


def _sha(v: Any) -> str:
    """FixedString(64) comes back as bytes from the driver (never compare
    `str(b'…')` with a hex digest)."""
    if isinstance(v, bytes | bytearray):
        return bytes(v).decode("ascii", "replace")
    return str(v or "")


def _tomb(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return None if v <= _EPOCH else v.replace(tzinfo=None) if v.tzinfo else v
    return None


class TrackerEmbedRepo:
    def __init__(self, ch: CHClient | None = None) -> None:
        self._ch = ch or get_client()

    # -- writes ----------------------------------------------------------------

    async def insert_rows(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        for r in rows:
            missing = set(TRACKER_EMBED_COLS) - set(r)
            if missing:
                raise ValueError(
                    f"qa_item_embeddings: row missing {sorted(missing)} — use build_embed_row()"
                )
        await self._ch.insert(TABLE, rows)

    # -- the pass's reads --------------------------------------------------------

    async def stored(self, org: str, namespace: str) -> dict[tuple[str, int], StoredEmbedding]:
        """Every row of the namespace without its vector: (kind, number) →
        {text_sha256, tombstoned_at, updated_at, embedding_dim,
        embedding_model} — what the pass compares the upstream window
        against (skip an unchanged text, re-embed a changed one, tombstone a
        row whose item left the window)."""
        rows = await self._ch.query_all(
            f"""
            SELECT kind, number, text_sha256, tombstoned_at, updated_at, embedding_dim,
                   embedding_model
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
            """,
            {"org": org, "ns": namespace},
        )
        return {
            (str(r.get("kind") or ""), int(r.get("number") or 0)): StoredEmbedding(
                text_sha256=_sha(r.get("text_sha256")),
                tombstoned_at=_tomb(r.get("tombstoned_at")),
                updated_at=r.get("updated_at"),
                embedding_dim=int(r.get("embedding_dim") or 0),
                embedding_model=str(r.get("embedding_model") or ""),
            )
            for r in rows
        }

    async def stored_digests(
        self, org: str, namespace: str, kind: str, numbers: list[int]
    ) -> dict[tuple[str, int], tuple[str, str]]:
        """(kind, number) → (text digest, embedding model) for the live rows
        of the keys — the query-time catch-up's "already embedded" test (a
        keyed read, no vector): a row another model wrote is not embedded."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})[:500]
        if not nums:
            return {}
        rows = await self._ch.query_all(
            f"""
            SELECT kind, number, text_sha256, embedding_model
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND kind = {{kind:String}} AND number IN {{nums:Array(UInt64)}}
              AND tombstoned_at IS NULL
            """,
            {"org": org, "ns": namespace, "kind": kind, "nums": nums},
        )
        return {
            (str(r.get("kind") or ""), int(r.get("number") or 0)): (
                _sha(r.get("text_sha256")),
                str(r.get("embedding_model") or ""),
            )
            for r in rows
        }

    async def rows_by_key(
        self, org: str, namespace: str, kind: str, numbers: list[int]
    ) -> list[dict[str, Any]]:
        """Full rows (vector included) for the keys a pass tombstones or
        re-activates — the mirror discipline needs every column."""
        nums = sorted({int(n) for n in numbers if int(n) > 0})
        if not nums:
            return []
        rows = await self._ch.query_all(
            f"""
            SELECT {_COLS}
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND kind = {{kind:String}} AND number IN {{nums:Array(UInt64)}}
            """,
            {"org": org, "ns": namespace, "kind": kind, "nums": nums},
        )
        for r in rows:
            r["text_sha256"] = _sha(r.get("text_sha256"))
            r["tombstoned_at"] = _tomb(r.get("tombstoned_at"))
        return rows

    # -- the leg's reads ---------------------------------------------------------

    async def stored_vector(
        self, org: str, namespace: str, kind: str, number: int
    ) -> StoredVector | None:
        """The query item's own stored vector (a point read on the key), so an
        `issue_number` query pays no embedding call; None when the item is
        not in the window."""
        row = await self._ch.query_one(
            f"""
            SELECT embedding, embedding_dim, embedding_model
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND kind = {{kind:String}} AND number = {{n:UInt64}}
              AND tombstoned_at IS NULL AND embedding_dim > 0
            LIMIT 1
            """,
            {"org": org, "ns": namespace, "kind": kind, "n": int(number)},
        )
        if not row or not row.get("embedding"):
            return None
        return StoredVector(
            embedding=[float(x) for x in row["embedding"]],
            embedding_dim=int(row.get("embedding_dim") or 0),
            embedding_model=str(row.get("embedding_model") or ""),
        )

    async def nearest(
        self,
        org: str,
        namespace: str,
        *,
        vector: list[float],
        dim: int,
        limit: int = 50,
        kinds: list[str] | None = None,
        before: datetime | None = None,
        exclude: tuple[str, int] | None = None,
        numbers: list[int] | None = None,
    ) -> list[EmbedHit]:
        """The nearest live items by cosine distance to `vector` among the
        addressed namespace's rows (`org`, `namespace` are always in the
        predicate: a restricted namespace's vectors are read only when it is
        the one addressed — the transport's gate), of the given kinds,
        created before the wall, the query item itself left out. The dim
        guard keeps a model change from mixing two spaces in one cosine. A
        full scan: narrowing to open or same-component items is no faster
        (the vector column is read whole either way; `kind` in the key is
        the lever)."""
        if not vector or int(dim) <= 0:
            return []
        conds = [
            "org = {org:String}",
            "namespace = {ns:String}",
            "tombstoned_at IS NULL",
            "embedding_dim = {dim:UInt16}",
        ]
        params: dict[str, Any] = {
            "org": org,
            "ns": namespace,
            "q": [float(x) for x in vector],
            "dim": int(dim),
            "limit": max(1, min(int(limit), 200)),
        }
        ks = sorted({str(k) for k in (kinds or []) if k in ("issue", "pr")})
        if ks:
            conds.append("kind IN {kinds:Array(String)}")
            params["kinds"] = ks
        if before is not None:
            conds.append("created_at < {before:DateTime}")
            params["before"] = before.replace(tzinfo=None, microsecond=0)
        if numbers is not None:
            # The bisect window's PRs only (0.4 s for ~7k numbers).
            conds.append("number IN {nums:Array(UInt64)}")
            params["nums"] = [int(n) for n in numbers if int(n) > 0]
        if exclude is not None and int(exclude[1]) > 0:
            conds.append("NOT (kind = {xk:String} AND number = {xn:UInt64})")
            params["xk"], params["xn"] = str(exclude[0]), int(exclude[1])
        from server.core.vectors.search_settings import vector_search_settings
        from server.settings import settings

        # The bare `ORDER BY distance ASC LIMIT` shape is what lets the
        # codes-first scan run once the column carries the codec (a tie-break
        # column in the ORDER BY turns it back into a full read); the
        # (kind, number) tie-break moves to the sort below.
        vec_settings = vector_search_settings(
            table=TABLE,
            fetch_multiplier=float(settings.tracker_embed_fetch_multiplier),
        )
        rows = await self._ch.query_all(
            f"""
            SELECT {_HIT_COLS}, cosineDistance(embedding, {{q:Array(Float32)}}) AS distance
            FROM {TABLE} FINAL
            WHERE {" AND ".join(conds)}
            ORDER BY distance ASC
            LIMIT {{limit:UInt32}}
            {vec_settings}
            """,
            params,
        )
        rows.sort(
            key=lambda r: (
                float(r.get("distance") or 0.0),
                str(r.get("kind") or ""),
                int(r.get("number") or 0),
            )
        )
        return [
            EmbedHit(
                kind=str(r.get("kind") or ""),
                number=int(r.get("number") or 0),
                source_id=str(r.get("source_id") or ""),
                component=str(r.get("component") or ""),
                is_open=bool(int(r.get("is_open") or 0)),
                created_at=r.get("created_at"),
                distance=float(r.get("distance") or 0.0),
            )
            for r in rows
        ]

    async def namespace_stats(self, org: str, namespace: str) -> EmbedStats:
        """How much of the table a namespace holds: live rows, tombstoned
        rows, the newest pass stamp, the dims present — the pass outcome and
        the leg's `not_computed` test."""
        row = await self._ch.query_one(
            f"""
            SELECT countIf(tombstoned_at IS NULL) AS live,
                   countIf(tombstoned_at IS NOT NULL) AS dead,
                   max(updated_at) AS last_updated, groupUniqArray(embedding_dim) AS dims
            FROM {TABLE} FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
            """,
            {"org": org, "ns": namespace},
        )
        row = row or {}
        last = row.get("last_updated")
        return EmbedStats(
            live=int(row.get("live") or 0),
            dead=int(row.get("dead") or 0),
            last_updated=last if isinstance(last, datetime) and last > _EPOCH else None,
            dims=sorted(int(d) for d in (row.get("dims") or []) if int(d) > 0),
        )


__all__ = [
    "TRACKER_EMBED_COLS",
    "TABLE",
    "ZERO_SHA",
    "EmbedHit",
    "EmbedStats",
    "StoredEmbedding",
    "StoredVector",
    "TrackerEmbedRepo",
    "build_embed_row",
    "now_naive",
]
