"""The erasure saga (server/erasure/store/): every step's
count + mutation under both scopes, as a single namespace and a namespace
list, tombstone vs purge (DELETE), lightweight vs part-rewrite; the census
(the Substrate registry), the exports, the request ledger reads."""

from __future__ import annotations

import functools
import importlib
import inspect
from datetime import datetime
from typing import Any

from tests.analyzer.cases._common import ORG, rows_by_substring
from tests.analyzer.corpus import Case, RecordingCH

STEP_MODULES = (
    "server.erasure.store.steps_memory",
    "server.erasure.store.steps_graph",
    "server.erasure.store.steps_ledgers",
)
CENSUS_MODULE = "server.erasure.store.census"
EXPORTS_MODULE = "server.erasure.store.exports"

_ID = "00000000-0000-0000-0000-0000000000a1"

# The mutation runner: the count says "one row" so the ALTER renders; the
# poll finds its mutation done; a saga's pending-mutation lookup finds none.
_ROWS = rows_by_substring(
    {
        "is_done = 0": [],
        "system.mutations": [
            {"mutation_id": "m1", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}
        ],
        "count()": [{"n": 1}],
        # A user-scope step's scope reads find one row each, so its bound mutation renders.
        " AS keys": [{"keys": ["k"]}],
        "SELECT DISTINCT namespace, memory_key FROM (": [{"namespace": "n", "memory_key": "k"}],
        # graph keys and document uris are read as hashes of their key columns
        ") AS h FROM ": [{"h": 1}],
        "AS id FROM (SELECT record_id": [{"id": _ID}],
        "AS id FROM loom.memory ": [{"id": _ID}],
        "SELECT DISTINCT namespace, memory_key FROM loom.shared": [
            {"namespace": "n", "memory_key": "k"}
        ],
        "AS sha FROM loom.document_blobs": [{"namespace": "n", "sha": "a" * 64}],
        "SELECT DISTINCT candidate_id FROM loom.learnings_queue": [{"candidate_id": _ID}],
        "SELECT * FROM loom.learnings_queue FINAL": [
            {"candidate_id": _ID, "status": "pending", "proposed_value": "r", "version": 1}
        ],
        # The conversation export's size pass finds one turn, so its text read renders.
        "AS bytes_before_previous": [
            {
                "namespace": "n",
                "thread_id": "t",
                "ts": datetime(2026, 1, 1),
                "msg_id": "m",
                "row_bytes": 1,
                "bytes_before": 0,
            }
        ],
    }
)


def _steps(module: str, suffix: str) -> list[Any]:
    mod = importlib.import_module(module)
    return [
        f
        for name, f in inspect.getmembers(mod, inspect.isfunction)
        if f.__module__ == mod.__name__
        and name.endswith(suffix)
        and name.split("_")[0] in ("tombstone", "delete", "supersede", "scrub", "retire")
    ]


async def _run_steps(
    ch: RecordingCH, c: dict[str, Any], module: str, suffix: str, value: Any
) -> None:
    from server.erasure.store import mutations as erasure_mutations

    saga = "erasure-corpus" if c["saga"] else ""
    with erasure_mutations.purge_scope(c["purge"]), erasure_mutations.saga_scope(saga):
        for fn in _steps(module, suffix):
            try:
                await fn(ORG, value, ch=ch)
            except Exception:  # noqa: BLE001 — one step's stub mismatch must not hide the rest
                continue


async def _by_namespace(module: str, ch: RecordingCH, c: dict[str, Any]) -> None:
    value = ["n", "n2"] if c["namespaces"] == "list" else "n"
    await _run_steps(ch, c, module, "_by_namespace", value)


async def _by_user(module: str, ch: RecordingCH, c: dict[str, Any]) -> None:
    await _run_steps(ch, c, module, "_by_user", "alice")


async def _census_and_reads(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.erasure.store import census as erasure_census
    from server.erasure.store import health as erasure_health
    from server.erasure.store import ledger as erasure_ledger
    from server.erasure.store import mutations as erasure_mutations
    from server.erasure.store import steps_graph

    scope_value: Any = (
        ["n", "n2"]
        if c["scope"] == "namespace_list"
        else ("n" if c["scope"] == "namespace" else "alice")
    )
    scope_type = "user_id" if c["scope"] == "user_id" else "namespace"
    await erasure_census.residue_census(ORG, scope_type, scope_value, ch=ch)
    from server.erasure.store import steps_pg

    names = scope_value if isinstance(scope_value, list) else [scope_value]
    by_user = scope_type == "user_id"
    async for _ in steps_pg.documents_in_clickhouse(
        ORG, None if by_user else names, "alice" if by_user else None, ch=ch
    ):
        pass
    cut = datetime(2026, 1, 1)
    await steps_graph.count_orphan_row_links(ORG, scope_type, scope_value, verified_at=cut, ch=ch)
    await steps_graph.delete_orphan_row_links(ORG, scope_type, scope_value, verified_at=cut, ch=ch)
    await erasure_ledger.list_failed_requests(org=ORG, ch=ch)
    await erasure_ledger.list_failed_requests(ch=ch)
    await erasure_ledger.list_completed_requests(since=datetime(2026, 1, 1), org=ORG, ch=ch)
    await erasure_ledger.list_completed_requests(since=datetime(2026, 1, 1), ch=ch)
    await erasure_ledger.list_stuck_in_progress(
        stale_threshold_seconds=300, max_attempts=5, batch_size=10, ch=ch
    )
    await erasure_ledger.list_unabandoned_exhausted(
        stale_threshold_seconds=300, max_attempts=5, batch_size=10, ch=ch
    )
    await erasure_health.orphan_graph_namespaces(org=ORG, ch=ch)
    await erasure_health.orphan_graph_namespaces(ch=ch)
    await erasure_health.integrity_counts(ch=ch)
    await erasure_ledger.fetch_request_status("erasure-1", ch=ch)
    await erasure_mutations.find_pending_mutation("loom.memory", "tag", ch=ch)


async def _exports(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.erasure.store import exports as erasure_exports

    scope_type = c["scope_type"]
    value = "alice" if scope_type == "user_id" else "n"
    await erasure_exports.export_memory(
        ORG, scope_type, value, include_tombstoned=c["include_tombstoned"], limit=100, ch=ch
    )
    await erasure_exports.export_conversations(
        ORG,
        scope_type,
        value,
        include_tombstoned=c["include_tombstoned"],
        limit=100,
        max_bytes=1000,
        as_of=datetime(2026, 1, 2),
        ch=ch,
    )
    await erasure_exports.export_conversation_threads(
        ORG, scope_type, value, include_tombstoned=c["include_tombstoned"], limit=100, ch=ch
    )
    await erasure_exports.export_agent_events(ORG, scope_type, value, limit=100, ch=ch)
    await erasure_exports.export_knowledge(ORG, "namespace", "n", limit=100, ch=ch)
    await erasure_exports.export_knowledge(ORG, "user_id", "u", limit=100, ch=ch)
    await erasure_exports.export_audit_namespace(ORG, "n", limit=100, ch=ch)
    await erasure_exports.export_audit_by_subject_substring(ORG, "alice", limit=100, ch=ch)


def cases() -> list[Case]:
    steps: list[Case] = []
    for module in STEP_MODULES:
        leaf = module.rsplit(".", 1)[1]
        steps += [
            Case(
                f"erasure.{leaf}_by_namespace",
                module,
                functools.partial(_by_namespace, module),
                {
                    "namespaces": ("one", "list"),
                    "purge": (False, True),
                    "saga": (False, True),
                },
                rows=_ROWS,
            ),
            Case(
                f"erasure.{leaf}_by_user",
                module,
                functools.partial(_by_user, module),
                {
                    "purge": (False, True),
                    "saga": (False, True),
                },
                rows=_ROWS,
            ),
        ]
    return [
        *steps,
        Case(
            "erasure.census_and_ledger",
            CENSUS_MODULE,
            _census_and_reads,
            {"scope": ("namespace", "namespace_list", "user_id")},
            rows=_ROWS,
        ),
        Case(
            "erasure.exports",
            EXPORTS_MODULE,
            _exports,
            {"scope_type": ("namespace", "user_id"), "include_tombstoned": (False, True)},
            rows=_ROWS,
        ),
    ]
