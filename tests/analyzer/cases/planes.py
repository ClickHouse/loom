"""The operation planes outside search: every read-shaped handler driven
through its Input model (required fields from the identity, optional
fields empty and then filled with typed dummies — the `filled` axis), and
the repositories / services those handlers and the workers read through.

Handlers that call an LLM or an embedder (set, add, consolidate, ask,
search) are left out: their statements are covered from the repositories
they call, and a provider round trip has no place in a statement corpus.
"""

from __future__ import annotations

import datetime as _dt
import importlib
import inspect
import sys
import types
import typing
from typing import Any

from server.auth.context import current_auth
from server.core.tag_sql import TagFilter
from server.identity.domain.read_scope import (
    WHOLE_NAMESPACE,
    ReadFilters,
    read_filters_bound,
    scope_and,
)
from tests.analyzer.cases._common import AGENT, NS, ORG, SMALL_VEC, rows_by_substring
from tests.analyzer.corpus import Case, RecordingCH, is_drive_defect

# An org admin's scope as `resolve_org_scope` builds it: a hidden restricted namespace and
# the collections it cannot read (`tests/unit/test_projection_failures_org_scope.py` pins
# it to the builder).
ADMIN_SCOPE = (
    " AND (NOT startsWith(namespace, {col_prefix:String})"
    " OR namespace IN {col_readable:Array(String)})"
    " AND namespace NOT IN {ns_hidden:Array(String)}"
)
ADMIN_SCOPE_PARAMS = {"col_prefix": "col-", "col_readable": [NS], "ns_hidden": ["restricted"]}

_UUID = "00000000-0000-0000-0000-000000000001"
_DT = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

# Required-field values by name; anything else gets a typed dummy.
_BY_NAME: dict[str, Any] = {
    "org": ORG,
    "from_org": ORG,
    "namespace": NS,
    "from_namespace": NS,
    "to_namespace": "n2",
    "target_namespace": "n2",
    "agent": AGENT,
    "from_agent": AGENT,
    "to_agent": "bot2",
    "claiming_agent": "bot2",
    "memory_key": "k1",
    "from_key": "k1",
    "to_key": "k2",
    "memory_id": _UUID,
    "from_memory_id": _UUID,
    "record_id": _UUID,
    "erasure_id": _UUID,
    "run_id": _UUID,
    "proposal_id": _UUID,
    "pattern_id": _UUID,
    "failure_ids": [_UUID],
    "policy_id": "p1",
    "candidate_id": _UUID,
    "thread_id": "t1",
    "confirm_thread_id": "t1",
    "session_id": "s1",
    "schema_name": "ticket",
    "name": "ticket",
    "owner_email": "owner@example.test",
    "subject": "corpus@example.test",
    "role": "writer",
    "decision": "approve",
    "outcome": "success",
    "realized_outcome": "success",
    "operation": "memory.get",
    "query": "deploy window",
    "seed_entity": "user",
    "entity": "user",
    "entity_a": "user",
    "entity_b": "clickhouse",
    "adapter_name": "adapter",
    "source_uri": "doc://a",
    "source_uris": ["doc://a"],
    "canonical_uri": "doc://a",
    "duplicate_uri": "doc://b",
    "confirm_duplicate_uri": "doc://b",
    "canonical_id": _UUID,
    "duplicate_id": _UUID,
    "confirm_canonical_key": "k1",
    "memory_keys": ["k1", "k2"],
    "memory_key_a": "k1",
    "memory_key_b": "k2",
    "blob_key": "b1",
    "from_date": "2026-01-01",
    "to_date": "2026-02-01",
    "as_of": "2026-01-01T00:00:00Z",
    "category_name": "work",
    "kind": "category",
    "pattern": "secret",
    "fields": {"title": "x"},
    "payload": {},
    "path": "src/a.py",
    "target": "k1",
    "action": "keep",
    "reason": "corpus",
    "ttl_seconds": 60,
    "value": "v",
    "batch_id": _UUID,
    "label": "notes",
    "addition": "x",
    "old": "x",
    "new": "y",
    "goal": "g",
    "status": "done",
    "confirm_subject": "corpus@example.test",
    "tool_name": "t",
    "trace": "t",
    "situation": "s",
    "steps": ["a"],
    "graph": {},
    "question": "q",
    "locations": [],
    "git_head": "abc",
    "cursor": "",
}

_SKIP_HANDLERS = {
    # LLM / embedder round trips, or writers whose statements the repos render.
    "handle_memory_set",
    "handle_memory_pin",
    "handle_memory_update",
    "handle_memory_update_by_id",
    "handle_memory_consolidate",
    "handle_memory_reindex",
    "handle_knowledge_add",
    "handle_knowledge_bulk_add",
    "handle_knowledge_upsert",
    "handle_knowledge_synthesize",
    "handle_knowledge_search",
    "handle_memory_import_curated",
    "handle_conversation_append",
    "handle_conversation_compact",
    "handle_decision_record",
    "handle_decision_precedents",
    "handle_observe_record",
    "handle_observe_crystallize",
    "handle_cost_record_turns",
    "handle_namespace_restore",
    "handle_memory_clear",
    "handle_memory_copy_to",
    "handle_memory_cross_promote",
    "handle_erasure_request",
    "handle_memory_delete",
    "handle_memory_redact",
    "handle_memory_outcome_run",
    "handle_memory_record_write",
    "handle_blacklist_sweep",
    "handle_memory_stack",
    "handle_working_memory_set",
    "handle_working_memory_append",
    "handle_working_memory_replace",
    "handle_context_offload",
    "handle_run_begin",
    "handle_memory_resolve_orphan_duplicates",
}

# Handler modules whose optional input fields reach no statement: the
# filled input renders the same shapes as the empty one.
_FILL_INERT = frozenset(
    {
        "namespace_validate",
        "grants",
        "context_offload",
        "knowledge",
        "_memory_consolidate",
        "_memory_evidence_reads",
        "_memory_restore",
        "_namespace_membership",
        "_namespace_snapshot",
        "_namespace_catalog",
        # The handler takes its scope from the admission binding, never its input's
        # fields; population_limit is a bound parameter.
        "_namespace_inventory",
    }
)

# Handlers whose `input` parameter carries no annotation.
_UNTYPED = {
    "handle_memory_digests": "MemoryDigestsInput",
    "handle_memory_outline": "MemoryOutlineInput",
    "handle_memory_backlinks": "MemoryBacklinksInput",
    "handle_memory_history": "MemoryHistoryInput",
    "handle_memory_timeline": "MemoryTimelineInput",
    "handle_admin_users": "AdminUsersInput",
    "handle_agents_connected": "AgentsConnectedInput",
    "handle_erasure_list": "ErasureListInput",
    "handle_conversation_threads": "ConversationThreadsInput",
    "handle_knowledge_update_metadata": "KnowledgeUpdateMetadataInput",
    "handle_memory_get_by_id": "MemoryGetByIdInput",
    "handle_memory_restore": "MemoryRestoreInput",
    "handle_memory_evidence": "MemoryEvidenceInput",
}

# (case name, handler module): the case name is the analyzer shape id, the module
# is where that plane's handlers live.
HANDLER_MODULES = (
    ("facts", "server.memory.app.facts"),
    ("namespace", "server.identity.app.namespace"),
    ("_namespace_membership", "server.identity.app.namespace_membership"),
    ("_namespace_snapshot", "server.identity.app.namespace_snapshot"),
    ("_namespace_catalog", "server.identity.app.namespace_catalog"),
    ("namespace_validate", "server.identity.app.namespace_validate"),
    ("_namespace_inventory", "server.identity.app.namespace_inventory"),
    ("namespace_ontology", "server.identity.app.namespace_ontology"),
    ("memory_records", "server.memory.app.records"),
    ("_memory_curation", "server.memory.app.curation"),
    ("_memory_query", "server.memory.app.query"),
    ("_memory_ops", "server.memory.app.ops"),
    ("_memory_consolidate", "server.memory.app.consolidate"),
    ("_memory_evidence_reads", "server.memory.app.evidence_reads"),
    ("_memory_recall", "server.memory.app.recall"),
    ("memory", "server.memory.app.handlers"),
    ("_memory_restore", "server.memory.app.restore"),
    ("conversations", "server.knowledge.app.conversations"),
    ("cost", "server.insights.app.cost"),
    ("run", "server.tasks.app.run"),
    ("decision", "server.memory.app.decision"),
    ("working_memory", "server.memory.app.working_memory"),
    ("admin_users", "server.identity.app.admin_users"),
    ("erasure", "server.erasure.app.handlers"),
    ("knowledge", "server.knowledge.app.writes"),
    ("_knowledge_reads", "server.knowledge.app.document_reads"),
    ("_knowledge_dedup", "server.knowledge.app.dedup"),
    ("grants", "server.identity.app.grants"),
    ("policy", "server.identity.app.history_policy_ops"),
    ("learnings", "server.memory.app.learnings"),
    ("blacklist", "server.memory.app.blacklist"),
    ("conflicts", "server.memory.app.conflicts_ops"),
    ("context_offload", "server.memory.app.context_offload"),
    ("audit", "server.insights.app.audit"),
    ("agents", "server.insights.app.agents"),
    ("memory_for_file", "server.memory.app.for_file"),
    ("memory_proposals", "server.memory.app.proposals_ops"),
)


def _input_models() -> dict[str, type]:
    from pydantic import BaseModel

    # Every wire model module is loaded by the registry; collect the *Input
    # models from each of them, wherever its domain package keeps it.
    importlib.import_module("server.operations.registry")
    out: dict[str, type] = {}
    for mod_name, mod in sorted(sys.modules.items()):
        if mod is None or not (mod_name.startswith("server.operations.") or ".api." in mod_name):
            continue
        for name, cls in inspect.getmembers(mod, inspect.isclass):
            if issubclass(cls, BaseModel) and name.endswith("Input"):
                out.setdefault(name, cls)
    return out


def _dummy_for(annotation: Any, name: str) -> Any:
    origin = typing.get_origin(annotation)
    if origin is typing.Literal:
        return typing.get_args(annotation)[0]
    if origin in (types.UnionType, typing.Union):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if args and typing.get_origin(args[0]) is typing.Literal:
            return typing.get_args(args[0])[0]
        if name in _BY_NAME:
            return _BY_NAME[name]
        return _dummy_for(args[0], name) if args else "x"
    if name in _BY_NAME:
        return _BY_NAME[name]
    if origin in (list, tuple, set):
        (inner,) = typing.get_args(annotation)[:1] or (str,)
        return [_dummy_for(inner, name)]
    if origin is dict:
        return {}
    if annotation is bool:
        return True
    if annotation is int:
        return 5
    if annotation is float:
        return 0.5
    if annotation is _dt.datetime:
        return _DT
    if inspect.isclass(annotation) and hasattr(annotation, "model_fields"):
        return make_input(annotation, filled=False)
    if annotation is str:
        if name.endswith(("_at", "_date", "since", "until", "as_of", "before", "after")):
            return "2026-01-01T00:00:00Z"
        if name.endswith("_id") or name.endswith("_ids"):
            return _UUID
        return "x"
    return None


def make_input(cls: type, *, filled: bool) -> Any:
    """Build an Input model: required fields by name / type, optional fields
    left at their defaults or (filled) set to typed dummies; a field the
    model refuses is dropped and the build retried."""
    from pydantic import ValidationError

    hints = typing.get_type_hints(cls)
    values: dict[str, Any] = {}
    for fname, info in cls.model_fields.items():
        if info.is_required() or filled:
            v = _dummy_for(hints.get(fname, str), fname)
            if v is not None:
                values[fname] = v
    retyped: set[str] = set()
    for _ in range(24):
        try:
            return cls(**values)
        except ValidationError as exc:
            bad = {str(e["loc"][0]) for e in exc.errors() if e.get("loc")}
            if not bad:
                raise
            progressed = False
            for b in bad:
                if b in values and b not in retyped:
                    # The by-name value did not fit this model: try the typed dummy once.
                    retyped.add(b)
                    v = _dummy_for(hints.get(b, str), f"__typed__{b}")
                    if v is not None:
                        values[b] = v
                        progressed = True
                        continue
                if b in values:
                    values.pop(b, None)
                    progressed = True
            if not progressed:
                raise
    return cls(**values)


def _handler_cases_for(module: str) -> list[tuple[Any, type]]:
    mod = importlib.import_module(module)
    models = _input_models()
    out = []
    for name, fn in inspect.getmembers(mod, inspect.iscoroutinefunction):
        if (
            not name.startswith("handle_")
            or name in _SKIP_HANDLERS
            or fn.__module__ != mod.__name__
        ):
            continue
        params = list(inspect.signature(fn).parameters.values())
        if len(params) != 1:
            continue
        ann = params[0].annotation
        cls = models.get(
            _UNTYPED.get(name) or (ann if isinstance(ann, str) else getattr(ann, "__name__", ""))
        )
        if cls is None:
            continue
        out.append((fn, cls))
    return out


def _handler_drive(module: str):
    # Import now, before any drive: a module first imported under the patched
    # client would bind the stub for good. The operations package first, the
    # way the app loads (identity/app/namespace.py and identity/api/namespace.py
    # import each other).
    importlib.import_module("server.operations.registry")
    importlib.import_module(module)

    async def _drive(ch: RecordingCH, c: dict[str, Any]) -> None:
        failures: list[str] = []
        defect: BaseException | None = None
        pairs = _handler_cases_for(module)
        for fn, cls in pairs:
            try:
                inp = make_input(cls, filled=c["filled"])
            except Exception as exc:  # noqa: BLE001 — an input the dummies cannot satisfy
                failures.append(f"{fn.__name__}: input {type(exc).__name__}: {str(exc)[:80]}")
                continue
            before = len(ch.calls)
            try:
                await fn(inp)
            except Exception as exc:  # noqa: BLE001 — a handler that stopped after its first statement still rendered it
                if is_drive_defect(exc):
                    # The siblings still render; the case reports the defect after them
                    defect = defect or exc
                elif len(ch.calls) == before:
                    failures.append(f"{fn.__name__}: {type(exc).__name__}: {str(exc)[:80]}")
        if not pairs:
            raise RuntimeError("no handler with a typed Input model")
        if defect is not None:
            raise defect
        if failures and not ch.calls:
            raise RuntimeError("; ".join(failures))

    return _drive


_HANDLER_ROWS = rows_by_substring(
    {
        "system.columns": [{"name": "consumer", "n": 1}],
        "loom.namespace_registry": [
            {
                "org": ORG,
                "namespace": NS,
                "owner_email": "owner@example.test",
                "visibility": "private",
                "join_policy": "request",
                "status": "active",
                "roles": "{}",
                "categories": [],
                "description": "",
            }
        ],
        "FROM loom.categories FINAL": [{"category_id": "c1", "name": "work", "description": ""}],
        # conversation.threads: the page of threads, then the keyed read of their counts
        # and last turns, so the titles lookup after them renders too.
        "AS msg_count": [{"thread_id": "t1", "msg_count": 1, "first_seen": _DT, "last_seen": _DT}],
        "{tids:Array(String)}": [{"thread_id": "t1", "n": 1, "last_turn": ("a", "user", "x")}],
        "count()": [{"n": 1}],
    }
)


# A live row for the memory ops that resolve their target first (memory.outcome
# looks it up in scope before it reads the evidence totals).
_MEMORY_ROW = {
    "memory_id": _UUID,
    "org": ORG,
    "namespace": NS,
    "agent": AGENT,
    "user_id": "-",
    "session_id": "-",
    "memory_key": "k1",
    "memory_type": "fact",
    "value": '"v"',
    "value_text": '"v"',
    "confidence": 0.9,
    "version": 1,
    "pinned": False,
    "created_at": _DT,
}
_MEMORY_LOOKUPS = rows_by_substring(
    {
        "SELECT created_at, memory_key": [_MEMORY_ROW],
        "value AS value_text": [_MEMORY_ROW],
    }
)


def _memory_ops_rows(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return _MEMORY_LOOKUPS(sql, params) or _HANDLER_ROWS(sql, params)


# --------------------------------------------------------------------------- repos


async def _conversations(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
    from server.knowledge.store import conversations as R

    f = c["filters"]
    await R.fetch_thread(ORG, NS, AGENT, "t1", ch=ch)
    await R.list_thread(
        ORG, NS, AGENT, "t1", run_id="r1" if f else None, after=(_DT, "m1") if f else None, ch=ch
    )
    await R.search_substring(
        ORG,
        [NS],
        AGENT,
        "deploy",
        scope=WHOLE_NAMESPACE,
        thread_id="t1" if f else None,
        role="user" if f else None,
        run_id="r1" if f else None,
        ch=ch,
    )
    await R.count_thread_msgs(ORG, NS, AGENT, "t1", ch=ch)
    # Saved-message search under a token pinned to one user.
    pinned = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    await R.search_substring(
        ORG, NS, AGENT, "deploy", scope=pinned, user_id="u1", saved_only=f, ch=ch
    )
    await R.message_exists(ORG, NS, AGENT, "t1", "m1", live_only=f, ch=ch)
    await R.message_state(ORG, NS, AGENT, "t1", "m1", user_id="u1", ch=ch)
    await R.embedding_state(ORG, NS, AGENT, "t1", "m1", user_id="u1", embedding_model="m", ch=ch)
    await R.saved_message_owner(ORG, NS, AGENT, "t1", "m1", ch=ch)
    await R.lexical_saved_messages(
        ORG,
        NS,
        AGENT if f else "*",
        ["deploy", "window"],
        scope=pinned,
        user_id="u1",
        conversation_id="t1" if f else None,
        role="user" if f else None,
        ch=ch,
    )
    await R.saved_message_ids(
        ORG, NS, AGENT if f else "*", "t1", ["m1", "m2"], scope=pinned, user_id="u1", ch=ch
    )
    await R.list_saved_messages(
        ORG,
        NS,
        AGENT,
        scope=pinned,
        user_id="u1",
        listed=1264,
        conversation_id="t1" if f else None,
        before=(_DT, "t1", "m1") if f else None,
        ch=ch,
    )
    for table in R.SAVED_MESSAGE_TABLES:
        await R.unsave_message(
            table, ORG, NS, AGENT, "t1", "m1", user_id="u1", remove=f, when=_DT, ch=ch
        )
    await R.namespace_logging_enabled(ORG, NS, ch=ch)
    await R.tombstone_thread(ORG, NS, AGENT, "t1", when=_DT, ch=ch)
    await R.tombstone_messages(ORG, NS, AGENT, "t1", ["m1"], when=_DT, ch=ch)
    await R.tombstone_embedding_if_message_gone(ORG, NS, AGENT, "t1", "m1", ch=ch)


# A tombstoned message, so the writer's re-check tombstones its embedding, and one row
# key for the delete's embeddings pass to reconcile.
_CONVERSATION_ROWS = rows_by_substring(
    {
        "max(tombstoned_at) AS gone": [{"n": 1, "gone": _DT}],
        "SELECT DISTINCT embedding_model, msg_id": [{"embedding_model": "m", "msg_id": "m1"}],
    }
)


# A transcript turn already stored, unsaved and unowned, whose embedding row is
# unflagged (`embedded`) or never landed: saving it flags the message row and
# either flags the embedding row or writes it.
def _logged_turn_rows(embedded: bool):
    return rows_by_substring(
        {
            "groupUniqArray(user_id)": [
                {
                    "n": 1,
                    "role": "assistant",
                    "content": "the logged answer",
                    "run_id": "r1",
                    "last_ts": _DT,
                    "owners": ["-"],
                    "tombstoned": False,
                    "unsaved": 1,
                }
            ],
            "countIf(live AND embedding_model": [
                {"current": 1 if embedded else 0, "unsaved": 1 if embedded else 0, "foreign": 0}
            ],
        }
    )


async def _save_logged_turn(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.knowledge.api.models_conversations import ConversationSaveMessageInput
    from server.knowledge.app.conversations import handle_conversation_save_message

    await handle_conversation_save_message(
        ConversationSaveMessageInput(
            org=ORG,
            namespace=NS,
            agent=AGENT,
            conversation_id="t1",
            message_id="m1",
            role="assistant",
            content="the saved answer",
            user_id="u1",
        )
    )


async def _knowledge(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.knowledge.store import chunks as K
    from server.knowledge.store import documents as D

    f = c["filters"]
    await K.count_active_by_uri(ORG, NS, ["doc://a"], ch=ch)
    await K.count_active_for_doc(ORG, NS, "doc://a", ch=ch)
    await K.count_matching_chunk_hashes(
        ORG, NS, ["h1"], excluding_source_uri="doc://b" if f else "", ch=ch
    )
    await K.find_chunk_duplicate_clusters(ORG, NS, ch=ch)
    await K.find_doc_by_content_sha256(
        ORG, NS, "s" * 64, excluding_source_uri="doc://b" if f else "", ch=ch
    )
    await K.find_doc_duplicate_clusters(ORG, NS, ch=ch)
    await K.list_for_doc(ORG, NS, "doc://a", embedding_model="m", ch=ch)
    await K.mark_doc_superseded(
        ORG, NS, "doc://a", embedding_model="m" if f else None, when=_DT, ch=ch
    )
    await K.mark_docs_superseded(ORG, NS, ["doc://a"], when=_DT, ch=ch)
    await D.get_many_by_uris(ORG, NS, ["doc://a"], include_superseded=f, ch=ch)
    await D.get_one(ORG, NS, "doc://a", include_superseded=f, ch=ch)
    await D.list_with_chunk_counts(
        ORG,
        NS,
        include_superseded=f,
        tag_filter=TagFilter(("t",), "folded_any") if f else None,
        uri_prefixes=["doc://"] if f else None,
        cursor=(_DT, "doc://a") if f else None,
        ch=ch,
    )


async def _ledgers(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.core import namespace_write_marks as W
    from server.insights.store import audit as A
    from server.memory.store import blacklist as B
    from server.memory.store import conflicts as C
    from server.memory.store import lifecycle as L
    from server.memory.store import redaction as R

    f = c["filters"]
    await A.earliest_audit_ts(ch=ch)
    await A.latest_checkpoints(10, ch=ch)
    await A.list_by_request_id("r1", ch=ch)
    await A.verify_window(3600, ch=ch)
    await A.verify_window(3600, after=(_DT, "r1", "0" * 64), limit=10, ch=ch)
    await A.reported_tamper_keys(90000, ch=ch)
    await A.window_stats(_DT, _DT, ch=ch)
    await C.allowlist_exists(ORG, NS, "k1", "k2", ch=ch)
    await C.allowlist_list(ORG, NS, ch=ch)
    await C.allowlist_remove(ORG, NS, "k1", "k2", "note", _DT, ch=ch)
    await C.list_conflicted(ORG, NS, before_version=5 if f else None, ch=ch, scope=WHOLE_NAMESPACE)
    await C.lookup_keys_for_ids(ORG, [_UUID], ch=ch, namespace=NS)
    await C.lookup_target(ORG, NS, _UUID, ch=ch)
    await C.mark_active_by_ids(ORG, [_UUID], ch=ch, namespace=NS)
    await C.tombstone_ids(ORG, [_UUID], "note", _DT, ch=ch, namespace=NS)
    await B.find_active_pattern(_UUID, org=ORG if f else None, ch=ch)
    await B.list_for_scope(ORG, NS, scope="namespace" if f else None, ch=ch)
    await B.tombstone_pattern(_UUID, _DT, ch=ch)
    await W.blacklist_updated_at(ch=ch)
    await W.last_writes(ORG, [NS, "n2"], ch=ch)
    await L.delete_procedure_runs_by_key(ORG, NS, "k1", ch=ch)
    await R.delete_llm_response_cache_by_key(ORG, "k1", ch=ch)
    await R.delete_ask_precedents_by_key(ORG, "k1", ch=ch)
    await R.profile_keys_built_from(ORG, NS, "u1", _UUID, ch=ch)
    key = (ORG, NS, AGENT, "u1", "s1", "k1")
    await R.redact_versions(key, _UUID, 1, when=_DT, reason_blob="r|admin", head=None, ch=ch)
    # Postgres's chunk purge (the heads' kept set) and the ClickHouse authority's (none).
    for live, through, kept in ((True, 1, ["c" * 64]), (False, None, None)):
        scrubs = await R.key_derivative_scrubs(
            ORG,
            NS,
            AGENT,
            "u1",
            "s1",
            "k1",
            head_live=live,
            through=through,
            redacted_sha="a" * 64,
            live_sha="b" * 64,
            set_chunks_kept=kept,
            ch=ch,
        )
        await R.run_scrubs(scrubs, apply=False, ch=ch)
        for s in scrubs:  # the recorder counts no rows, so the purges are sent here
            await ch.execute(s.update_sql(), params=s.params)
    await R.set_chunk_count(ORG, NS, AGENT, "u1", "k1", ch=ch)
    from server.memory.store import embeddings as E

    await E.live_vectors(ORG, NS, [(AGENT, "u1", "s1", "k1")], model="m", ch=ch)


_FAILED_BATCH = {
    "queue_id": _UUID,
    "agent": AGENT,
    "user_id": "u1",
    "session_id": "s1",
    "status": "failed",
    "version": 1,
    "past_last_day": 0,
}


async def _services(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.app import conflicts as CF
    from server.memory.app import digest_chain as DC
    from server.memory.app import entity_communities as EC
    from server.memory.app import entity_resolution as ER
    from server.memory.app import extraction_queue as EQ
    from server.memory.app import namespace_retrieval_defaults as RD
    from server.memory.app import procedure_ledger as PL
    from server.memory.app import projection_failures as PF
    from server.memory.app.curation import fetch_timeline
    from server.memory.store import conflict_candidates as conflict_candidates

    f = c["filters"]
    await PF.list_projection_failures(
        org=ORG,
        namespace=NS if f else None,
        projection="entity_graph" if f else "",
        status="active" if f else "",
        ch=ch,
    )
    await PF.scan_active_projection_failures(ch=ch)
    await PF.scan_active_projection_failures(after=(_DT, _UUID), ch=ch)
    await PF.mark_projection_failures_resolved(org=ORG, failure_ids=[_UUID], ch=ch)
    await PF.list_projection_failures(
        org=ORG, scope_where=ADMIN_SCOPE, scope_params=ADMIN_SCOPE_PARAMS, ch=ch
    )
    await PF.mark_projection_failures_resolved(
        org=ORG,
        failure_ids=[_UUID],
        scope_where=ADMIN_SCOPE,
        scope_params=ADMIN_SCOPE_PARAMS,
        ch=ch,
    )
    await EQ.claim_batch(
        limit=10,
        lease_seconds=60,
        ch=ch,
        org_filter=ORG if f else None,
        namespace_filter=NS if f else None,
    )
    await EQ.list_status(
        org=ORG,
        namespace=NS,
        scope=WHOLE_NAMESPACE,
        agent=AGENT if f else None,
        user_id="alice" if f else None,
        session_id="s1" if f else None,
        queue_id=_UUID if f else None,
        status="pending" if f else None,
        ch=ch,
    )
    # The caller's own batches: one user's under a narrowed token, else the namespace's.
    own = ReadFilters(scope="own_user", user_id="u1", session_id=None) if f else WHOLE_NAMESPACE
    by = "process:sa:a"
    await EQ.list_own_status(
        org=ORG, namespace=NS, queue_ids=[_UUID, "q2"], enqueued_by=by, scope=own, ch=ch
    )
    await EQ.requeue_own_failed(
        org=ORG, namespace=NS, queue_ids=[_UUID], enqueued_by=by, scope=own, max_attempts=3, ch=ch
    )
    await EQ.pending_count(
        org=ORG, namespace=NS, agent=AGENT if f else None, user_id="alice" if f else None, ch=ch
    )
    # a request still waiting on its last day before the queue's TTL
    await EQ.drop_expiring(ch=ch, org_filter=ORG if f else None, namespace_filter=NS if f else None)
    await EC.community_assignment(ch, org=ORG, namespaces=[NS, "n2"], entities=["nate", "sam"])
    await EC.fetch_communities(ch, org=ORG, namespace=NS)
    await ER.lookup_canonical_entities(ch, org=ORG, namespaces=[NS], entity_texts=["Nate"])
    await ER.resolve_entities_for_write(ch, org=ORG, namespace=NS, entity_texts=["Nate"])
    await DC.neighbours(
        ch, org=ORG, namespace=NS, agent=AGENT, user_id="alice", session_id="s1", at=_DT
    )
    await PL.cost_by_revision(org=ORG, namespace=NS, memory_key="k1")
    await PL.count_since(org=ORG, namespace=NS, memory_key="k1", since="2026-01-01")
    await PL.failure_notes(org=ORG, namespace=NS, memory_key="k1")
    await PL.stats_by_revision(org=ORG, namespace=NS, memory_key="k1")
    await PL.unfolded(org=ORG, namespace=NS, memory_key="k1", since="2026-01-01")
    await CF.candidate_rows_many(
        org=ORG, namespace=NS, items=[("k1", "preference", SMALL_VEC, 0.5)], ch=ch
    )
    # The per-write peer read — stage A, stage B, the join fallback.
    _peer = {
        "org": ORG,
        "ns": NS,
        "mt": "preference",
        "self_key": "k1",
        "emb": SMALL_VEC,
        "max_dist": 0.5,
        "dim": len(SMALL_VEC),
    }
    await ch.query_all(conflict_candidates._peer_candidates_sql(), params={**_peer, "k": 32})
    await ch.query_all(
        conflict_candidates._PEER_ROWS_SQL, params={**_peer, "ids": [(AGENT, "-", "-", "k2")]}
    )
    await CF._peer_rows_joined(ch, dict(_peer))
    # A writer bounded to one user: its peers in its scope, others' pinned rows.
    one_user = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    await CF._peer_rows(ch, dict(_peer), one_user)
    scoped = dict(_peer)
    await CF._peer_rows_joined(ch, scoped, scope_and(one_user, scoped, "m"))
    await CF._foreign_pinned_rows(ch, dict(_peer), one_user, [])
    with read_filters_bound(current_auth(), one_user):  # the narrowed batch and its pinned arm
        await CF.candidate_rows_many(
            org=ORG, namespace=NS, items=[("k1", "preference", SMALL_VEC, 0.5)], ch=ch
        )
    await CF.check_against_shared(
        org=ORG, namespace=NS, target_key="k1", memory_type="preference", embedding=SMALL_VEC
    )
    await CF.resolve_conflict_floor(ORG, NS, "preference")
    RD.clear_cache()
    await RD.resolve(ORG, NS, ch=ch)
    await CF.prefetch_write_config(ORG, NS, "preference", "k1")
    await fetch_timeline(
        org=ORG,
        namespace=NS,
        entity="nate" if f else "",
        tags=["ops"] if f else None,
        memory_key_prefix="user_" if f else "",
        since=_DT if f else None,
        until=_DT if f else None,
        order="asc" if f else "desc",
        ch=ch,
        scope=WHOLE_NAMESPACE,
    )


async def _list_score(ch: RecordingCH, c: dict[str, Any]) -> None:
    """memory.list's `sort_by=score` path (the wildcard axes
    leave WHERE and the writer columns are selected); the handler drive's
    filled input takes the first literal, `recency`."""
    from server.memory.api.models_crud import MemoryListInput
    from server.memory.app.query import _list_by_score

    w = c["wildcard"]
    inp = MemoryListInput(
        org=ORG,
        namespace=NS,
        agent="*" if w else AGENT,
        user_id="*" if w else "-",
        sort_by="score",
        prefix="lesson_",
        memory_type="semantic" if not w else None,
        quotas={"semantic": 3} if w else None,
        tags=["ops", "infra"] if w else [],
        # Every tag, both sides lowercased.
        tags_every=w,
    )
    # The served-set flags, as the handler builds them
    await _list_by_score(
        inp,
        TagFilter(("ops", "infra"), "folded_all") if w else None,
        {"include_duplicates": w, "include_superseded": w, "include_proposed": w},
    )


async def _conversation_threads(ch: RecordingCH, c: dict[str, Any]) -> None:
    """conversation.threads under each `fields` choice that changes the keyed read:
    every field, the count alone, the last turn alone; across agents and for one."""
    from server.knowledge.api.models_conversations import ConversationThreadsInput
    from server.knowledge.app.conversations import handle_conversation_threads

    await handle_conversation_threads(
        ConversationThreadsInput(org=ORG, namespace=NS, agent=c["agent"], fields=c["fields"])
    )


async def _session_runs(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The session-run ledger reads: the run under an id, the idle sweep, a
    run's spend over its window, and the time a re-sent turn is recorded with."""
    from server.insights.store import cost
    from server.tasks.store import runs

    await runs.active_runs_by_id(ch, ORG, "mcp-" + "0" * 40)
    await runs.idle_session_runs(ch, idle_seconds=1800, limit=500)
    await runs.run_cost(ch, ORG, NS, "s1", _DT, _DT)
    await cost.recorded_turn_times(ORG, NS, "s1", ["m1"], ch=ch)


async def _rebuild_session_chunks(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The provider rebuild's read of a session's chunks around one stored without a
    vector."""
    from server.insights.store import provider_staging

    await provider_staging.session_chunks(ORG, NS, "s1", 0, 64, ch=ch)


async def _profiles(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The profile schema reads and the structured profile's source reads: a
    user's facts (with and without the fold's key rule), their digest, the
    profile row, its head's pin and lock, and the users whose profile is behind, each unbounded and under
    an `own_user` scope."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
    from server.memory.store import profile_schemas, profile_sources

    await profile_schemas.get(ORG, NS, ch=ch)
    await profile_schemas.list_active(
        exclude_orgs=["o2"],
        after=("o1", "a"),
        limit=10,
        only=("o1", "b"),
        waiting=[("o1", "c", "0" * 64)],
        ch=ch,
    )
    own = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    for scope in (WHOLE_NAMESPACE, own):
        for keys in (False, True):
            await profile_sources.user_facts(
                org=ORG,
                namespace=NS,
                user_id="u1",
                types=["preference"],
                limit=5,
                scope=scope,
                user_keys_only=keys,
                ch=ch,
            )
        await profile_sources.sources_digest(
            org=ORG, namespace=NS, user_id="u1", scope=scope, ch=ch
        )
        await profile_sources.profile_row(
            org=ORG, namespace=NS, user_id="u1", label="structured_profile", scope=scope, ch=ch
        )
        await profile_sources.profile_head_hold(
            org=ORG, namespace=NS, user_id="u1", label="structured_profile", scope=scope, ch=ch
        )
        await profile_sources.users_behind(
            org=ORG,
            namespace=NS,
            label="structured_profile",
            schema_sha256="0" * 64,
            cooldown_seconds=600,
            limit=10,
            skip=("u2",),
            refused={"u3": ("0" * 16, 1)},
            timeout_seconds=60,
            max_memory_bytes=4 * 1024**3,
            scope=scope,
            ch=ch,
        )


async def _grants_repo(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The grants `collection.create` reads to find another holder of the name, and the
    member count `namespace.inventory` takes over a collection's lineage; `grants.list`
    reads through its handler."""
    from server.identity.store import grants as grants_repo

    await grants_repo.list_active(ORG, NS, ch=ch)
    await grants_repo.member_count(ORG, ["col-a", "col-b"], ch=ch)


async def _mentor_evidence(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The evidence ladder `mentor.brief` reads at each brief."""
    from server.insights.app import mentor_rules
    from server.insights.store import mentor as mentor_store

    who = (ORG, NS, "agent-a", 604800)
    await mentor_store.call_evidence(
        *who,
        searches=mentor_rules.SEARCHES,
        gets=mentor_rules.GETS,
        ch=ch,
    )
    await mentor_store.indexed_writes(*who, model="fake-ngram-1024", ch=ch)
    await mentor_store.reported_keys(*who, ch=ch)
    await mentor_store.graded_runs(*who, ch=ch)


async def _inspect_reads(ch: RecordingCH, c: dict[str, Any]) -> None:
    """Every read `memory.inspect` makes of one view, under the whole namespace or a
    narrowed scope."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
    from server.memory.store import inspect as store

    scope = (
        WHOLE_NAMESPACE
        if c["scope"] == "namespace"
        else ReadFilters(scope="own_user", user_id="u1", session_id=None)
    )
    view = (ORG, NS, c["subject"], "x1", c["view"])
    await store.view_counts(*view, scope=scope, ch=ch)
    await store.view_tags(*view, scope=scope, limit=20, ch=ch)
    await store.view_categories(*view, scope=scope, ch=ch)
    await store.view_newest(*view, scope=scope, limit=10, ch=ch)
    await store.version_counts(ORG, NS, [_INSPECT_ID], ch=ch)
    await store.subject_calls(
        ORG,
        NS,
        c["subject"],
        "x1",
        window_seconds=604800,
        skip_consumers=["loom-ui", "loom-assistant"],
        ch=ch,
    )


_INSPECT_ID = "00000000-0000-0000-0000-000000000001"


async def _knowledge_inspect_reads(ch: RecordingCH, c: dict[str, Any]) -> None:
    """Every read `knowledge.inspect` makes: the documents, chunks, retired ingestions,
    extraction coverage and knowledge.ingest jobs, and the decision rows under the whole
    namespace or a narrowed scope."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
    from server.knowledge.store import inspect as store
    from server.memory.store.document_decisions import decisions_by_document

    scope = (
        WHOLE_NAMESPACE
        if c["scope"] == "namespace"
        else ReadFilters(scope="own_user", user_id="u1", session_id=None)
    )
    await store.document_groups(ORG, NS, ch=ch)
    await store.retired_versions(ORG, NS, ch=ch)
    await store.chunk_models(ORG, NS, ch=ch)
    await store.chunk_health(ORG, NS, "fake-ngram-1024", ch=ch)
    await store.newest_documents(ORG, NS, limit=20, ch=ch)
    await store.document_chunks(ORG, NS, ["docs/a.md"], "fake-ngram-1024", ch=ch)
    await store.document_retired_versions(ORG, NS, ["docs/a.md"], ch=ch)
    await store.longer_document_uris(ORG, NS, ["docs/a.md"], ch=ch)
    await store.extraction_health(ORG, NS, ch=ch)
    await store.ingestion_health(ORG, NS, lease_s=120.0, ch=ch)
    await store.source_jobs(ORG, NS, ["docs/a.md"], lease_s=120.0, ch=ch)
    await store.sources_without_document(ORG, NS, limit=20, lease_s=120.0, ch=ch)
    await decisions_by_document(ORG, NS, ["docs/a.md"], scope=scope, ch=ch)
    # The per-format reads namespace.tune's extractor drafts make.
    await store.format_unavailable_sources(ORG, NS, lease_s=120.0, ch=ch)
    await store.extraction_by_format(ORG, NS, ch=ch)


async def _tune_document_reads(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The document reads behind `namespace.tune`'s chunking draft."""
    from server.knowledge.app.tune import MAX_DOCUMENT_CHARS, MAX_SAMPLE_CHARS
    from server.knowledge.store import tune as store

    await store.sample_documents(
        ORG, NS, limit=50, max_chars=MAX_DOCUMENT_CHARS, total_chars=MAX_SAMPLE_CHARS, ch=ch
    )
    await store.document_writers(ORG, NS, limit=20, ch=ch)


async def _tune_reads(ch: RecordingCH, c: dict[str, Any]) -> None:
    """Every read `namespace.tune` makes of the namespace's rows, under the whole
    namespace or a narrowed scope."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
    from server.identity.store import namespace_tune as store

    scope = (
        WHOLE_NAMESPACE
        if c["scope"] == "namespace"
        else ReadFilters(scope="own_user", user_id="u1", session_id=None)
    )
    await store.writing_agents(ORG, NS, scope=scope, limit=20, ch=ch)
    await store.key_prefixes(
        ORG, NS, scope=scope, min_rows=5, name_pattern="^[a-z]+$", limit=500, ch=ch
    )
    await store.plain_tags(
        ORG, NS, scope=scope, min_rows=5, name_pattern="^[a-z]+$", limit=500, ch=ch
    )
    await store.tag_spellings(ORG, NS, ["billing"], scope=scope, ch=ch)
    await store.extraction_writers(ORG, NS, scope=scope, limit=20, ch=ch)
    await store.written_by_extraction(ORG, NS, [("a", "-", "-", "k1")], scope=scope, ch=ch)
    await store.tagged_rows(ORG, NS, "billing", scope=scope, limit=5, ch=ch)
    await store.tagged_in_other_categories(ORG, NS, "billing", scope=scope, ch=ch)


# The handlers whose org-wide reads `resolve_org_scope` scopes: an org admin's
# `*` claim renders the whole-org branch, the corpus's listed claim the member one.
_ORG_ROLLUPS = frozenset({"cost", "learnings", "audit", "agents"})


def cases() -> list[Case]:
    out = [
        Case(
            f"handlers.{name.lstrip('_')}",
            module,
            _handler_drive(module),
            {
                "filled": (False,) if name in _FILL_INERT else (False, True),
                **({"claim": ("listed", "*")} if name in _ORG_ROLLUPS else {}),
            },
            rows=_memory_ops_rows if name == "_memory_ops" else _HANDLER_ROWS,
        )
        for name, module in HANDLER_MODULES
    ]
    out.append(
        Case(
            "handlers.conversation_threads",
            "server.knowledge.app.conversations",
            _conversation_threads,
            {"fields": (None, ["msg_count"], ["last_role"]), "agent": ("*", AGENT)},
            rows=_HANDLER_ROWS,
        )
    )
    out.append(Case("session_runs.ledger", "server.tasks.store.runs", _session_runs, {}))
    out.append(
        Case(
            "provider_rebuild.session_chunks",
            "server.insights.store.provider_staging",
            _rebuild_session_chunks,
            {},
        )
    )
    out.append(Case("profiles", "server.memory.store.profile_sources", _profiles, {}))
    out.append(Case("grants_repo", "server.identity.store.grants", _grants_repo, {}))
    out.append(
        Case(
            "namespace_tune",
            "server.identity.store.namespace_tune",
            _tune_reads,
            {"scope": ("namespace", "own_user")},
            # Each read answers one row with every column the drafts read back.
            rows=lambda sql, params: [
                {
                    "agent": "a",
                    "user_id": "-",
                    "session_id": "-",
                    "memory_key": "k1",
                    "prefix": "k1",
                    "rows": 1,
                    "total": 1,
                    "tag": "billing",
                }
            ],
        )
    )
    out.append(
        Case(
            "memory_inspect",
            "server.memory.store.inspect",
            _inspect_reads,
            {
                "subject": ("user", "agent", "session"),
                "view": ("own", "others"),
                "scope": ("namespace", "own_user"),
            },
            # One listed row, so the preview read runs too.
            rows=lambda sql, params: [
                {
                    "id": _INSPECT_ID,
                    "version": 1,
                    "versions": 1,
                    "sensitivity": "",
                    "value_preview": '"x"',
                    "cut": 0,
                }
            ],
        )
    )
    out.append(
        Case(
            "knowledge_inspect",
            "server.knowledge.store.inspect",
            _knowledge_inspect_reads,
            {"scope": ("namespace", "own_user")},
            # Each read answers one row with every column the reads return.
            rows=lambda sql, params: [{"n": 0, "source_uri": "docs/a.md"}],
        )
    )
    out.append(
        Case(
            "namespace_tune_documents",
            "server.knowledge.store.tune",
            _tune_document_reads,
            {},
            rows=lambda sql, params: [{"agent": "a", "rows": 1, "total": 1}],
        )
    )
    out.append(
        Case(
            "mentor_evidence",
            "server.insights.store.mentor",
            _mentor_evidence,
            {},
            # Each read is an aggregate: one row, every column it reads back.
            rows=lambda sql, params: [{"n": 0, "retention_days": 0}],
        )
    )
    out.append(
        Case(
            "memory_list_score",
            "server.memory.app.query",
            _list_score,
            {"wildcard": (False, True)},
        )
    )
    out += [
        Case(
            "conversation_repo",
            "server.knowledge.store.conversations",
            _conversations,
            {"filters": (False, True)},
            rows=_CONVERSATION_ROWS,
        ),
        Case(
            "conversation_save_logged_turn",
            "server.knowledge.app.conversations",
            _save_logged_turn,
            rows=_logged_turn_rows(embedded=True),
        ),
        Case(
            "conversation_save_unembedded_turn",
            "server.knowledge.app.conversations",
            _save_logged_turn,
            rows=_logged_turn_rows(embedded=False),
        ),
        Case(
            "knowledge_repos",
            "server.knowledge.store.chunks",
            _knowledge,
            {"filters": (False, True)},
        ),
        Case("ledger_repos", "server.insights.store.audit", _ledgers, {"filters": (False, True)}),
        Case(
            "plane_services",
            "server.memory.app.projection_failures",
            _services,
            {"filters": (False, True)},
            # A failed batch drives the retry mutation.
            rows=rows_by_substring(
                {"SELECT queue_id, agent, user_id, session_id, status, version": [_FAILED_BATCH]}
            ),
        ),
    ]
    return out
