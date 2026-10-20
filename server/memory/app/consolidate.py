"""memory.consolidate: merge a cluster of near-duplicate memories into one
canonical row through an LLM, and the run ledger consolidations.list reads."""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from server.memory.api.models_admin import (
    ConsolidationRunRow,
    ConsolidationsListInput,
    ConsolidationsListOutput,
    MemoryConsolidateInput,
    MemoryConsolidateOutput,
    MemoryConsolidateResult,
)
from server.memory.app.drops import record_drop
from server.memory.app.provenance import writer_actor
from server.memory.domain.sensitivity import (
    inherited_sensitivity,
    value_reads_high,
)
from server.memory.store.rows import next_version
from server.observability.genai import staged

log = logging.getLogger(__name__)

_SUPPRESSION_TAG = "polarity:negative"


def _consolidation_compatible(row: Mapping[str, Any], seed: Mapping[str, Any]) -> bool:
    """Whether two candidate memories may be consolidated into one cluster.

    Guards applied before the cosine/entity-overlap test: same memory_type, and
    same polarity — a suppression/negative memory must never merge into a positive
    one on the same topic (cosine-near but opposite in meaning).
    """
    if row.get("memory_type") != seed.get("memory_type"):
        return False
    row_neg = _SUPPRESSION_TAG in (row.get("tags") or [])
    seed_neg = _SUPPRESSION_TAG in (seed.get("tags") or [])
    return row_neg == seed_neg


def _parse_merge_response(text: str) -> tuple[str, str]:
    """Pull (canonical_key, canonical_value) out of the merge LLM reply.

    Raises ValueError when the reply carries no JSON object or the object
    lacks either field — the caller records the cluster as failed instead
    of silently skipping it.
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith(("json\n", "json ")):
            text = text[5:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no JSON object in merge response: {text[:120]!r}")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("merge response JSON is not an object")
    key = str(parsed.get("canonical_key", "")).strip()
    value = str(parsed.get("canonical_value", "")).strip()
    if not key or not value:
        raise ValueError("merge response lacked canonical_key/value")
    return key, value


def _cluster_cohesion(
    cluster: list[dict[str, Any]],
    cos: Any,
) -> float:
    """Mean pairwise cosine over the cluster's embeddings (0.0 for < 2)."""
    embs = [list(r["embedding"]) for r in cluster if r.get("embedding")]
    pairs = [(i, j) for i in range(len(embs)) for j in range(i + 1, len(embs))]
    if not pairs:
        return 0.0
    return float(sum(float(cos(embs[i], embs[j])) for i, j in pairs) / len(pairs))


def _union_preserving_order(cluster: list[dict[str, Any]], col: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for r in cluster:
        for v in r.get(col) or []:
            sv = str(v)
            if sv and sv not in seen:
                seen.add(sv)
                out.append(sv)
    return out


def _earliest(cluster: list[dict[str, Any]], col: str) -> datetime | None:
    vals: list[datetime] = [r[col] for r in cluster if isinstance(r.get(col), datetime)]
    return min(vals) if vals else None


def _confidence_of_earliest_anchor(cluster: list[dict[str, Any]]) -> float:
    """The grade that belongs to the anchor the canonical row keeps (the
    earliest one) — never a blend across rows about different dates."""
    dated = [r for r in cluster if isinstance(r.get("temporal_anchor"), datetime)]
    if not dated:
        return 0.0
    winner = min(dated, key=lambda r: r["temporal_anchor"])
    return float(winner.get("temporal_confidence") or 0.0)


def _with_sensitivity_tag_for_row(
    tags: list[str], cluster: list[dict[str, Any]], canonical_value: str
) -> list[str]:
    """The merged row's `sensitivity:<level>` tag for the label it inherits; the union of
    source tags may already carry one."""
    from server.memory.domain.sensitivity import sensitivity_tags

    level = inherited_sensitivity(
        (str(r.get("sensitivity") or "") for r in cluster), canonical_value
    )
    return [*tags, *sensitivity_tags(level, tags)]


def _consolidation_canonical_row(
    input: MemoryConsolidateInput,
    cluster: list[dict[str, Any]],
    *,
    canonical_key: str,
    canonical_value: str,
    content_sha256: str,
    version: int,
    now: datetime,
) -> dict[str, Any]:
    """The merged loom.memory row, carrying EVERY persisted column.

    Facets are the UNION of the sources (tags/entities/files/concepts) and
    the validity anchors the EARLIEST (valid_at/temporal_anchor) — the
    merged fact is true from when its oldest constituent was. Every column must be set: when
    canonical_key is one of the merged keys, FINAL would zero an omitted column on that key.
    When canonical_key IS one of the merged keys this is an LWW overwrite:
    its memory_id and created_at are carried forward; otherwise a fresh id.
    """
    from server.memory.store import MEMORY_PERSISTED_COLS

    same_key = next((r for r in cluster if r["memory_key"] == canonical_key), None)
    memory_id = (
        str(same_key["memory_id"])
        if same_key is not None and same_key.get("memory_id")
        else str(uuid.uuid4())
    )
    created_at = (
        same_key["created_at"]
        if same_key is not None and same_key.get("created_at") is not None
        else now
    )
    row: dict[str, Any] = {
        "org": input.org,
        "namespace": input.namespace,
        "agent": input.agent,
        "user_id": input.user_id,
        "session_id": "-",
        "memory_key": canonical_key,
        "memory_type": cluster[-1].get("memory_type", "semantic"),
        "value": canonical_value,
        "value_json": "{}",
        "confidence": 1.0,
        "source_trace": "memory.consolidate",
        "version": version,
        "tombstoned_at": None,
        "created_at": created_at,
        "expires_at": None,
        # Pinned rows are excluded from the candidate query, so the merge
        # never swallows one; the canonical row starts unpinned.
        "pinned": 0,
        "run_id": "-",
        "entities": _union_preserving_order(cluster, "entities"),
        "memory_id": memory_id,
        "redacted_at": None,
        "redaction_reason": "",
        "status": "active",
        "conflict_with": [],
        "duplicate_of": "",
        "evidence_count": len(cluster),
        "recall_count": 0,
        "last_recalled_at": None,
        "cross_promoted_from_memory_id": None,
        "cross_promoted_from_namespace": "",
        "tags": _with_sensitivity_tag_for_row(
            _union_preserving_order(cluster, "tags"),
            cluster,
            canonical_value,
        ),
        "superseded_by": "",
        "rejection_reason": "",
        "temporal_anchor": _earliest(cluster, "temporal_anchor"),
        "temporal_confidence": _confidence_of_earliest_anchor(cluster),
        "updated_at": now,
        "valid_at": _earliest(cluster, "valid_at"),
        "valid_to": None,
        # record-axis retirement stamp: a fresh canonical row is live
        "invalid_at": None,
        "char_limit": None,
        "reinforcement_count": 1,
        "last_reinforced_at": now,
        "mentioned_at": None,
        "source_resource_id": "",
        "content_sha256": content_sha256,
        "files": _union_preserving_order(cluster, "files"),
        "concepts": _union_preserving_order(cluster, "concepts"),
        # A merged row is new content nobody has attested to; the sources'
        # attestations (if any) were bound to their own hashes and do not
        # carry over. Verification starts again at `none`.
        "verified_by": "",
        "verified_at": None,
        "verified_sha256": "",
        # A consolidated row is synthesised from several sources: its grounding
        # is not any one fact's grade, so it starts ungraded.
        "grounding": "",
        "evidence": "",
        # The candidate query keeps 'high' rows out of a cluster, but a 'medium'
        # row (an email, a phone number) is embedded and can enter one. The merged
        # row is never labelled lower than its most sensitive source or its own text.
        "sensitivity": inherited_sensitivity(
            (str(r.get("sensitivity") or "") for r in cluster), canonical_value
        ),
        # A consolidated row is derived from its merged sources; the writer is
        # whoever ran the consolidation (or the worker).
        "actor": writer_actor(fallback="process:consolidation"),
        "source_class": "derived",
        # A canonical row is new: it starts unlocked (locked sources never
        # enter a cluster — the candidate query excludes them).
        "lock_mode": "",
        "lock_reason": "",
        "lock_until": None,
        "locked_by": "",
        "locked_at": None,
        # Expiry post-action: a merged row takes the default.
        "on_expire": "",
        "expire_reason": "",
    }
    missing = [c for c in MEMORY_PERSISTED_COLS if c not in row]
    if missing:  # pragma: no cover — schema drift guard
        raise RuntimeError(f"consolidation canonical row missing columns {missing}")
    return row


async def _record_consolidation_run(
    ch: Any,
    input: MemoryConsolidateInput,
    *,
    merged_keys: list[str],
    cluster_size: int,
    cluster_cohesion: float,
    output_memory_key: str,
    memory_type: str,
    llm: Any,
    llm_result: Any,
    duration_ms: int,
    status: str,
    failure_reason: str,
) -> None:
    """One loom.consolidation_runs row per cluster examined (written OR
    failed). Fail-open (the consolidation itself already happened) but
    never silent: a swallowed insert error here would make failed clusters disappear."""
    try:
        await ch.insert(
            "loom.consolidation_runs",
            [
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "agent": input.agent,
                    "user_id": input.user_id,
                    # source_memory_keys carries lineage; source_memory_ids is
                    # left to its CH default rather than written empty.
                    "source_memory_keys": list(merged_keys),
                    "cluster_size": int(cluster_size),
                    "cluster_cohesion": float(cluster_cohesion),
                    "output_memory_key": output_memory_key,
                    "memory_type": memory_type,
                    "llm_provider": getattr(llm, "provider", ""),
                    "llm_model": getattr(llm, "model", ""),
                    "tokens_in": int(getattr(llm_result, "tokens_in", 0) or 0),
                    "tokens_out": int(getattr(llm_result, "tokens_out", 0) or 0),
                    "duration_ms": int(duration_ms),
                    "status": status,
                    "failure_reason": failure_reason[:500],
                }
            ],
            settings={"async_insert": 1, "wait_for_async_insert": 0},
        )
    except Exception as e:  # noqa: BLE001 — the run ledger is best-effort
        log.warning(
            "memory.consolidate: consolidation_runs insert failed for %s/%s "
            "(status=%s, keys=%s): %s",
            input.org,
            input.namespace,
            status,
            merged_keys,
            e,
        )


_CONSOLIDATION_CANDIDATES_SQL = """
SELECT
    toString(m.memory_id) AS memory_id,
    m.session_id,
    m.memory_key,
    m.memory_type,
    m.value,
    m.created_at,
    m.tags,
    m.files,
    m.concepts,
    m.valid_at,
    m.temporal_anchor,
    m.temporal_confidence,
    m.sensitivity,
    e.embedding,
    e.entities
FROM loom.memory AS m FINAL
INNER JOIN loom.memory_embeddings AS e FINAL
        ON m.org = e.org AND m.namespace = e.namespace
        AND m.agent = e.agent AND m.user_id = e.user_id
        AND m.session_id = e.session_id
        AND m.memory_key = e.memory_key
WHERE m.org = {org:String}
  AND m.namespace = {ns:String}
  AND m.agent = {agent:String}
  AND m.user_id = {uid:String}
  AND m.tombstoned_at IS NULL
  AND m.status IN ('active', 'duplicate', 'conflicted')
  AND m.created_at <= {age_cutoff:DateTime}
  -- Exclude pinned: nothing downstream re-checks the pin, so a pinned
  -- row swept into a cluster would be tombstoned with the rest.
  AND m.pinned = 0
  -- Same for a lock in force: a no_delete or
  -- read_only row is never merged away.
  AND (m.lock_mode = '' OR (m.lock_until IS NOT NULL AND m.lock_until <= now()))
  -- a live vector only: a retired copy or a high write's fence is not a candidate
  AND e.tombstoned_at IS NULL
  AND e.embedding_dim > 0
  -- the served model's vectors only: one row per memory, comparable cosines
  AND e.embedding_model = {model:String}
  AND length(e.embedding) > 0
  AND m.sensitivity != 'high'
ORDER BY m.created_at DESC
LIMIT {limit:UInt32}
"""


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _cluster_rows(rows: list[dict[str, Any]], threshold: float) -> list[list[dict[str, Any]]]:
    """Greedy single-link agglomeration: each row joins the first cluster whose
    seed it's close to, else starts a new singleton."""
    clusters: list[list[dict[str, Any]]] = []
    for row in rows:
        row_entities = set(row.get("entities") or [])
        merged_into = False
        for cluster in clusters:
            seed = cluster[0]
            if not _consolidation_compatible(row, seed):
                continue
            seed_entities = set(seed.get("entities") or [])
            # Require entity overlap OR very-high similarity, so memories that
            # merely share vocabulary don't merge.
            sim = _cos(list(row["embedding"]), list(seed["embedding"]))
            if sim >= threshold and (
                (row_entities & seed_entities) or sim >= max(threshold + 0.05, 0.92)
            ):
                cluster.append(row)
                merged_into = True
                break
        if not merged_into:
            clusters.append([row])
    return clusters


_MERGE_SYSTEM = (
    "You merge several memories about the same subject into ONE "
    "canonical memory. The merged memory must:\n"
    "1. Be a single complete short sentence (under 200 chars).\n"
    "2. Preserve every unique fact across the input memories.\n"
    "3. Later entries update earlier ones, never the reverse: when "
    "two entries contradict, the LATER one (higher index) wins.\n"
    "3b. When entries are related but NOT contradictory, keep both "
    "facts; do not treat 'more detail' as a contradiction.\n"
    "4. Use the user's most specific vocabulary (names, "
    "numbers, dates verbatim).\n\n"
    "Return strict JSON: "
    '{"canonical_key":"<short_snake_case>",'
    ' "canonical_value":"<sentence>"}'
)


async def _ask_merge(llm: Any, cluster: list[dict[str, Any]]) -> tuple[Any, str, str, str, str]:
    """The LLM's merge of one cluster, checked by the merge guard: (llm_result,
    canonical_key, canonical_value, failure_reason, refused_lossy). The key and
    value are '' when the merge failed or was refused."""
    cluster_text = "\n".join(
        f"  [{i}] key={r['memory_key']!r}, value={r['value']!r}" for i, r in enumerate(cluster)
    )
    user = f"Memories to merge:\n{cluster_text}\n\nMerged memory JSON:"
    llm_result = None
    canonical_key = canonical_value = ""
    failure_reason = ""
    try:
        llm_result = await llm.complete(
            system=_MERGE_SYSTEM,
            user=user,
            max_tokens=256,
        )
        canonical_key, canonical_value = _parse_merge_response(llm_result.text)
    except Exception as e:  # noqa: BLE001 — recorded, never silent
        failure_reason = f"{type(e).__name__}: {e}"[:500]
    # A merge that drops a date, quantity, identifier, name or negation one source
    # carried cannot answer every question the sources answered. Descriptive
    # similarity chose the cluster; this is the decision-side check before the sources
    # are lost.
    refused_lossy = ""
    if canonical_key and canonical_value:
        from server.memory.domain.merge_guard import merge_preserves

        verdict = merge_preserves([str(r.get("value") or "") for r in cluster], canonical_value)
        if not verdict.ok:
            refused_lossy = verdict.reason()
            failure_reason = f"merge lost distinguishing values: {refused_lossy}"[:500]
            canonical_key = canonical_value = ""
    return llm_result, canonical_key, canonical_value, failure_reason, refused_lossy


async def _write_canonical(
    ch: Any,
    input: MemoryConsolidateInput,
    embedder: Any,
    cluster: list[dict[str, Any]],
    *,
    canonical_key: str,
    canonical_value: str,
    now_naive: datetime,
) -> dict[str, Any]:
    """LWW upsert of the canonical row, its derived_from lineage and its
    embedding row; if canonical_key was one of the merged keys this overwrites
    it (and that row is then NOT tombstoned)."""

    import server.memory.store as memory_repo
    from server.core.hashing import sha256_value as _content_sha256

    canonical_sha = _content_sha256(canonical_value)
    version = next_version()
    canonical_row = _consolidation_canonical_row(
        input,
        cluster,
        canonical_key=canonical_key,
        canonical_value=canonical_value,
        content_sha256=canonical_sha,
        version=version,
        now=now_naive,
    )
    await memory_repo.insert_memory_versions([canonical_row], ch=ch)
    # Lineage: the canonical row is derived from its merged sources, one belief-source
    # link per source memory, so memory.provenance / erasure can walk from the summary
    # back down.
    try:
        from server.memory.app.evidence import link_belief_sources

        await link_belief_sources(
            org=input.org,
            namespace=input.namespace,
            memory_links=[(str(canonical_row["memory_key"]), str(canonical_row["memory_id"]))],
            evidence_ids=[str(r["memory_id"]) for r in cluster if r.get("memory_id")],
            support_type="derived_from",
            ch=ch,
        )
    except Exception as e:  # noqa: BLE001 — lineage is additive
        log.warning("consolidate: derived_from links failed (non-fatal): %s", e)
    if canonical_row["sensitivity"] == "high":
        # The merged text holds a credential: no vector, and the key is fenced.
        from server.memory.app.high_fence import fence_high

        await fence_high([(memory_repo.embedding_key(canonical_row), version)], ch=ch)
        return canonical_row
    canonical_embedding = await embedder.embed(canonical_value)
    await ch.insert(
        "loom.memory_embeddings",
        [
            memory_repo.build_embedding_row(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id,
                session_id="-",
                memory_key=canonical_key,
                memory_type=canonical_row["memory_type"],
                scope="private",
                content_excerpt=canonical_value[:256],
                embedding_model=embedder.model_name,
                embedding_dim=embedder.dim,
                embedding=canonical_embedding,
                confidence=1.0,
                tombstoned_at=None,
                created_at=canonical_row["created_at"],
                version=version,
                tags=list(canonical_row["tags"]),
                entities=list(canonical_row["entities"]),
                temporal_anchor=canonical_row["temporal_anchor"],
                temporal_confidence=float(canonical_row.get("temporal_confidence") or 0.0),
                valid_at=canonical_row["valid_at"],
                files=list(canonical_row["files"]),
                concepts=list(canonical_row["concepts"]),
            )
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    return canonical_row


async def _retire_merged(
    ch: Any,
    input: MemoryConsolidateInput,
    cluster: list[dict[str, Any]],
    *,
    canonical_key: str,
    now_naive: datetime,
) -> None:
    """Tombstone the merged-from rows (except the canonical itself). The
    tombstone is a NEW RMT version, so it must mirror EVERY persisted column: an
    omitted column would be zeroed under FINAL (memory_id/tags/entities/created_at/
    pinned/content_sha256) and leave memory.restore nothing to restore."""
    import server.memory.store as memory_repo
    from server.memory.app.set_chunks import tombstone_set_chunks_fail_open

    tombstone_ids = [
        str(r["memory_id"])
        for r in cluster
        if r["memory_key"] != canonical_key and r.get("memory_id")
    ]

    version = memory_repo.next_version()  # the tombstones', which the vector retire carries

    def _tombstone(existing: memory_repo.MemoryRow, _ts: datetime = now_naive) -> dict[str, Any]:
        return {"tombstoned_at": _ts, "expires_at": None, "version": version}

    await memory_repo.write_versions_by_ids(
        input.org,
        tombstone_ids,
        _tombstone,
        ch=ch,
    )
    # Search reads loom.memory_embeddings, so the merged-from rows must be
    # tombstoned THERE too or they keep surfacing beside the canonical —
    # the duplicate the merge was meant to remove. Non-fatal per row: the
    # memory-table tombstone is durable, and a stray embedding is a
    # ranking nuisance, not data loss.
    for r in cluster:
        if r["memory_key"] == canonical_key:
            continue
        try:
            await memory_repo.tombstone_embeddings_for_key(
                input.org,
                input.namespace,
                input.agent,
                input.user_id,
                str(r.get("session_id") or "-"),
                str(r["memory_key"]),
                ts=now_naive,
                version=version,
                ch=ch,
            )
        except Exception as e:  # noqa: BLE001 — the memory tombstone stands
            log.warning(
                "memory.consolidate: embeddings tombstone failed for %s: %s",
                r["memory_key"],
                e,
            )
        # and its verbatim chunks, as memory.delete does: the chunk leg reads them with
        # no join back to loom.memory
        await tombstone_set_chunks_fail_open(
            org=input.org,
            namespace=input.namespace,
            memory_key=str(r["memory_key"]),
            user_id=input.user_id,
            ch=ch,
        )


@staged("consolidate_merge")
async def handle_memory_consolidate(
    input: MemoryConsolidateInput,
) -> MemoryConsolidateOutput:
    """Cluster semantically-similar active memories and merge each cluster into
    one canonical memory via the LLM.

    Fetch active memories under identity (age >= min_age_seconds), cluster by
    cosine similarity + entity overlap, ask the LLM to merge each cluster of >= 2
    into a canonical key/value, then LWW-upsert the canonical and tombstone the
    merged-from keys. `dry_run=True` skips the write step.
    """
    import time as _time

    from server.core.ch import get_client
    from server.embedding import get_embedder
    from server.llm.client import get_llm_client

    ch = get_client()
    embedder = get_embedder()
    llm = get_llm_client()
    now_utc = datetime.now(UTC)
    age_cutoff = now_utc - timedelta(seconds=input.min_age_seconds)

    rows = await ch.query_all(
        _CONSOLIDATION_CANDIDATES_SQL,
        params={
            "org": input.org,
            "ns": input.namespace,
            "agent": input.agent,
            "uid": input.user_id,
            "age_cutoff": age_cutoff.replace(tzinfo=None),
            "limit": int(input.limit),
            "model": embedder.model_name,
        },
    )
    # A row whose value reads as high, whatever its label (unset after copy_to or an import,
    # or `low` from its writer), never goes into the merge prompt, which is sent to the LLM.
    rows = [r for r in rows if not value_reads_high(r.get("value"))]

    out = MemoryConsolidateOutput(examined=len(rows))
    if len(rows) < 2:
        return out

    clusters = _cluster_rows(rows, float(input.similarity_threshold))
    merge_clusters = [c for c in clusters if len(c) >= 2]
    out.clusters_found = len(merge_clusters)
    if not merge_clusters:
        return out

    consolidated: list[MemoryConsolidateResult] = []
    tokens_in = tokens_out = 0
    # Clusters are independent (disjoint keys), so their merges run
    # concurrently under a bound; results are collected by cluster index so the output order
    # is the cluster order, not the completion order. Token totals and failure counts are
    # updated from coroutines on one event loop, so plain += is safe.
    import asyncio as _asyncio

    from server.settings import settings as _settings

    slots: list[MemoryConsolidateResult | None] = [None] * len(merge_clusters)
    _sem = _asyncio.Semaphore(max(1, int(_settings.consolidation_merge_concurrency)))

    async def _merge_one(slot: int, cluster: list[dict[str, Any]]) -> None:
        nonlocal tokens_in, tokens_out
        async with _sem:
            cluster_started = _time.monotonic()
            # Oldest -> newest: the merge prompt resolves contradictions toward the
            # LAST entry, and the canonical row inherits cluster[-1]'s memory_type.
            # Candidates arrive DESC, so this re-sort is required.
            cluster.sort(key=lambda r: r["created_at"])
            merged_keys = [r["memory_key"] for r in cluster]
            cluster_cohesion = _cluster_cohesion(cluster, _cos)
            (
                llm_result,
                canonical_key,
                canonical_value,
                failure_reason,
                refused_lossy,
            ) = await _ask_merge(llm, cluster)
            if llm_result is not None:
                tokens_in += llm_result.tokens_in
                tokens_out += llm_result.tokens_out
            if not canonical_key or not canonical_value:
                # Record a failed merge too, so the Admin UI and the drop ledger see every
                # cluster examined, not only the successful ones.
                failure_reason = failure_reason or "merge response lacked canonical_key/value"
                log.warning(
                    "memory.consolidate: merge failed for %s/%s cluster %s: %s",
                    input.org,
                    input.namespace,
                    merged_keys,
                    failure_reason,
                )
                out.clusters_failed += 1
                if refused_lossy:
                    out.clusters_refused_lossy += 1
                record_drop(
                    input.org,
                    input.namespace,
                    "consolidate.merge",
                    "refused_lossy" if refused_lossy else "failed",
                    item=",".join(merged_keys),
                    n=len(cluster),
                )
                if not input.dry_run:
                    await _record_consolidation_run(
                        ch,
                        input,
                        merged_keys=merged_keys,
                        cluster_size=len(cluster),
                        cluster_cohesion=cluster_cohesion,
                        output_memory_key="",
                        memory_type=cluster[-1].get("memory_type", "semantic"),
                        llm=llm,
                        llm_result=llm_result,
                        duration_ms=int((_time.monotonic() - cluster_started) * 1000),
                        status="refused_lossy" if refused_lossy else "failed",
                        failure_reason=failure_reason,
                    )
                return

            slots[slot] = MemoryConsolidateResult(
                canonical_key=canonical_key,
                canonical_value=canonical_value,
                merged_keys=merged_keys,
                cluster_size=len(cluster),
            )

            if input.dry_run:
                return

            now_naive = now_utc.replace(tzinfo=None)
            canonical_row = await _write_canonical(
                ch,
                input,
                embedder,
                cluster,
                canonical_key=canonical_key,
                canonical_value=canonical_value,
                now_naive=now_naive,
            )
            await _retire_merged(
                ch, input, cluster, canonical_key=canonical_key, now_naive=now_naive
            )
            await _record_consolidation_run(
                ch,
                input,
                merged_keys=merged_keys,
                cluster_size=len(cluster),
                cluster_cohesion=cluster_cohesion,
                output_memory_key=canonical_key,
                memory_type=canonical_row["memory_type"],
                llm=llm,
                llm_result=llm_result,
                duration_ms=int((_time.monotonic() - cluster_started) * 1000),
                status="written",
                failure_reason="",
            )

    await _asyncio.gather(*[_merge_one(i, c) for i, c in enumerate(merge_clusters)])
    consolidated = [r for r in slots if r is not None]
    out.consolidated = consolidated
    out.tokens_in = tokens_in
    out.tokens_out = tokens_out
    return out


async def handle_consolidations_list(
    input: ConsolidationsListInput,
) -> ConsolidationsListOutput:
    from server.core.ch import get_client

    ch = get_client()
    rows = await ch.query_all(
        """
        SELECT
            toString(run_id)             AS run_id,
            memory_type,
            arrayMap(x -> toString(x), source_memory_ids) AS source_memory_ids,
            source_memory_keys,
            cluster_size,
            cluster_cohesion,
            toString(coalesce(
                output_memory_id,
                toUUID('00000000-0000-0000-0000-000000000000')
            )) AS output_memory_id,
            output_memory_key,
            llm_provider,
            llm_model,
            tokens_in,
            tokens_out,
            duration_ms,
            CAST(status AS String)       AS status,
            failure_reason,
            toString(ran_at)             AS ran_at
        FROM loom.consolidation_runs
        WHERE org = {org:String}
          AND namespace = {ns:String}
        ORDER BY ran_at DESC
        LIMIT {lim:UInt32}
        """,
        params={
            "org": input.org,
            "ns": input.namespace,
            "lim": int(input.limit),
        },
    )
    out = [ConsolidationRunRow(**r) for r in rows]
    return ConsolidationsListOutput(rows=out)
