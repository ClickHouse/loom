"""memory.consolidate: merge a cluster of near-duplicate memories into one
canonical row through an LLM, and the run ledger consolidations.list reads."""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import server.memory.store as memory_repo
from server.core import pg
from server.core.error_summary import error_summary
from server.core.errors import stored_caller_text
from server.core.hashing import hash_text, sha256_value
from server.identity.domain.read_scope import WHOLE_NAMESPACE
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
from server.memory.domain.lock import refuses_retirement
from server.memory.domain.sensitivity import (
    Sensitivity,
    inherited_sensitivity,
    sensitivity_tags,
    value_reads_high,
)
from server.memory.store.pg_memory import AdmissionContendedError, MemoryErased
from server.memory.store.rows import next_version
from server.observability.genai import staged
from server.settings import settings
from server.settings.budgets import llm_max_tokens

log = logging.getLogger(__name__)

# The failure reasons a run records in Loom's own words: `consolidations.list` shows
# these whole, and any other reason as its error's kind.
_LOSSY_MERGE = "merge lost distinguishing values: "
_NO_JSON = "no JSON object in merge response"
_NOT_OBJECT = "merge response JSON is not an object"
_NO_CANONICAL = "merge response lacked canonical_key/value"
_LEFT_LIVE = "merged rows left live: "
_OWN_REASONS = (_LOSSY_MERGE, _NO_JSON, _NOT_OBJECT, _NO_CANONICAL, _LEFT_LIVE)
# `error_summary`'s shape: a kind and a code, never a message.
_SUMMARY = re.compile(r"[A-Za-z_]\w*(?: (?:code|status|sqlstate) \w+)*")
_UNSHOWN_FAILURE = "the merge failed; the error is in the server log"

_SUPPRESSION_TAG = "polarity:negative"
# The session a canonical row is written in: a merge spans its writer's sessions.
_CANONICAL_SESSION = "-"


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
        raise ValueError(_NO_JSON)
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError(_NOT_OBJECT)
    key = str(parsed.get("canonical_key", "")).strip()
    value = str(parsed.get("canonical_value", "")).strip()
    if not key or not value:
        raise ValueError(_NO_CANONICAL)
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


def _summary_sensitivity(cluster: list[dict[str, Any]], canonical_value: str) -> Sensitivity:
    """The summary's label: never lower than its most sensitive source or its own text.
    The candidate query keeps 'high' rows out of a cluster, but a 'medium' row (an email,
    a phone number) is embedded and can enter one."""
    return inherited_sensitivity(
        (str(r.get("sensitivity") or "") for r in cluster), canonical_value
    )


def _consolidation_canonical_row(
    input: MemoryConsolidateInput,
    cluster: list[dict[str, Any]],
    *,
    canonical_key: str,
    canonical_value: str,
    content_sha256: str,
    version: int,
    now: datetime,
    sensitivity: Sensitivity,
) -> dict[str, Any]:
    """The merged loom.memory row, carrying EVERY persisted column, labelled `sensitivity`
    (`_summary_sensitivity`) with its `sensitivity:<level>` tag beside the sources' tags.

    Facets are the UNION of the sources (tags/entities/files/concepts) and
    the validity anchors the EARLIEST (valid_at/temporal_anchor) — the
    merged fact is true from when its oldest constituent was. Every column must be set: when
    canonical_key is one of the merged keys, FINAL would zero an omitted column on that key.
    When canonical_key IS one of the merged keys the row is a new version of that
    member: its memory_id and created_at are carried forward; otherwise a fresh id.
    """
    from server.memory.store import MEMORY_PERSISTED_COLS

    same_key = _canonical_member(cluster, canonical_key)
    tags = _union_preserving_order(cluster, "tags")
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
        "session_id": _CANONICAL_SESSION,
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
        "tags": [*tags, *sensitivity_tags(sensitivity, tags)],
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
        "sensitivity": sensitivity,
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
            error_summary(e),
        )


_CONSOLIDATION_CANDIDATES_SQL = """
SELECT
    toString(m.memory_id) AS memory_id,
    m.org,
    m.namespace,
    m.agent,
    m.user_id,
    m.session_id,
    m.version,
    m.redacted_at,
    m.memory_key,
    m.memory_type,
    m.value,
    m.content_sha256,
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
  AND m.redacted_at IS NULL
  AND m.status IN ('active', 'duplicate', 'conflicted')
  AND m.created_at <= {age_cutoff:DateTime}
  -- Pinned and locked rows stay out of a cluster; one pinned or locked
  -- since this read is left by `_held_since`.
  AND m.pinned = 0
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
            max_tokens=llm_max_tokens("consolidate_merge"),
        )
    except Exception as e:  # noqa: BLE001 — recorded, never silent
        failure_reason = error_summary(e)  # a provider's error may quote the prompt
    else:
        try:
            canonical_key, canonical_value = _parse_merge_response(llm_result.text)
        except ValueError as e:  # our own message, or json's, which quotes nothing
            failure_reason = f"{type(e).__name__}: {e}"
        except Exception as e:  # noqa: BLE001 — recorded, never silent
            failure_reason = error_summary(e)
    # A merge that drops a date, quantity, identifier, name or negation one source
    # carried cannot answer every question the sources answered. Descriptive
    # similarity chose the cluster; this is the decision-side check before the sources
    # are lost.
    refused_lossy = ""
    if canonical_key and canonical_value:
        from server.memory.domain.merge_guard import merge_preserves

        verdict = merge_preserves([str(r.get("value") or "") for r in cluster], canonical_value)
        if not verdict.ok:
            refused_lossy = verdict.counts()
            failure_reason = f"{_LOSSY_MERGE}{refused_lossy}"[:500]
            canonical_key = canonical_value = ""
    return llm_result, canonical_key, canonical_value, failure_reason, refused_lossy


class _Unmerged(Exception):
    """A cluster left unmerged, nothing written: its drop reason and its run's."""

    drop = ""
    reason = ""


class _HeldSince(_Unmerged):
    """A row the merge reads or writes over is not its to take: a merged row changed,
    pinned, locked, redacted, deleted or erased since the read."""

    drop, reason = "held_since_read", "a merged row changed since the read"


class _KeyTaken(_Unmerged):
    """No key was free for the summary: each holds a row the merge never read, another
    cluster of the pass took it, or (for a 'high' summary) a merged row sits there."""

    drop, reason = "key_taken", "no key was free for the summary"


class _VectorOwed(Exception):
    """A committed summary's vector failed: under Postgres the delivery worker writes it.
    Raised out of `then`, not logged there, so the commit leaves it owed."""


class _EmbedFailed(_Unmerged):
    """The summary's text could not be embedded."""

    drop, reason = "embed_failed", "the summary could not be embedded"


def _canonical_member(cluster: list[dict[str, Any]], canonical_key: str) -> dict[str, Any] | None:
    """The merged row the canonical row writes over: the one at its identity key."""
    return next(
        (
            r
            for r in cluster
            if r["memory_key"] == canonical_key and r.get("session_id") == _CANONICAL_SESSION
        ),
        None,
    )


def _held_since(row: memory_repo.MemoryRow, read_sha: str) -> bool:
    """Whether `row`, a head the merge read as holding `read_sha`, holds other text now,
    is redacted (a redaction keeps the hash) or is pinned or locked against a merge."""
    return (
        hash_text(row.content_sha256) != read_sha
        or row.redacted_at is not None
        or bool(row.pinned)
        or refuses_retirement(row)
    )


def _cluster_key(canonical_key: str, cluster: list[dict[str, Any]]) -> str:
    """The model's key made the cluster's own, for when the model's key is taken."""
    rows = ",".join(sorted(str(r["memory_id"]) for r in cluster))
    return f"{canonical_key}-{sha256_value(rows)[:8]}"


async def _summary_target(
    input: MemoryConsolidateInput,
    cluster: list[dict[str, Any]],
    canonical_key: str,
    claimed: set[str],
    *,
    high: bool,
) -> tuple[str, memory_repo.MemoryRow | None]:
    """The key the summary is written at, and the head read there: the model's key, or
    the cluster's own (`_cluster_key`) when another cluster of the pass claimed it or it
    holds a live row the merge never read. A merged row at the key must still be as the
    merge read it (`_held_since`). A `high` summary never takes a merged row's key: that
    row's vectors are retired with it rather than left for a fence to hide."""
    for key in (canonical_key, _cluster_key(canonical_key, cluster)):
        over = _canonical_member(cluster, key)
        if key in claimed or (high and over is not None):
            continue
        claimed.add(key)  # before the read: the pass's clusters run concurrently
        head = await memory_repo.get_by_key(
            memory_repo.Identity(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id,
                session_id=_CANONICAL_SESSION,
            ),
            memory_key=key,
            fresh=True,
            strong=True,
            scope=WHOLE_NAMESPACE,
            include_tombstoned=True,  # a deleted head is written over, above it
        )
        live = head if head is not None and head.tombstoned_at is None else None
        if over is None and live is not None:
            claimed.discard(key)  # another cluster may own the row there
            continue
        if over is not None and (
            live is None
            or str(live.memory_id) != str(over["memory_id"])
            or _held_since(live, hash_text(over.get("content_sha256")))
        ):
            raise _HeldSince(key)
        return key, head
    raise _KeyTaken(canonical_key)


async def _write_canonical(
    ch: Any,
    input: MemoryConsolidateInput,
    embedder: Any,
    cluster: list[dict[str, Any]],
    *,
    canonical_key: str,
    canonical_value: str,
    now_naive: datetime,
    claimed: set[str],
) -> dict[str, Any]:
    """The summary, its derived_from lineage and its vector, written over the head of the
    key `_summary_target` picks; the vector under the commit's lease. A text that cannot be
    embedded raises `_EmbedFailed`, before anything is written. A merged row at the key
    changed since the read raises `_HeldSince`; under Postgres, a merged row deleted,
    rewritten, redacted or erased before the commit raises it too."""
    sensitivity = _summary_sensitivity(cluster, canonical_value)
    high = sensitivity == "high"
    try:  # before any key is claimed or read
        embedding = None if high else await embedder.embed(canonical_value)
    except Exception as e:  # re-raised: the cluster is recorded as failed
        log.warning("consolidate: the summary's embed failed: %s", error_summary(e))
        raise _EmbedFailed() from e
    canonical_key, head = await _summary_target(input, cluster, canonical_key, claimed, high=high)
    version = next_version() if head is None else memory_repo.version_over(head)
    canonical_row = _consolidation_canonical_row(
        input,
        cluster,
        canonical_key=canonical_key,
        canonical_value=canonical_value,
        content_sha256=sha256_value(canonical_value),
        version=version,
        now=now_naive,
        sensitivity=sensitivity,
    )

    from server.memory.app.high_fence import fence_committed, fenced_once_committed

    # A high summary gets no vector: its key is fenced instead, and a failed fence recorded
    # on either authority, since the key holds no merged row (`_summary_target`).
    fence = [(memory_repo.identity_key(canonical_row), version)] if high else []

    async def _vector() -> None:
        if embedding is None:
            return
        row = _canonical_embedding_row(input, embedder, canonical_row, embedding)
        try:
            await ch.insert(
                "loom.memory_embeddings",
                [row],
                settings={"async_insert": 0, "wait_for_async_insert": 1},
            )
        except Exception as e:  # the row is in: raised on ClickHouse, owed under Postgres
            if not pg.enabled():
                raise
            raise _VectorOwed(error_summary(e)) from e

    async def _landed() -> None:
        if fence and not pg.enabled():  # under Postgres `fenced_once_committed` fences it
            await fence_committed(fence, op="consolidate.merge", ch=ch)
        await _lineage()

    async def _lineage() -> None:
        # One belief-source link per merged row, so memory.provenance and erasure walk
        # from the summary down.
        from server.memory.app.evidence import link_belief_sources

        try:
            await link_belief_sources(
                org=input.org,
                namespace=input.namespace,
                memory_links=[(canonical_key, str(canonical_row["memory_id"]))],
                evidence_ids=[
                    str(r["memory_id"])
                    for r in cluster
                    if str(r["memory_id"]) != str(canonical_row["memory_id"])
                ],
                support_type="derived_from",
                ch=ch,
            )
        except Exception as e:  # noqa: BLE001 — lineage is additive
            log.warning("consolidate: derived_from links failed (non-fatal): %s", error_summary(e))

    try:
        async with fenced_once_committed(lambda: fence, op="consolidate.merge", ch=ch) as answer:
            await memory_repo.insert_versions_over_heads(
                [canonical_row],
                [head],
                ch=ch,
                then=_vector,
                landed=_landed,
                documented=[embedding is not None],
                sources=[(str(r["memory_id"]), int(r["version"])) for r in cluster],
                live_sources=True,  # a merged row deleted or rewritten since refuses it too
                on_commit=answer.mark_committed,
            )
    except (AdmissionContendedError, MemoryErased) as e:
        raise _HeldSince(canonical_key) from e
    except _VectorOwed as owed:  # the delivery worker builds it
        log.warning("consolidate: the summary's vector is left owed: %s", owed)
    return canonical_row


def _canonical_embedding_row(
    input: MemoryConsolidateInput, embedder: Any, row: dict[str, Any], embedding: list[float]
) -> dict[str, Any]:
    return memory_repo.build_embedding_row(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=_CANONICAL_SESSION,
        memory_key=row["memory_key"],
        memory_type=row["memory_type"],
        scope="private",
        content_excerpt=str(row["value"])[:256],
        embedding_model=embedder.model_name,
        embedding_dim=embedder.dim,
        embedding=embedding,
        confidence=1.0,
        tombstoned_at=None,
        created_at=row["created_at"],
        version=int(row["version"]),
        tags=list(row["tags"]),
        entities=list(row["entities"]),
        temporal_anchor=row["temporal_anchor"],
        temporal_confidence=float(row.get("temporal_confidence") or 0.0),
        valid_at=row["valid_at"],
        files=list(row["files"]),
        concepts=list(row["concepts"]),
    )


async def _retire_merged(
    ch: Any,
    input: MemoryConsolidateInput,
    cluster: list[dict[str, Any]],
    *,
    canonical_key: str,
    now_naive: datetime,
) -> bool:
    """Tombstone the merged-from rows (except the canonical itself); False when one is
    left live beside the summary: held since the read, or moving through every admission
    round under Postgres. The tombstone is a NEW RMT version, so it must mirror EVERY
    persisted column: an omitted column would be zeroed under FINAL (memory_id/tags/
    entities/created_at/pinned/content_sha256) and leave memory.restore nothing to
    restore."""
    from server.memory.app.set_chunks import tombstone_set_chunks_fail_open

    kept = _canonical_member(cluster, canonical_key)
    if kept is not None:
        # The summary wrote over it and has no verbatim chunks: the old value's go.
        await tombstone_set_chunks_fail_open(
            org=input.org,
            namespace=input.namespace,
            memory_key=canonical_key,
            user_id=input.user_id,
            ch=ch,
        )
    tombstone_ids = [str(r["memory_id"]) for r in cluster if r is not kept]

    # Each above the head it reads, as a delete's tombstone, and only while that head holds
    # the text the merge read, unpinned and unlocked: a value written, a pin or a lock since
    # is not the merge's to undo. Its vector retire carries the tombstone's version.
    merged = {str(r["memory_id"]): hash_text(r.get("content_sha256")) for r in cluster}

    versions: dict[str, int] = {}
    held_ids: set[str] = set()  # a row deleted since is not held: it is gone

    def _skip_held(row: memory_repo.MemoryRow) -> bool:
        if _held_since(row, merged[str(row.memory_id)]):
            held_ids.add(str(row.memory_id))
            return True
        return False

    def _tombstone(existing: memory_repo.MemoryRow, _ts: datetime = now_naive) -> dict[str, Any]:
        versions[str(existing.memory_id)] = version = memory_repo.version_over(existing)
        return {"tombstoned_at": _ts, "expires_at": None, "version": version}

    # Under Postgres one moved head refuses the batch: read again, it is skipped as held.
    # On ClickHouse nothing refuses it, so the first round writes.
    for _ in range(settings.pg_admission_rounds):
        try:
            await memory_repo.write_versions_by_ids(
                input.org,
                tombstone_ids,
                _tombstone,
                namespace=input.namespace,
                ch=ch,
                skip=_skip_held,
                strong=True,
            )
            break
        except (AdmissionContendedError, MemoryErased) as e:
            versions.clear()
            held_ids.clear()
            log.info("memory.consolidate: merged rows moved; read again: %s", error_summary(e))
    else:
        log.warning("memory.consolidate: merged rows kept changing; left live beside the summary")
        record_drop(
            input.org,
            input.namespace,
            "consolidate.merge",
            "retire_contended",
            item=",".join(str(r["memory_key"]) for r in cluster if r is not kept),
            n=len(tombstone_ids),
        )
        return False
    held = [r for r in cluster if str(r["memory_id"]) in held_ids]
    if held:
        # held since the read (`_held_since`): not the merge's to retire
        record_drop(
            input.org,
            input.namespace,
            "consolidate.merge",
            "merged_row_left_live",
            item=",".join(str(r["memory_key"]) for r in held),
            n=len(held),
        )
    # Search reads loom.memory_embeddings, so the merged-from rows must be
    # tombstoned THERE too or they keep surfacing beside the canonical —
    # the duplicate the merge was meant to remove. Non-fatal per row: the
    # memory-table tombstone is durable, and a stray embedding is a
    # ranking nuisance, not data loss.
    for r in cluster:
        version = versions.get(str(r["memory_id"]))
        if r is kept or version is None:
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
                error_summary(e),
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
    return not held


@staged("consolidate_merge")
async def handle_memory_consolidate(
    input: MemoryConsolidateInput,
) -> MemoryConsolidateOutput:
    """Cluster semantically-similar active memories and merge each cluster into
    one canonical memory via the LLM.

    Fetch active memories under identity (age >= min_age_seconds), cluster by
    cosine similarity + entity overlap, ask the LLM to merge each cluster of >= 2
    into a canonical key/value, then write the canonical row over its key's head and
    tombstone the merged-from keys. `dry_run=True` skips the write step.
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
    # Under Postgres, only the rows the authority still serves: a delete, a rewrite or a
    # redaction ClickHouse has not published is never merged back in.
    rows = await memory_repo.served_rows(rows)

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

    slots: list[MemoryConsolidateResult | None] = [None] * len(merge_clusters)
    claimed: set[str] = set()  # the summaries' keys, one cluster each
    _sem = _asyncio.Semaphore(max(1, int(settings.consolidation_merge_concurrency)))

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

            async def _failed(*, status: str, reason: str, drop: str = "") -> None:
                # Recorded like a written merge, so the Admin UI and the drop ledger see
                # every cluster examined.
                out.clusters_failed += 1
                record_drop(
                    input.org,
                    input.namespace,
                    "consolidate.merge",
                    drop or status,
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
                        status=status,
                        failure_reason=reason,
                    )

            if not canonical_key or not canonical_value:
                failure_reason = failure_reason or _NO_CANONICAL
                log.warning(
                    "memory.consolidate: merge failed for %s/%s cluster %s: %s",
                    input.org,
                    input.namespace,
                    merged_keys,
                    failure_reason,
                )
                if refused_lossy:
                    out.clusters_refused_lossy += 1
                status = "refused_lossy" if refused_lossy else "failed"
                await _failed(status=status, reason=failure_reason)
                return

            if input.dry_run:
                slots[slot] = MemoryConsolidateResult(
                    canonical_key=canonical_key,
                    canonical_value=canonical_value,
                    merged_keys=merged_keys,
                    cluster_size=len(cluster),
                )
                return

            now_naive = now_utc.replace(tzinfo=None)
            try:
                canonical_row = await _write_canonical(
                    ch,
                    input,
                    embedder,
                    cluster,
                    canonical_key=canonical_key,
                    canonical_value=canonical_value,
                    now_naive=now_naive,
                    claimed=claimed,
                )
            except _Unmerged as e:  # the next pass merges what the rows hold then
                log.warning("memory.consolidate: cluster %s not merged: %s", merged_keys, e.drop)
                await _failed(status="failed", reason=e.reason, drop=e.drop)
                return
            canonical_key = str(canonical_row["memory_key"])  # the key written
            slots[slot] = MemoryConsolidateResult(
                canonical_key=canonical_key,
                canonical_value=canonical_value,
                merged_keys=merged_keys,
                cluster_size=len(cluster),
            )
            retired = await _retire_merged(
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
                failure_reason="" if retired else f"{_LEFT_LIVE}changed since the read",
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
    out = [
        ConsolidationRunRow(**{**r, "failure_reason": _shown_failure(str(r["failure_reason"]))})
        for r in rows
    ]
    return ConsolidationsListOutput(rows=out)


def _shown_failure(reason: str) -> str:
    """A run's `failure_reason` as `consolidations.list` returns it: a reason Loom
    writes in words (a lossy merge, an unreadable merge response) whole; any other in
    `caller_text`'s shape, since a stored `<Kind>: <text>` can carry the provider's or
    the store's text; a fixed sentence for text in neither shape."""
    if not reason:
        return reason
    # A parser reason may be stored after its kind (`ValueError: ...`): the same reason.
    own = reason.removeprefix("ValueError: ")
    unmerged = {c.reason for c in (_HeldSince, _KeyTaken, _EmbedFailed)}
    if own.startswith(_OWN_REASONS) or own in unmerged or _SUMMARY.fullmatch(reason):
        return own
    return stored_caller_text(reason) or _UNSHOWN_FAILURE
