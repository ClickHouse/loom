"""Decision handlers: record, precedents, chain, outcome.

Composition over new storage: a decision is a typed record (schema
`decision`, registered on first use per namespace) plus a searchable memory
row under the same key plus authored edges. Precedents = memory.search over
memory_type="decision"; the chain = BFS over loom.operator_edges; the
outcome = a record revision that also credits the deciding run's memories.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, NamedTuple

from fastapi import HTTPException

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now as _now
from server.core.errors import NotFoundError, PreconditionFailedError
from server.core.naming import SENTINEL, named
from server.identity.app.request_scope import request_scope
from server.memory.api.models_crud import MemorySetInput
from server.memory.api.models_decision import (
    ChainNodeRow,
    DecisionChainInput,
    DecisionChainOutput,
    DecisionOutcomeInput,
    DecisionOutcomeOutput,
    DecisionPrecedentsInput,
    DecisionPrecedentsOutput,
    DecisionRecordInput,
    DecisionRecordOutput,
    DecisionRow,
)
from server.memory.api.models_graph import MemoryLinkInput
from server.memory.api.models_records import (
    MemoryRecordWriteInput,
    MemorySchemaRegisterInput,
    SchemaFieldInput,
)
from server.memory.api.models_search import MemorySearchInput
from server.memory.app import decisions as svc
from server.memory.domain.decision_schema import SCHEMA_FIELDS, SCHEMA_NAME
from server.memory.store import (
    Identity,
    WriterRecord,
    active_decision_records,
    get_by_key,
    live_writer_record,
    next_version,
    tombstone_decision_record,
)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ schema


async def ensure_schema(org: str, namespace: str) -> None:
    """Register the built-in `decision` schema the first time a namespace
    records one. Idempotent: an active schema of that name is left alone."""
    from server.memory.app.records import _schema_by_name, handle_memory_schema_register

    existing = await _schema_by_name(org, namespace, SCHEMA_NAME)
    if existing and str(existing.get("status")) == "active":
        return
    await handle_memory_schema_register(
        MemorySchemaRegisterInput(
            org=org,
            namespace=namespace,
            name=SCHEMA_NAME,
            description="Built-in: a decision as a first-class record.",
            fields=[SchemaFieldInput(**f) for f in SCHEMA_FIELDS],
            created_by="loom:decision",
            status="active",
        )
    )


# -------------------------------------------------------------------- reads


async def _decision_records(
    ch: Any, org: str, namespace: str, keys: list[str]
) -> dict[str, dict[str, Any]]:
    """Active decision records by key: {memory_key: record_json + made_at}."""
    if not keys:
        return {}
    rows = await active_decision_records(org, namespace, keys, ch=ch)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        rec = svc.parse_record_json(r.record_json)
        rec["made_at"] = r.created_at.isoformat()
        out[r.memory_key] = rec
    return out


def _row(key: str, rec: dict[str, Any], similarity: float = 0.0) -> Any:
    alts = rec.get("alternatives") or []
    if isinstance(alts, str):
        alts = [a for a in alts.split("\n") if a.strip()]
    conf = rec.get("confidence")
    succ = rec.get("success")
    return DecisionRow(
        memory_key=key,
        category=str(rec.get("category") or ""),
        scenario=str(rec.get("scenario") or ""),
        reasoning=str(rec.get("reasoning") or ""),
        outcome=str(rec.get("outcome") or ""),
        alternatives=[str(a) for a in alts],
        confidence=float(conf) if isinstance(conf, (int, float)) else None,
        decision_maker=str(rec.get("decision_maker") or ""),
        run_id=str(rec.get("run_id") or ""),
        realized_outcome=str(rec.get("realized_outcome") or ""),
        success=bool(succ) if isinstance(succ, bool) else None,
        made_at=str(rec.get("made_at") or ""),
        similarity=float(similarity),
    )


# ------------------------------------------------------------------ handlers


class _Written(NamedTuple):
    """What `_write_decision` wrote: the record id, or None and the key memory.set merged the
    row into ("" if that row could not be read)."""

    record_id: str | None
    merged_into_key: str = ""


def _merged_hint(merged_into_key: str, op: str) -> str:
    """Why `op` wrote nothing after memory.set merged its row."""
    if not merged_into_key:
        return (
            "memory.set merged this into a decision that can no longer be read; nothing was "
            f"written. Call {op} again."
        )
    return (
        f"memory.set merged this into {merged_into_key}, a decision with the same text; "
        "nothing was written."
    )


async def _write_decision(
    *,
    org: str,
    namespace: str,
    agent: str,
    user_id: str,
    session_id: str,
    run_id: str,
    key: str,
    payload: dict[str, Any],
    tags: list[str],
    valid_at: datetime | None,
    if_match_sha256: str = "",
) -> _Written:
    """Memory row + record under one key. The row goes first: a delete keeps the record
    only while its writer's row is live, so that row must exist before the record does.
    `if_match_sha256` makes the row write conditional on the live row's hash (memory.set
    answers 409 PRECONDITION_FAILED otherwise)."""
    from server.memory.app.records import handle_memory_record_write
    from server.memory.app.write import handle_memory_set

    conf = payload.get("confidence")
    set_out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=namespace,
            agent=agent,
            user_id=user_id or "-",
            session_id=session_id or "-",
            run_id=run_id or "-",
            memory_key=key,
            memory_type="decision",
            value=svc.memory_value(payload),
            tags=tags,
            confidence=float(conf) if isinstance(conf, (int, float)) and conf > 0 else 1.0,
            source_trace=f"run:{run_id}" if named(run_id) else "decision",
            valid_at=valid_at,
            if_match_sha256=if_match_sha256,
        )
    )
    if set_out.conflict_verdict == "merge":
        # memory.set stored no row, so a record here would not match the writer's row.
        return _Written(None, set_out.merged_into_key)
    rec_out = await handle_memory_record_write(
        MemoryRecordWriteInput(
            org=org,
            namespace=namespace,
            schema_name=SCHEMA_NAME,
            memory_key=key,
            agent=agent,
            user_id=user_id or "-",
            session_id=session_id or "-",
            fields=svc.record_fields(payload),  # type: ignore[arg-type]
            valid_at=valid_at,
        )
    )
    # Retire the record if the writer's row was deleted before the record landed.
    try:
        await tombstone_decision_record(
            org, namespace, key, version=next_version(), ch=get_client()
        )
    except Exception as e:  # noqa: BLE001 — the decision is written
        log.warning("decision: closing record check failed for %s: %s", key, e)
    return _Written(str(rec_out.record_id))


async def handle_decision_record(input: DecisionRecordInput) -> DecisionRecordOutput:
    from server.memory.app.ops import handle_memory_link

    ch = get_client()
    await ensure_schema(input.org, input.namespace)
    key = svc.decision_key(input.category, input.scenario)
    prior = await _decision_records(ch, input.org, input.namespace, [key])
    made_at = (input.valid_at or _now()).isoformat()
    payload: dict[str, Any] = {
        "category": input.category.strip(),
        "scenario": input.scenario.strip(),
        "reasoning": input.reasoning.strip(),
        "outcome": input.outcome.strip(),
        "alternatives": svc.clean_alternatives(input.alternatives),
        "confidence": float(input.confidence) if input.confidence > 0 else None,
        "decision_maker": input.decision_maker.strip() or input.agent,
        "run_id": named(input.run_id),
        "made_at": made_at,
    }
    tags = [
        "decision",
        f"decision:category:{payload['category'].lower()}",
        *[t for t in input.tags if t],
    ]
    written = await _write_decision(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        run_id=input.run_id,
        key=key,
        payload=payload,
        tags=tags,
        valid_at=input.valid_at,
    )
    if written.record_id is None:
        return DecisionRecordOutput(
            memory_key=written.merged_into_key,
            record_id="",
            next=_merged_hint(written.merged_into_key, "decision.record"),
        )

    edges = 0
    evidence = [f"decision.record: {payload['scenario'][:200]}"]
    for rel, keys in (("caused", input.caused_by), ("influenced", input.influenced_by)):
        for src in dict.fromkeys(k.strip() for k in keys if k and k.strip()):
            if src == key:
                continue
            try:
                await handle_memory_link(
                    MemoryLinkInput(
                        org=input.org,
                        namespace=input.namespace,
                        from_key=src,
                        to_key=key,
                        relation=rel,
                        explanation=f"{src} {rel} the decision {payload['outcome'][:120]!r}",
                        evidence=evidence,
                        confidence=float(input.confidence) if input.confidence > 0 else 1.0,
                        run_id=named(input.run_id) or SENTINEL,
                    )
                )
                edges += 1
            except Exception as e:  # noqa: BLE001 — an edge never fails the record
                log.warning("decision.record: edge %s -%s-> %s failed: %s", src, rel, key, e)

    return DecisionRecordOutput(
        memory_key=key,
        record_id=written.record_id,
        superseded_prior=key in prior,
        edges_written=edges,
        next=(
            f"decision {key} recorded. When the outcome is known, call "
            "decision.outcome(memory_key, realized_outcome, success) — it also credits "
            "the memories the run relied on. Before a similar decision, call "
            "decision.precedents."
        ),
    )


async def handle_decision_precedents(input: DecisionPrecedentsInput) -> DecisionPrecedentsOutput:
    from server.memory.app.search import handle_memory_search

    ch = get_client()
    tags = [f"decision:category:{input.category.strip().lower()}"] if input.category.strip() else []
    res = await handle_memory_search(
        MemorySearchInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent or "*",
            query=input.query,
            top_k=max(input.top_k * 2, 10),
            memory_type="decision",
            tags=tags,
            namespace_scope="this",
            auto_touch=False,
        )
    )
    hits = [(h.memory_key, float(getattr(h, "similarity", 0.0) or 0.0)) for h in res.results]
    records = await _decision_records(ch, input.org, input.namespace, [k for k, _ in hits])
    rows = []
    for key, sim in hits:
        rec = records.get(key)
        if rec is None:
            continue
        if input.with_outcome_only and not rec.get("realized_outcome"):
            continue
        rows.append(_row(key, rec, sim))
        if len(rows) >= input.top_k:
            break
    return DecisionPrecedentsOutput(precedents=rows, embedding_model=res.embedding_model)


async def handle_decision_chain(input: DecisionChainInput) -> DecisionChainOutput:
    ch = get_client()

    async def fetch(frontier: list[str], direction: str) -> list[tuple[str, str, str]]:
        col = "to_key" if direction == "upstream" else "from_key"
        rows = await ch.query_all(
            f"""
            SELECT from_key, to_key, relation FROM loom.operator_edges FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND {col} IN {{keys:Array(String)}}
              AND relation IN {{rels:Array(String)}} AND tombstoned_at IS NULL
            LIMIT 1000
            """,
            params={
                "org": input.org,
                "ns": input.namespace,
                "keys": list(frontier),
                "rels": list(svc.CHAIN_RELATIONS),
            },
        )
        return [(str(r["from_key"]), str(r["to_key"]), str(r["relation"])) for r in rows]

    nodes = await svc.walk_chain(
        input.memory_key, fetch=fetch, direction=input.direction, max_hops=input.max_hops
    )
    records = await _decision_records(
        ch, input.org, input.namespace, [input.memory_key, *(n.memory_key for n in nodes)]
    )
    seed_rec = records.get(input.memory_key)
    return DecisionChainOutput(
        seed=input.memory_key,
        seed_decision=_row(input.memory_key, seed_rec) if seed_rec else None,
        nodes=[
            ChainNodeRow(
                memory_key=n.memory_key,
                hop=n.hop,
                relation=n.relation,
                direction=n.direction,
                via=n.via,
                decision=(
                    _row(n.memory_key, records[n.memory_key]) if n.memory_key in records else None
                ),
            )
            for n in nodes
        ],
    )


def _outcome_payload(
    input: DecisionOutcomeInput, writer: WriterRecord
) -> tuple[dict[str, Any], list[str]]:
    """The writer's record with the outcome applied, and the row's tags."""
    payload = svc.parse_record_json(writer.record_json)
    payload["realized_outcome"] = input.realized_outcome.strip()
    if input.success is not None:
        payload["success"] = input.success
    payload["made_at"] = writer.created_at.isoformat()
    tags = [
        "decision",
        f"decision:category:{str(payload.get('category') or '').lower()}",
        "decision:outcome-known",
    ]
    if payload.get("success") is not None:
        tags.append("decision:success" if payload["success"] else "decision:failure")
    return payload, tags


def _no_active_decision(input: DecisionOutcomeInput) -> NotFoundError:
    return NotFoundError(
        f"no active decision {input.memory_key!r} in {input.org}/{input.namespace}",
        memory_key=input.memory_key,
    )


async def handle_decision_outcome(input: DecisionOutcomeInput) -> DecisionOutcomeOutput:
    from server.memory.app.write import stored_sha256

    ch = get_client()

    # Only the record's live writer revises it; record and writer come from one read.
    writer = await live_writer_record(input.org, input.namespace, input.memory_key, ch=ch)
    if writer is None:
        raise _no_active_decision(input)
    payload, tags = _outcome_payload(input, writer)
    identity = Identity(
        org=input.org,
        namespace=input.namespace,
        agent=writer.agent,
        user_id=writer.user_id,
        session_id=writer.session_id,
    )

    async def read_row() -> Any:
        return await get_by_key(
            identity, input.memory_key, fresh=True, strong=True, scope=request_scope()
        )

    row = await read_row()
    if row is None:
        raise _no_active_decision(input)
    # Conditional on the row read with the record, so a delete since then is not written back.
    try:
        written = await _write_decision(
            org=input.org,
            namespace=input.namespace,
            agent=writer.agent,
            user_id=writer.user_id,
            session_id=writer.session_id,
            run_id=str(payload.get("run_id") or "-"),
            key=input.memory_key,
            payload=payload,
            tags=tags,
            valid_at=None,
            if_match_sha256=stored_sha256(row),
        )
    except HTTPException as e:
        # memory.set's hash mismatch, or its write fence finding the key deleted since the
        # check; the lease's busy PreconditionFailedError passes through.
        if not (isinstance(e.detail, dict) and e.detail.get("code") == "PRECONDITION_FAILED"):
            raise
        if await read_row() is None:
            raise _no_active_decision(input) from None
        raise PreconditionFailedError(
            "the decision changed while its outcome was being written; call decision.outcome again"
        ) from None
    if written.record_id is None:
        # Nothing was stored, so the run's memories earn no credit from it.
        return DecisionOutcomeOutput(
            memory_key=input.memory_key,
            updated=False,
            next=_merged_hint(written.merged_into_key, "decision.outcome"),
        )

    # Close the loop: the memories the deciding run relied on earn (or lose)
    # credit from the realized outcome — the feedback every decision log lacks.
    credited = 0
    run_id = str(payload.get("run_id") or "")
    if input.success is not None and named(run_id):
        try:
            from server.memory.api.models_temporal import MemoryOutcomeRunInput
            from server.memory.app.ops import handle_memory_outcome_run

            run_row = await ch.query_one(
                """
                SELECT memories_used FROM loom.agent_runs FINAL
                WHERE org = {org:String} AND namespace = {ns:String} AND run_id = {rid:String}
                LIMIT 1
                """,
                params={"org": input.org, "ns": input.namespace, "rid": run_id},
            )
            used = [str(k) for k in ((run_row or {}).get("memories_used") or [])]
            if used:
                out = await handle_memory_outcome_run(
                    MemoryOutcomeRunInput(
                        org=input.org,
                        namespace=input.namespace,
                        outcome="positive" if input.success else "negative",
                        memory_keys=used,
                        run_id=run_id,
                        source=f"decision:{input.memory_key}",
                    )
                )
                credited = int(getattr(out, "applied", 0) or 0) if input.success else len(used)
        except Exception as e:  # noqa: BLE001 — credit is additive
            log.warning("decision.outcome: run credit skipped for %s: %s", run_id, e)
    return DecisionOutcomeOutput(
        memory_key=input.memory_key, updated=True, memories_credited=credited
    )
