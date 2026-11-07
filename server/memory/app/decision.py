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
from typing import Any

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
from server.memory.app.sensitivity_level import resolve_level
from server.memory.app.set_commit import count_attribute_tags, count_write
from server.memory.domain.decision_schema import (
    DECISION_TAG,
    FAILURE_TAG,
    OUTCOME_KNOWN_TAG,
    PROPOSED_TAG,
    SCHEMA_FIELDS,
    SCHEMA_NAME,
    SUCCESS_TAG,
)
from server.memory.domain.decision_statement import alternatives, memory_value
from server.memory.domain.provenance import DeclaredSourceClass, is_declarable
from server.memory.domain.sensitivity import (
    ATTRIBUTE_TAG_PREFIX,
    SENSITIVITY_TAG_PREFIX,
    Sensitivity,
    attribute_tags,
    detector_text,
)
from server.memory.store import (
    Identity,
    MemoryRow,
    WriterRecord,
    active_decision_records,
    active_record,
    get_by_key,
    live_writer_record,
    next_version,
    tombstone_decision_records,
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
    """Active decision records by key: {memory_key: record_json + made_at + stage}."""
    if not keys:
        return {}
    rows = await active_decision_records(org, namespace, keys, ch=ch)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        rec = svc.parse_record_json(r.record_json)
        rec["made_at"] = r.created_at.isoformat()
        rec["stage"] = svc.stage_of(r.proposed, rec)
        out[r.memory_key] = rec
    return out


def _row(key: str, rec: dict[str, Any], similarity: float = 0.0) -> Any:
    conf = rec.get("confidence")
    succ = rec.get("success")
    return DecisionRow(
        memory_key=key,
        category=str(rec.get("category") or ""),
        scenario=str(rec.get("scenario") or ""),
        reasoning=str(rec.get("reasoning") or ""),
        outcome=str(rec.get("outcome") or ""),
        alternatives=alternatives(rec),
        confidence=float(conf) if isinstance(conf, (int, float)) else None,
        decision_maker=str(rec.get("decision_maker") or ""),
        run_id=str(rec.get("run_id") or ""),
        realized_outcome=str(rec.get("realized_outcome") or ""),
        success=bool(succ) if isinstance(succ, bool) else None,
        made_at=str(rec.get("made_at") or ""),
        stage=rec["stage"],
        similarity=float(similarity),
    )


# ------------------------------------------------------------------ tags


# The server-set tags an outcome's rewrite drops from the row before rebuilding: those of
# `_decision_tags`, and the stage tag, since an outcome records that the decision was acted
# on. The caller's tags are kept as the row carries them.
_OUTCOME_DROPPED_TAGS = (DECISION_TAG, OUTCOME_KNOWN_TAG, SUCCESS_TAG, FAILURE_TAG, PROPOSED_TAG)


def _category_tag(category: str) -> str:
    return f"decision:category:{category.strip().lower()}"


def _decision_tags(
    category: str, *, outcome_known: bool = False, success: bool | None = None
) -> list[str]:
    """The `decision` tags of a decision's memory row: the marker and the category, and
    after `decision.outcome` the outcome-known mark and its success or failure."""
    tags = [DECISION_TAG, _category_tag(category)]
    if outcome_known:
        tags.append(OUTCOME_KNOWN_TAG)
        if success is not None:
            tags.append(SUCCESS_TAG if success else FAILURE_TAG)
    return tags


# ------------------------------------------------------------------ handlers


def _pin_refusal(e: HTTPException) -> dict[str, Any] | None:
    """The refusal's detail when memory.set refused because the writer's row is pinned. A
    decision's conflict check never refuses for a pinned peer, so it is always that row."""
    detail: dict[str, Any] = e.detail if isinstance(e.detail, dict) else {}
    return detail if detail.get("code") == "PINNED_CONFLICT" else None


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
    files: list[str] | None,
    source_class: DeclaredSourceClass | None,
    source_resource_id: str,
    if_match_sha256: str = "",
    pinned: bool = False,
    revises: WriterRecord | None = None,
) -> str:
    """Memory row + record under one key; returns the record_id. The row goes first: a
    delete keeps the record only while its writer's row is live, so that row must exist
    before the record does. `if_match_sha256` makes the row write conditional on the live
    row's hash (memory.set answers 409 PRECONDITION_FAILED otherwise); `pinned` keeps a
    pinned row's pin. `revises` is the record an outcome read: a re-record since then may
    leave the row's hash as it was, so the record is checked again before the row and
    before the record are written."""
    from server.memory.app.records import handle_memory_record_write
    from server.memory.app.write import handle_memory_set

    statement, sensitivity, added = await _checked_statement(org, namespace, payload, tags)

    async def check_revised() -> None:
        if revises is None:
            return
        live = await live_writer_record(org, namespace, key, ch=get_client())
        if live is None:
            raise _no_active_decision(org, namespace, key)
        if live != revises:
            raise _decision_changed()

    await check_revised()
    conf = payload.get("confidence")
    await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=namespace,
            agent=agent,
            user_id=user_id or "-",
            session_id=session_id or "-",
            run_id=run_id or "-",
            memory_key=key,
            memory_type="decision",
            value=statement,
            tags=[*tags, *added],
            confidence=float(conf) if isinstance(conf, (int, float)) and conf > 0 else 1.0,
            source_trace=f"run:{run_id}" if named(run_id) else "decision",
            valid_at=valid_at,
            files=files,
            source_class=source_class,
            source_resource_id=source_resource_id,
            sensitivity=sensitivity,
            if_match_sha256=if_match_sha256,
            pinned=pinned,
        )
    )
    count_attribute_tags(namespace, added)
    await check_revised()
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
        await tombstone_decision_records(org, namespace, [key], version=next_version())
    except Exception as e:  # noqa: BLE001 — the decision is written
        log.warning("decision: closing record check failed for %s: %s", key, e)
    return str(rec_out.record_id)


async def _checked_statement(
    org: str, namespace: str, payload: dict[str, Any], tags: list[str]
) -> tuple[str, Sensitivity, list[str]]:
    """Run memory.set's blacklist, sensitivity and attribute checks on the whole decision
    before the record is written, since the row's statement leaves some fields out. Returns
    (statement, sensitivity, attribute labels to add). A blacklisted decision raises."""
    from server.memory.app.blacklist_guard import enforce_on_insert

    # The stage rides the row's tag, not the record.
    statement = memory_value(payload, proposed=PROPOSED_TAG in tags)
    try:
        # Every field: the statement leaves out the category, maker and run, and memory.set
        # checks only it.
        full_text = "\n".join(svc.blacklist_fields(payload))
        await enforce_on_insert(org=org, namespace=namespace, value=full_text)
    except HTTPException:
        count_write(namespace, "blacklisted")
        raise
    except Exception:
        # An accounting hook, as in memory.set: an outage counts as `error`, re-raised.
        count_write(namespace, "error")
        raise
    fields = svc.label_fields(payload)
    sensitivity = await resolve_level("", detector_text([*fields, statement]))
    added: list[str] = []
    for field in fields:
        added += attribute_tags(field, [*tags, *added])
    return statement, sensitivity, added


async def handle_decision_record(input: DecisionRecordInput) -> DecisionRecordOutput:
    from server.memory.app.ops import handle_memory_link

    empty = svc.fields_without_key_text(input.category, input.scenario)
    if empty:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "MISSING_IDENTITY_FIELD",
                "message": f"category and scenario need a letter or digit to key on: {empty}",
                "fields": empty,
            },
        )
    ch = get_client()
    await ensure_schema(input.org, input.namespace)
    key = svc.decision_key(input.category, input.scenario)
    # superseded_prior needs only whether an active record exists.
    prior = await active_record(input.org, input.namespace, SCHEMA_NAME, key, ch=ch)
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
    # A caller's own decision:proposed tag never stands in for the stage.
    # Compared as a later tags-only write stores it: in lower case.
    caller_tags = [t for t in input.tags if t and t.strip().lower() != PROPOSED_TAG]
    stage_tags = [PROPOSED_TAG] if input.stage == "proposed" else []
    tags = [*_decision_tags(payload["category"]), *caller_tags, *stage_tags]
    try:
        record_id = await _write_decision(
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
            # Unset: the files the decision's text names, never its realized outcome's.
            files=input.files if input.files is not None else svc.decision_files(payload),
            source_class=input.source_class,
            source_resource_id=input.source_resource_id,
        )
    except HTTPException as e:
        refusal = _pin_refusal(e)
        if refusal is None:
            raise
        # memory.set's advice (memory.update, memory.pin) would change the row, not the record.
        raise HTTPException(
            status_code=409,
            detail={
                **refusal,
                "message": f"decision {key!r} is pinned; an admin unpins it with memory.unpin "
                "before it can be recorded again",
            },
        ) from None

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
        record_id=record_id,
        superseded_prior=prior is not None,
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
    from server.memory.app.search.scoring import one_per_key, per_key_fetch

    ch = get_client()
    tags = [_category_tag(input.category)] if input.category.strip() else []
    want = max(input.top_k * 2, 10)  # room for records without an outcome
    res = await handle_memory_search(
        MemorySearchInput(
            org=input.org,
            namespace=input.namespace,
            agent=input.agent or "*",
            query=input.query,
            top_k=per_key_fetch(want),
            memory_type="decision",
            tags=tags,
            namespace_scope="this",
            auto_touch=False,
        )
    )
    best = one_per_key(res.results, input.namespace, want)
    hits = [(h.memory_key, float(getattr(h, "similarity", 0.0) or 0.0)) for h in best]
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


def _outcome_payload(input: DecisionOutcomeInput, writer: WriterRecord) -> dict[str, Any]:
    """The writer's record with the outcome applied."""
    payload = svc.parse_record_json(writer.record_json)
    payload["realized_outcome"] = input.realized_outcome.strip()
    if input.success is not None:
        payload["success"] = input.success
    payload["made_at"] = writer.created_at.isoformat()
    return payload


def _outcome_tags(row: MemoryRow, payload: dict[str, Any]) -> list[str]:
    """The decision tags rebuilt for the outcome, then the caller's tags the row carries,
    without the stage tag (an outcome means the decision was acted on); the server's
    sensitivity and attribute labels are worked out again on the write."""
    category = str(payload.get("category") or "")
    rebuilt = {*_OUTCOME_DROPPED_TAGS, _category_tag(category)}
    server_labels = (SENSITIVITY_TAG_PREFIX, ATTRIBUTE_TAG_PREFIX)
    kept = [t for t in row.tags if t not in rebuilt and not t.startswith(server_labels)]
    success = payload.get("success")
    return [*_decision_tags(category, outcome_known=True, success=success), *kept]


def _decision_changed() -> PreconditionFailedError:
    return PreconditionFailedError(
        "the decision changed while its outcome was being written; call decision.outcome again"
    )


def _no_active_decision(org: str, namespace: str, key: str) -> NotFoundError:
    return NotFoundError(f"no active decision {key!r} in {org}/{namespace}", memory_key=key)


async def handle_decision_outcome(input: DecisionOutcomeInput) -> DecisionOutcomeOutput:
    from server.memory.app.write import served_sha256

    ch = get_client()

    # Only the record's live writer revises it; record and writer come from one read.
    writer = await live_writer_record(input.org, input.namespace, input.memory_key, ch=ch)
    if writer is None:
        raise _no_active_decision(input.org, input.namespace, input.memory_key)
    payload = _outcome_payload(input, writer)
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
        raise _no_active_decision(input.org, input.namespace, input.memory_key)
    # Conditional on the row read with the record, so a delete since then is not written back.
    try:
        await _write_decision(
            org=input.org,
            namespace=input.namespace,
            agent=writer.agent,
            user_id=writer.user_id,
            session_id=writer.session_id,
            run_id=str(payload.get("run_id") or "-"),
            key=input.memory_key,
            payload=payload,
            tags=_outcome_tags(row, payload),
            valid_at=None,
            # The record holds neither files nor source: the row keeps its own, so the
            # outcome's text cannot swap them.
            files=row.files,
            source_class=row.source_class if is_declarable(row.source_class) else None,
            source_resource_id=row.source_resource_id,
            if_match_sha256=served_sha256(row),
            pinned=row.pinned,
            revises=writer,
        )
    except HTTPException as e:
        # memory.set's hash mismatch, its write fence finding the key deleted since the
        # check, or a pin since the read; the lease's busy PreconditionFailedError passes.
        mismatch = isinstance(e.detail, dict) and e.detail.get("code") == "PRECONDITION_FAILED"
        if not mismatch and _pin_refusal(e) is None:
            raise
        if await read_row() is None:
            raise _no_active_decision(input.org, input.namespace, input.memory_key) from None
        raise _decision_changed() from None

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
