"""`loom import-clickgap`: a ClickGap review-lesson SQLite export → Loom."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

# --- ClickGap import helpers (pure; unit-tested) ----------------------
# Map a source export's review-lesson schema onto Loom's flat memory schema
# (typed entities, suppression polarity, provenance class).

# Sources that carry person identifiers — flagged so erasure can target by tag.
_CLICKGAP_HUMAN_SOURCES: frozenset[str] = frozenset({"operator", "user_feedback", "ui"})

# Drives the dry-run crosstab only; the importer preserves per-row expires_at
# verbatim regardless.
_CLICKGAP_PERMANENT_KINDS: frozenset[str] = frozenset(
    {
        "principle",
        "false_positive",
        "test_pattern",
        "fix_recipe",
        "decision",
        "code_pattern",
        "cidb_insight",
    }
)


def _clickgap_classify_source(source: str) -> tuple[str, bool]:
    """Map a raw `source` to (provenance_class, is_human).

    Classes: session (run-scoped), human (person-attributed,
    erasure-relevant), reviewer, eval, pipeline (default).
    """
    s = (source or "").strip()
    if s.startswith("session_index:"):
        return "session", False
    if s.startswith("mention:") or s.startswith("maintainer") or s in _CLICKGAP_HUMAN_SOURCES:
        return "human", True
    if s.startswith("reviewer"):
        return "reviewer", False
    if s.startswith(("review:", "prompt", "audit", "dismissal", "learning")):
        return "eval", False
    return "pipeline", False


def _clickgap_source_trace(source: str) -> str:
    """Provenance URI for loom.memory.source_trace; keeps the full source."""
    cls, _ = _clickgap_classify_source(source)
    return f"clickgap://{cls}/{source}" if source else "clickgap://pipeline/unknown"


def _clickgap_session_id(source: str) -> str:
    """session_index:<id> rows are session-scoped → map the embedded id onto
    the identity tuple's session_id; everything else uses the default '-'."""
    s = (source or "").strip()
    if s.startswith("session_index:"):
        return s.split(":", 1)[1] or "-"
    return "-"


def _clickgap_tags(
    *,
    kind: str,
    source: str,
    typed_entities: list[tuple[str, str]],
) -> list[str]:
    """Build the tag set that preserves what the flat schema can't:

    - kind:<kind>            lesson kind (sole surviving copy when memory_type
                             is remapped, and the multi-kind search filter).
    - source:<class>         provenance class (filterable).
    - pii:human-attributed   erasure candidates.
    - polarity:negative      suppression memory ('do NOT re-raise'); honoured
                             by the consolidation guard until a first-class
                             polarity column lands.
    - entity:<kind>:<text>   typed entity (loom.memory.entities is untyped).
    """
    cls, is_human = _clickgap_classify_source(source)
    tags: list[str] = [f"source:{cls}"]
    if kind:
        tags.append(f"kind:{kind}")
    if is_human:
        tags.append("pii:human-attributed")
    if kind == "false_positive":
        tags.append("polarity:negative")
    seen = set(tags)
    for ent, ent_kind in typed_entities:
        if not ent:
            continue
        tag = f"entity:{ent_kind or 'entity'}:{ent}"
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def _clickgap_memory_id(org: str, namespace: str, lesson_id: object) -> str:
    """Deterministic UUIDv5 per lesson so (a) memory_id is stable across
    re-imports — idempotent under ReplacingMergeTree(version) — and (b) the
    usage/totals rows can reference it without reading back a generated UUID."""
    import uuid

    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"clickgap://{org}/{namespace}/lesson/{lesson_id}",
        )
    )


def _clickgap_value_json(text: str, reasoning: str | None) -> str:
    """Pack assertion + justification into value_json. get returns value_json
    AS value when populated, so the assertion must survive here. The
    embedding's content_excerpt is the assertion alone — search embeds the
    claim, not the justification."""
    import json as _json

    return _json.dumps(
        {"text": text or "", "reasoning": reasoning or ""},
        ensure_ascii=False,
    )


# Lesson kind → memory_type. Mostly 1:1; summary → episodic (non-searchable)
# reproduces the source's snapshot exclusion — summaries stay reachable by
# get/list but never rank in search. Original kind survives as a kind:<> tag.
_CLICKGAP_TYPE_REMAP: dict[str, str] = {"summary": "episodic"}


def _clickgap_memory_type(kind: str) -> str:
    """Resolve a lesson kind to its memory_type.

    Guards against an unintended collision: a kind equal to a non-searchable
    type would silently drop from search; fail loudly so a future kind rename
    is caught rather than vanishing from recall.
    """
    from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES

    k = kind or "semantic"
    if k in _CLICKGAP_TYPE_REMAP:
        return _CLICKGAP_TYPE_REMAP[k]
    if k in NON_SEARCHABLE_TYPES:
        raise ValueError(
            f"kind {k!r} maps onto non-searchable memory_type {k!r} and would "
            f"be silently excluded from memory.search; add an explicit entry "
            f"to _CLICKGAP_TYPE_REMAP if that exclusion is intended."
        )
    return k


def _clickgap_parse_dt(v: object) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# Canonicalize names so 'Summary'/'summary' collapse to one category_id.
def _clickgap_canon_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


async def _clickgap_embed_texts(embedder: Any, texts: list[str]) -> list[list[float]]:
    many = getattr(embedder, "embed_many", None)
    if many is not None:
        return cast("list[list[float]]", await many(texts))
    return [await embedder.embed(t) for t in texts]


def _clickgap_count_source(sdb: sqlite3.Connection, sqlite_path: str) -> dict[str, int] | None:
    """Count the five source tables and print the source summary.

    None means the export holds no lessons: the source is closed, the reason is
    on stderr, and the command exits 2.
    """
    counts: dict[str, int] = {}
    for tbl in (
        "lessons",
        "lesson_entities",
        "lesson_categories",
        "lesson_category_items",
        "lesson_recalls",
    ):
        try:
            counts[tbl] = sdb.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[tbl] = 0
    print(
        f"import-clickgap source: {sqlite_path}\n"
        f"  lessons={counts['lessons']} entities={counts['lesson_entities']} "
        f"categories={counts['lesson_categories']} "
        f"category-links={counts['lesson_category_items']} "
        f"recalls={counts['lesson_recalls']}"
    )
    if counts["lessons"] == 0:
        print("ERROR: no lessons in source — aborting.", file=sys.stderr)
        sdb.close()
        return None
    return counts


def _clickgap_preload(
    sdb: sqlite3.Connection, org: str, ns: str
) -> tuple[
    dict[int, list[tuple[str, str]]], list[Any], dict[str, dict[str, str]], dict[int, list[str]]
]:
    """Read the small supporting tables into lookups keyed by lesson.

    Returns (entities_by_lesson, cat_rows, cat_meta_by_loom_id, links_by_lesson):
    typed entities per lesson, the raw category rows (counted by the dry run),
    canonicalized categories by Loom category_id, and the Loom category_ids
    each lesson links to.
    """
    # Preload supporting tables (small).
    entities_by_lesson: dict[int, list[tuple[str, str]]] = {}
    for r in sdb.execute("SELECT lesson_id, entity, entity_kind FROM lesson_entities").fetchall():
        entities_by_lesson.setdefault(int(r["lesson_id"]), []).append(
            (str(r["entity"] or ""), str(r["entity_kind"] or ""))
        )

    cat_rows = sdb.execute(
        "SELECT id, name, description, summary FROM lesson_categories"
    ).fetchall()
    loom_cat_id_by_src: dict[int, str] = {}
    cat_meta_by_loom_id: dict[str, dict[str, str]] = {}
    for r in cat_rows:
        cname = _clickgap_canon_name(r["name"])
        if not cname:
            continue
        loom_id = hashlib.sha256(f"{org}\x00{ns}\x00{cname}".encode()).hexdigest()[:32]
        loom_cat_id_by_src[int(r["id"])] = loom_id
        # First writer wins for display name/description.
        cat_meta_by_loom_id.setdefault(
            loom_id,
            {
                "name": r["name"] or cname,
                "description": r["description"] or "",
                "summary": r["summary"] or "",
            },
        )

    links_by_lesson: dict[int, list[str]] = {}
    for r in sdb.execute("SELECT lesson_id, category_id FROM lesson_category_items").fetchall():
        linked_id = loom_cat_id_by_src.get(int(r["category_id"]))
        if linked_id:
            links_by_lesson.setdefault(int(r["lesson_id"]), []).append(linked_id)
    return entities_by_lesson, cat_rows, cat_meta_by_loom_id, links_by_lesson


async def _clickgap_dry_run_report(
    lesson_rows: list[Any],
    cat_rows: list[Any],
    cat_meta_by_loom_id: dict[str, dict[str, str]],
    entities_by_lesson: dict[int, list[tuple[str, str]]],
    embedder: Any,
    live_model: str,
) -> None:
    """--dry-run: print the kind/TTL crosstab, parse checks and category/entity
    counts, and probe the embedder. Writes nothing."""
    kind_ttl: dict[str, list[int]] = {}
    unparsable = 0
    no_reasoning = 0
    for r in lesson_rows:
        k = str(r["kind"] or "")
        has_ttl = 1 if (r["expires_at"] not in (None, "")) else 0
        kind_ttl.setdefault(k, [0, 0])
        kind_ttl[k][has_ttl] += 1
        if _clickgap_parse_dt(r["created_at"]) is None:
            unparsable += 1
        if not (r["reasoning"] or "").strip():
            no_reasoning += 1
    print("  kind → memory_type + TTL crosstab [permanent / ttl'd]:")
    for k in sorted(kind_ttl, key=lambda x: -sum(kind_ttl[x])):
        perm, ttl = kind_ttl[k]
        mt = _clickgap_memory_type(k)
        remap = f" → {mt} (NON-SEARCHABLE: get/list only)" if mt != k else ""
        flag = " (expected permanent)" if k in _CLICKGAP_PERMANENT_KINDS else ""
        print(f"    {k:<22} permanent={perm:<5} ttl'd={ttl}{remap}{flag}")
    print(f"  unparsable created_at rows: {unparsable}")
    print(f"  rows missing reasoning: {no_reasoning}")
    print(
        f"  categories (canonicalized): {len(cat_meta_by_loom_id)} "
        f"(from {len(cat_rows)} source rows)"
    )
    print(f"  distinct entities: {len({e for v in entities_by_lesson.values() for e, _ in v})}")
    # Embedder reachability probe.
    try:
        probe = await _clickgap_embed_texts(embedder, [str(lesson_rows[0]["text"] or "x")])
        print(f"  embedder: {live_model} (dim={len(probe[0]) if probe else 0}) — reachable")
    except Exception as e:  # noqa: BLE001 — the dry run reports an unreachable embedder
        print(f"  embedder: {live_model} — UNREACHABLE: {e}", file=sys.stderr)
    print("(dry-run; no writes)")


@dataclass(slots=True)
class _ClickgapWrite:
    """Request-scoped state the import-clickgap write phases share: the
    destination tuple, the CH client, the embedder, and the per-run clock and
    version stamped on every row."""

    ch: Any
    org: str
    ns: str
    agent: str
    batch_size: int
    embedder: Any
    live_model: str
    now: datetime
    run_version: int
    # Provenance of every lesson row: the CLI's pipeline actor, and `imported`
    # (copied from another system, like okf's rows).
    actor: str = ""
    source_class: str = "imported"
    # Filled by the lesson windows; read by the entity-vector and link phases.
    # (entity_text, memory_key)
    entity_vec_jobs: list[tuple[str, str]] = field(default_factory=list)
    link_inserts: list[dict[str, Any]] = field(default_factory=list)
    # Per-row tallies of the lesson windows, printed in the lessons summary.
    polarity_neg: int = 0
    non_searchable: int = 0  # rows remapped to a non-searchable type
    unparsable_created: int = 0  # created_at failed to parse → fell back to now


async def _clickgap_write_categories(
    w: _ClickgapWrite, cat_meta_by_loom_id: dict[str, dict[str, str]]
) -> None:
    """Embed each canonical category and insert it into loom.categories."""
    ch, org, ns, embedder = w.ch, w.org, w.ns, w.embedder
    live_model, run_version, now = w.live_model, w.run_version, w.now
    # 1. Categories → loom.categories.
    if cat_meta_by_loom_id:
        cat_ids = list(cat_meta_by_loom_id.keys())
        cat_vecs = await _clickgap_embed_texts(
            embedder,
            [
                f"{cat_meta_by_loom_id[c]['name']}\n{cat_meta_by_loom_id[c]['description']}".strip()
                for c in cat_ids
            ],
        )
        cat_inserts = [
            {
                "org": org,
                "namespace": ns,
                "category_id": cid,
                "name": cat_meta_by_loom_id[cid]["name"],
                "description": cat_meta_by_loom_id[cid]["description"],
                "summary": cat_meta_by_loom_id[cid]["summary"],
                "embedding": vec,
                "embedding_model": live_model,
                "embedding_dim": len(vec),
                "version": run_version,
                "created_at": now,
                "tombstoned_at": None,
            }
            for cid, vec in zip(cat_ids, cat_vecs, strict=False)
        ]
        await ch.insert("loom.categories", cat_inserts)
    print(f"  ✓ {len(cat_meta_by_loom_id)} categories")


async def _clickgap_map_lesson_window(
    w: _ClickgapWrite,
    window: list[Any],
    entities_by_lesson: dict[int, list[tuple[str, str]]],
    links_by_lesson: dict[int, list[str]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Embed one window of lessons and map each onto its loom.memory row and
    (searchable kinds only) its loom.memory_embeddings row.

    Returns (mem_batch, emb_batch) for _run to insert. Entity-vector jobs,
    category links and the per-row tallies accumulate on `w`.
    """
    from server.core.hashing import sha256_value
    from server.memory.domain.memory_types import is_searchable_type
    from server.memory.store import build_embedding_row

    org, ns, agent, embedder = w.org, w.ns, w.agent, w.embedder
    live_model, run_version, now = w.live_model, w.run_version, w.now
    excerpts = [str(r["text"] or "")[:512] for r in window]
    vecs = await _clickgap_embed_texts(embedder, excerpts)
    mem_batch: list[dict[str, Any]] = []
    emb_batch: list[dict[str, Any]] = []
    for r, excerpt, vec in zip(window, excerpts, vecs, strict=False):
        lid = int(r["id"])
        text = str(r["text"] or "")
        kind = str(r["kind"] or "semantic")
        mtype = _clickgap_memory_type(kind)
        searchable = is_searchable_type(mtype)
        if not searchable:
            w.non_searchable += 1
        typed = entities_by_lesson.get(lid, [])
        bare_entities: list[str] = []
        _seen_ent: set[str] = set()
        for ent, _ in typed:
            if ent and ent not in _seen_ent:
                _seen_ent.add(ent)
                bare_entities.append(ent)
        tags = _clickgap_tags(kind=kind, source=str(r["source"] or ""), typed_entities=typed)
        if "polarity:negative" in tags:
            w.polarity_neg += 1
        created = _clickgap_parse_dt(r["created_at"])
        if created is None:
            # Fall back to import time, but count it — otherwise an
            # unparseable created_at silently gets a fresh now() every
            # run, breaking partition/recency determinism.
            w.unparsable_created += 1
            created = now
        expires = _clickgap_parse_dt(r["expires_at"])
        last_recalled = _clickgap_parse_dt(r["last_recalled_at"])
        rc = int(r["recall_count"] or 0)
        ec = int(r["evidence_count"] or 1)
        mem_id = _clickgap_memory_id(org, ns, lid)
        memory_key = f"lesson_{lid}"
        session_id = _clickgap_session_id(str(r["source"] or ""))
        src_res = f"clickgap:lesson:{lid}"
        trace = _clickgap_source_trace(str(r["source"] or ""))
        value_json = _clickgap_value_json(text, r["reasoning"])
        # The hash memory.set stores for this value (`value` is json.dumps(text)),
        # so an if_match_sha256 read from list/search matches the row.
        sha = sha256_value(text)
        last_reinforced = last_recalled or created
        # Non-searchable rows get no embedding row nor entity-ANN
        # vectors, or they'd resurface in search and defeat the exclusion.
        if searchable:
            for ent in bare_entities:
                w.entity_vec_jobs.append((ent, memory_key))
        mem_batch.append(
            {
                "org": org,
                "namespace": ns,
                "agent": agent,
                "user_id": "-",
                "session_id": session_id,
                "run_id": "-",
                "memory_key": memory_key,
                "memory_type": mtype,
                # JSON-encode: `value` is read back via json.loads on the
                # list/bulk_get paths, so a bare string would raise there.
                "value": json.dumps(text),
                "value_json": value_json,
                "confidence": 1.0,
                "source_trace": trace,
                "source_resource_id": src_res,
                "actor": w.actor,
                "source_class": w.source_class,
                "version": run_version,
                "tombstoned_at": None,
                "expires_at": expires,
                "valid_at": created,
                "temporal_anchor": created,
                "char_limit": None,
                "pinned": 0,
                "status": "active",
                "memory_id": mem_id,
                "tags": tags,
                "entities": bare_entities,
                "created_at": created,
                "updated_at": created,
                "content_sha256": sha,
                "evidence_count": ec,
                "recall_count": rc,
                "last_recalled_at": last_recalled,
                "reinforcement_count": rc,
                "last_reinforced_at": last_reinforced,
            }
        )
        if vec and searchable:
            emb_batch.append(
                build_embedding_row(
                    org=org,
                    namespace=ns,
                    agent=agent,
                    user_id="-",
                    session_id=session_id,
                    scope="private",
                    memory_key=memory_key,
                    memory_type=mtype,
                    content_excerpt=excerpt,
                    tags=tags,
                    entities=bare_entities,
                    embedding_model=live_model,
                    embedding_dim=len(vec),
                    embedding=vec,
                    confidence=1.0,
                    tombstoned_at=None,
                    created_at=created,
                    valid_at=created,
                    temporal_anchor=created,
                    expires_at=expires,
                    reinforcement_count=rc,
                    last_reinforced_at=last_reinforced,
                    source_resource_id=src_res,
                    source_class=w.source_class,
                    version=run_version,
                )
            )
        for cid in links_by_lesson.get(lid, []):
            w.link_inserts.append(
                {
                    "org": org,
                    "namespace": ns,
                    "memory_key": memory_key,
                    "category_id": cid,
                    "created_at": now,
                    "tombstoned_at": None,
                }
            )
    return await _clickgap_carry_forward(w, mem_batch), emb_batch


# Columns a re-run takes from the existing live row instead of the import's
# first-version defaults: a pin, a review verdict and a char limit are
# loom-side decisions the export knows nothing of. Every column the import does
# not set at all (attestation, locks, conflict links, sensitivity, ...) is
# carried forward too.
_CLICKGAP_LOOM_OWNED = frozenset({"pinned", "status", "char_limit"})


async def _clickgap_carry_forward(
    w: _ClickgapWrite, mem_batch: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """A lesson already imported gets its new version through
    build_memory_version_row, so a re-run refreshes the lesson's own columns
    and keeps every other one; a new lesson gets its first-version row
    completed to every persisted column (build_first_version_row)."""
    from server.memory.store import (
        build_first_version_row,
        build_memory_version_row,
        identity_key,
        memory_rows_by_identity_keys,
    )

    existing = await memory_rows_by_identity_keys([identity_key(r) for r in mem_batch], ch=w.ch)
    out: list[dict[str, Any]] = []
    for r in mem_batch:
        prior = existing.get(identity_key(r))
        if prior is None:
            out.append(build_first_version_row(r, now=w.now))
            continue
        overrides = {k: v for k, v in r.items() if k not in _CLICKGAP_LOOM_OWNED}
        out.append(build_memory_version_row(prior, **overrides))
    return out


async def _clickgap_write_entity_vectors(w: _ClickgapWrite) -> None:
    """Embed the entities of searchable lessons into loom.entity_vectors."""
    ch, org, ns, agent, embedder = w.ch, w.org, w.ns, w.agent, w.embedder
    live_model, now, batch_size = w.live_model, w.now, w.batch_size
    # 3. Entity vectors — embed each distinct entity once, emit one row
    #    per (entity, memory_key) so the entity-ANN boost has provenance.
    distinct_entities = sorted({e for e, _ in w.entity_vec_jobs})
    ev_total = 0
    if distinct_entities:
        evecs: dict[str, list[float]] = {}
        for start in range(0, len(distinct_entities), batch_size):
            chunk = distinct_entities[start : start + batch_size]
            done = await _clickgap_embed_texts(embedder, chunk)
            for e, v in zip(chunk, done, strict=False):
                evecs[e] = v
        ev_batch: list[dict[str, Any]] = []
        for ent, memory_key in w.entity_vec_jobs:
            vec = evecs.get(ent) or []
            if not vec:
                continue
            ev_batch.append(
                {
                    "org": org,
                    "namespace": ns,
                    "agent": agent,
                    "user_id": "-",
                    "entity_text": ent,
                    "entity_embedding": vec,
                    "embedding_model": live_model,
                    "embedding_dim": len(vec),
                    "memory_key": memory_key,
                    "created_at": now,
                    "tombstoned_at": None,
                }
            )
            if len(ev_batch) >= batch_size:
                await ch.insert("loom.entity_vectors", ev_batch)
                ev_total += len(ev_batch)
                ev_batch = []
        if ev_batch:
            await ch.insert("loom.entity_vectors", ev_batch)
            ev_total += len(ev_batch)
    print(f"  ✓ {ev_total} entity vectors ({len(distinct_entities)} distinct)")


async def _clickgap_seed_recall_events(
    w: _ClickgapWrite, sdb: sqlite3.Connection, lesson_rows: list[Any]
) -> None:
    """--seed-recall-events: copy lesson_recalls into loom.memory_usage_events,
    skipping orphaned and unparseable rows."""
    ch, org, ns, agent = w.ch, w.org, w.ns, w.agent
    ue_total = 0
    ue_orphans = 0  # recall rows whose lesson_id was not imported
    ue_unparsable = 0  # recall rows with an unparseable recalled_at
    # Filter to imported lessons — recalls outlive the lessons they
    # pointed at, and an orphaned event would pollute the totals.
    valid_ids = {int(r["id"]) for r in lesson_rows}
    ue_batch: list[dict[str, Any]] = []
    cur = sdb.execute("SELECT lesson_id, recalled_at FROM lesson_recalls")
    for r in cur:
        lid = int(r["lesson_id"])
        if lid not in valid_ids:
            ue_orphans += 1
            continue
        ts = _clickgap_parse_dt(r["recalled_at"])
        if ts is None:
            ue_unparsable += 1
            continue
        ue_batch.append(
            {
                "memory_id": _clickgap_memory_id(org, ns, lid),
                "org": org,
                "namespace": ns,
                "event_type": "recall",
                "event_count": 1,
                "ts": ts,
                "agent": agent,
            }
        )
        if len(ue_batch) >= 50000:
            await ch.insert(
                "loom.memory_usage_events",
                ue_batch,
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            )
            ue_total += len(ue_batch)
            ue_batch = []
    if ue_batch:
        await ch.insert(
            "loom.memory_usage_events",
            ue_batch,
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
        ue_total += len(ue_batch)
    print(
        f"  ✓ {ue_total} recall events (Tier 2; pr_number dropped; "
        f"{ue_orphans} orphan + {ue_unparsable} unparseable skipped)"
    )


async def _clickgap_verify(w: _ClickgapWrite, counts: dict[str, int]) -> bool:
    """Read the import back and print the verification block.

    False when any embedding row carries a model other than the live one.
    """
    ch, org, ns, agent, live_model = w.ch, w.org, w.ns, w.agent, w.live_model
    ok = True

    async def _scalar(sql: str, params: dict[str, Any]) -> int:
        rows = await ch.query_all(sql, params=params)
        return int(rows[0]["c"]) if rows else 0

    live_count = await _scalar(
        "SELECT count() AS c FROM loom.memory FINAL "
        "WHERE org={o:String} AND namespace={n:String} AND agent={a:String} "
        "AND tombstoned_at IS NULL",
        {"o": org, "n": ns, "a": agent},
    )
    bad_model = await _scalar(
        "SELECT count() AS c FROM loom.memory_embeddings FINAL "
        "WHERE org={o:String} AND namespace={n:String} "
        "AND embedding_model != {m:String}",
        {"o": org, "n": ns, "m": live_model},
    )
    permanent = await _scalar(
        "SELECT count() AS c FROM loom.memory FINAL "
        "WHERE org={o:String} AND namespace={n:String} "
        "AND tombstoned_at IS NULL AND expires_at IS NULL",
        {"o": org, "n": ns},
    )
    hottest = await ch.query_all(
        "SELECT max(recall_count) AS c FROM loom.memory FINAL "
        "WHERE org={o:String} AND namespace={n:String}",
        params={"o": org, "n": ns},
    )
    max_recall = int(hottest[0]["c"]) if hottest else 0
    print("  verification:")
    print(f"    live memory rows: {live_count} (source: {counts['lessons']})")
    print(f"    wrong-model embedding rows (must be 0): {bad_model}")
    print(f"    permanent (no-TTL) rows: {permanent}")
    print(f"    max recall_count preserved: {max_recall}")
    if bad_model:
        ok = False
    if live_count < counts["lessons"]:
        print(
            "    ! live count below source — async inserts may still be flushing; re-check shortly"
        )
    return ok


async def _clickgap_write_category_links(w: _ClickgapWrite) -> None:
    """Insert the lesson → category links the lesson phase collected."""
    ch, batch_size = w.ch, w.batch_size
    # 4. Category-item links.
    total_links = 0
    for start in range(0, len(w.link_inserts), batch_size):
        await ch.insert("loom.memory_categories", w.link_inserts[start : start + batch_size])
        total_links += len(w.link_inserts[start : start + batch_size])
    print(f"  ✓ {total_links} category-item links")


async def _clickgap_enroll(stack: Any) -> Any:
    """Connect and enroll before selecting the importer's effective embedder."""
    from server import boot
    from server.cli._ch import connect_checked
    from server.core.background import register_buffer_drainer
    from server.core.ch import get_client
    from server.embedding import get_embedder
    from server.insights.app.provider_command import model_command

    await connect_checked(get_client())
    register_buffer_drainer("boot-writes", boot.drain_writes)
    await stack.enter_async_context(model_command())
    return get_embedder()


def _cmd_import_clickgap(args: argparse.Namespace) -> int:
    """Import a ClickGap review-lesson SQLite export into Loom.

    Maps:
      lessons               → loom.memory + loom.memory_embeddings
      lesson_entities       → loom.memory.entities + entity:<kind>:<text> tags
                              + loom.entity_vectors (entity-ANN boost)
      lesson_categories     → loom.categories
      lesson_category_items → loom.memory_categories

    Signal preservation:
    - kind → memory_type 1:1 (only episodic/profile are non-searchable).
    - recall_count → loom.memory.recall_count (authoritative) and mirrored
      onto memory_embeddings.reinforcement_count (the search-rank proxy, since
      recall_count is not a column there).
    - last_recalled_at → memory.last_recalled_at + emb.last_reinforced_at.
    - expires_at preserved verbatim per-row (not recomputed from a policy).
    - reasoning → value_json.reasoning, never concatenated into the assertion.
    - false_positive lessons are suppression memories — tagged
      polarity:negative so consolidation never merges them into a positive.
    - source provenance → source_trace + source:<class> tag; human-attributed
      rows get pii:human-attributed (erasure-by-tag).

    Embedding: REEMBED-ONLY (the export carries no vectors). Embeds the
    assertion, each distinct entity, and each category description, tagged
    with the live model name so search can see them.

    Idempotent: deterministic memory_key='lesson_<id>' + memory_id (UUIDv5)
    + per-run version. The optional --seed-recall-events Tier-2 log is
    additive (run once into a fresh namespace).

    Flags:
      --sqlite PATH         (required) path to the ClickGap memory.db
      --org / --namespace   (required) Loom destination tuple
      --agent A             (default 'clickgap') Loom agent id
      --dry-run             count + print maps + validate parse, no writes
      --batch-size N        rows per CH insert call (default 1000)
      --seed-recall-events  ALSO import the full lesson_recalls event log
                            into loom.memory_usage_events (Tier 2; additive,
                            non-idempotent). Off by default; the denormalized
                            recall columns already drive ranking.
    """
    import asyncio
    import sqlite3
    import sys
    import time
    from datetime import UTC, datetime

    from server.cli._ch import disconnect
    from server.core.ch import get_client
    from server.embedding import get_embedder
    from server.memory.app.provenance import writer_actor
    from server.memory.store import insert_memory_versions

    async def _run() -> int:
        sqlite_path = args.sqlite
        if not sqlite_path:
            print("--sqlite is required", file=sys.stderr)
            return 2
        try:
            sdb = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
            sdb.row_factory = sqlite3.Row
        except sqlite3.Error as e:
            print(f"failed to open sqlite: {e}", file=sys.stderr)
            return 2

        org = args.org
        ns = args.namespace
        agent = args.agent or "clickgap"
        dry = bool(args.dry_run)
        batch_size = int(args.batch_size or 1000)
        seed_events = bool(args.seed_recall_events)

        counts = _clickgap_count_source(sdb, sqlite_path)
        if counts is None:
            return 2

        entities_by_lesson, cat_rows, cat_meta_by_loom_id, links_by_lesson = _clickgap_preload(
            sdb, org, ns
        )

        embedder = get_embedder()
        live_model = embedder.model_name

        lesson_rows = sdb.execute(
            "SELECT id, text, source, created_at, kind, expires_at, "
            "evidence_count, recall_count, last_recalled_at, reasoning "
            "FROM lessons"
        ).fetchall()

        if dry:
            await _clickgap_dry_run_report(
                lesson_rows, cat_rows, cat_meta_by_loom_id, entities_by_lesson, embedder, live_model
            )
            sdb.close()
            return 0

        ch = get_client()
        embedder = await _clickgap_enroll(command_stack)
        live_model = embedder.model_name
        now = datetime.now(UTC).replace(tzinfo=None)
        run_version = time.time_ns()

        w = _ClickgapWrite(
            ch=ch,
            org=org,
            ns=ns,
            agent=agent,
            batch_size=batch_size,
            embedder=embedder,
            live_model=live_model,
            now=now,
            run_version=run_version,
            actor=writer_actor(fallback="process:cli:import-clickgap"),
        )

        await _clickgap_write_categories(w, cat_meta_by_loom_id)

        # 2. Lessons → memory + embeddings. Batched so reembed runs one
        #    embed call per window. Collect (entity, memory_key) pairs for
        #    entity_vectors and (memory_key, category_id) for links.
        total_items = total_emb = 0
        for start in range(0, len(lesson_rows), batch_size):
            window = lesson_rows[start : start + batch_size]
            mem_batch, emb_batch = await _clickgap_map_lesson_window(
                w, window, entities_by_lesson, links_by_lesson
            )
            if mem_batch:
                await insert_memory_versions(mem_batch, ch=ch)
                total_items += len(mem_batch)
            if emb_batch:
                await ch.insert("loom.memory_embeddings", emb_batch)
                total_emb += len(emb_batch)
        print(
            f"  ✓ {total_items} lessons → memory ({total_emb} embeddings, "
            f"{w.polarity_neg} suppression/polarity:negative, "
            f"{w.non_searchable} non-searchable [summary→episodic, get/list only])"
        )
        if w.unparsable_created:
            print(
                f"  ! {w.unparsable_created} rows had an unparseable created_at "
                f"→ stamped import time (re-run --dry-run to inspect)",
                file=sys.stderr,
            )

        await _clickgap_write_entity_vectors(w)

        await _clickgap_write_category_links(w)

        # 5. Tier-2 (optional): full recall event log → memory_usage_events.
        #    Additive / non-idempotent, analytics-only. pr_number is dropped.
        if seed_events and counts["lesson_recalls"]:
            await _clickgap_seed_recall_events(w, sdb, lesson_rows)

        # 6. Verification gate — prove the import is searchable.
        ok = await _clickgap_verify(w, counts)

        sdb.close()
        print("✓ import complete" if ok else "✗ import completed WITH verification failures")
        print(
            "  next: enable recall ranking — see "
            "docs/guides/importing-clickgap-memory.md, then run "
            "`python -m bench.clickgap_migration_validate`."
        )
        return 0 if ok else 1

    async def _run_then_disconnect() -> int:
        # A failed write must not skip the disconnect (a no-op when unconnected).
        try:
            async with command_stack:
                return await _run()
        finally:
            await disconnect(get_client())

    from contextlib import AsyncExitStack

    command_stack = AsyncExitStack()
    return asyncio.run(_run_then_disconnect())


def _add_clickgap_import_parser(sub: Any) -> None:
    """The `import-clickgap` subcommand."""

    p_clickgap = sub.add_parser(
        "import-clickgap",
        help="Import a ClickGap (Detector→Validator→Reviewer) SQLite export "
        "into Loom (lessons + typed entities + categories). Reembed-only; "
        "preserves kind→memory_type, per-row TTL, recall_count (→ ranking "
        "via reinforcement_count), suppression polarity, and provenance. "
        "Idempotent via deterministic keys + per-run version.",
    )
    p_clickgap.add_argument("--sqlite", required=True, help="Path to the ClickGap memory.db file")
    p_clickgap.add_argument("--org", required=True)
    p_clickgap.add_argument(
        "--namespace",
        required=True,
        help="Destination namespace (use a dedicated one, "
        "e.g. 'clickgap', so rollback = namespace.delete)",
    )
    p_clickgap.add_argument(
        "--agent",
        default="clickgap",
        help="Loom agent id for the imported rows (default: 'clickgap')",
    )
    p_clickgap.add_argument(
        "--dry-run",
        action="store_true",
        help="Count + print kind/TTL/category maps + validate parse + probe embedder, no writes.",
    )
    p_clickgap.add_argument(
        "--batch-size", type=int, default=1000, help="Rows per CH insert call (default: 1000)"
    )
    p_clickgap.add_argument(
        "--seed-recall-events",
        action="store_true",
        help="ALSO import the full lesson_recalls event log "
        "into loom.memory_usage_events (Tier 2; "
        "additive/non-idempotent, analytics-only — the "
        "denormalized recall columns already drive "
        "ranking). pr_number is dropped.",
    )
    p_clickgap.set_defaults(func=_cmd_import_clickgap)
