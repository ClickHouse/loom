"""loom.memory_ask_precedents — reusable synthesized answers.

One row per (org, namespace, precedent_key). `lookup` reads the newest
version; `evidence_for` hashes a synthesis window against loom.memory;
`store` writes a fresh precedent; `touch` bumps the reuse counter as a new
version. Every write is best-effort and off the request path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.hashing import hash_text
from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.precedents import Evidence, Precedent

TABLE = "loom.memory_ask_precedents"


async def evidence_for(
    org: str,
    idents: list[tuple[str, str]],
    *,
    ch: _CHClient | None = None,
) -> Evidence:
    """Current content_sha256 of every live (namespace, memory_key) in
    `idents`. A pair with no live row is absent from the result, which the
    validity check reads as a changed window."""
    if not idents:
        return Evidence({})
    ch = ch or _default_client()
    namespaces = sorted({ns for ns, _k in idents})
    keys = sorted({k for _ns, k in idents})
    rows = await ch.query_all(
        """
        SELECT namespace, memory_key, content_sha256
        FROM loom.memory FINAL
        WHERE org = {org:String}
          AND namespace IN {nss:Array(String)}
          AND memory_key IN {keys:Array(String)}
          AND tombstoned_at IS NULL
          AND redacted_at IS NULL
        """,
        params={"org": org, "nss": namespaces, "keys": keys},
    )
    wanted = set(idents)
    out: dict[tuple[str, str], str] = {}
    for r in rows:
        ident = (str(r.get("namespace") or ""), str(r.get("memory_key") or ""))
        if ident in wanted:
            out[ident] = hash_text(r.get("content_sha256"))
    return Evidence(out)


async def lookup(
    org: str, namespace: str, precedent_key: str, *, ch: _CHClient | None = None
) -> Precedent | None:
    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        SELECT precedent_key, question, answer, cited_keys, window_namespaces,
               window_keys, window_sha256, synth_model, reuse_count, created_at,
               retrieved_at
        FROM {TABLE} FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND precedent_key = {{pk:String}}
        LIMIT 1
        """,
        params={"org": org, "ns": namespace, "pk": precedent_key},
    )
    if not row:
        return None
    created = row.get("created_at")
    if isinstance(created, datetime) and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    retrieved = row.get("retrieved_at")
    if isinstance(retrieved, datetime) and retrieved.tzinfo is None:
        retrieved = retrieved.replace(tzinfo=UTC)
    return Precedent(
        # FixedString(64) reads back as bytes; str(b"…") is the repr, 66
        # characters long, which does not fit the column on a touch.
        precedent_key=hash_text(row.get("precedent_key")) or precedent_key,
        question=str(row.get("question") or ""),
        answer=str(row.get("answer") or ""),
        cited_keys=[str(k) for k in (row.get("cited_keys") or [])],
        evidence=Evidence.from_columns(
            [str(x) for x in (row.get("window_namespaces") or [])],
            [str(x) for x in (row.get("window_keys") or [])],
            [str(x) for x in (row.get("window_sha256") or [])],
        ),
        synth_model=str(row.get("synth_model") or ""),
        reuse_count=int(row.get("reuse_count") or 0),
        created_at=created if isinstance(created, datetime) else None,
        retrieved_at=retrieved if isinstance(retrieved, datetime) else None,
    )


async def store(
    *,
    org: str,
    namespace: str,
    precedent_key: str,
    question: str,
    shape_sha256: str,
    answer: str,
    cited_keys: list[str],
    evidence: Evidence,
    synth_model: str = "",
    retrieved_at: datetime | None = None,
    ch: _CHClient | None = None,
) -> None:
    ch = ch or _default_client()
    nss, keys, hashes = evidence.columns()
    row: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        "precedent_key": precedent_key,
        "question": question,
        "shape_sha256": shape_sha256 or "",
        "answer": answer,
        "cited_keys": list(cited_keys),
        "window_namespaces": nss,
        "window_keys": keys,
        "window_sha256": hashes,
        "synth_model": synth_model or "",
        "reuse_count": 0,
        "last_reused_at": None,
    }
    # The moment the window was retrieved: the fast path compares the
    # namespace write marks against it. Absent = never fast.
    if retrieved_at is not None:
        row["retrieved_at"] = retrieved_at.astimezone(UTC).replace(tzinfo=None)
    await ch.insert(TABLE, [row])


async def touch(
    *,
    org: str,
    namespace: str,
    prec: Precedent,
    shape_sha256: str = "",
    retrieved_at: datetime | None = None,
    ch: _CHClient | None = None,
) -> None:
    """Record one reuse as a newer version of the same row. `created_at` is
    carried over so the TTL and the max-age check keep counting from the
    answer's birth, not from its last use. `retrieved_at`, when given, is
    the time of the retrieval that just proved the window byte-identical:
    the proof holds as of that retrieval, so the fast path may measure the
    marks against it from now on."""
    ch = ch or _default_client()
    nss, keys, hashes = prec.evidence.columns()
    row: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        "precedent_key": prec.precedent_key,
        "question": prec.question,
        "shape_sha256": shape_sha256 or "",
        "answer": prec.answer,
        "cited_keys": list(prec.cited_keys),
        "window_namespaces": nss,
        "window_keys": keys,
        "window_sha256": hashes,
        "synth_model": prec.synth_model,
        "reuse_count": int(prec.reuse_count) + 1,
        "last_reused_at": naive_utc_now(),
    }
    if prec.created_at is not None:
        row["created_at"] = prec.created_at.astimezone(UTC).replace(tzinfo=None)
    fresh_at = retrieved_at or prec.retrieved_at
    if fresh_at is not None:
        row["retrieved_at"] = fresh_at.astimezone(UTC).replace(tzinfo=None)
    await ch.insert(TABLE, [row])
