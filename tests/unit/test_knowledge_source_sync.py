"""Common source worker state, ownership and accounting over recording turns.

Stores and recording inputs are local fixtures. The worker, recording turn,
write ownership checks, retirement and extraction bookkeeping run unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import repo_registry
from server.core import leases
from server.core.ch import sequential_reads_active
from server.core.hashing import sha256_text
from server.core.secrets_at_rest import seal
from server.knowledge.app import source_sync, source_sync_recording, writes
from server.knowledge.app import sources as source_ops
from server.knowledge.domain import access_list
from server.knowledge.domain.sources import (
    MIRRORED_ACCESS,
    SYNC_WORKER_ACTOR,
    KnowledgeSource,
)
from server.knowledge.store import documents
from server.memory.domain.provenance import pipeline_actor_var
from server.observability import audit
from server.settings import settings

ORG, NS = "acme", "code-acme-app"
DOCS = "text/plain"
PDF = "application/pdf"
KEY = "recording-fixture-credential"


def _file(file_id: str, mime: str, modified: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": file_id,
        "name": f"{file_id} title",
        "modifiedTime": modified,
        "uri": f"https://docs.example.com/document/d/{file_id}/edit",
        **kw,
    }


class _World:
    """The registry, sync state and stored document boundaries."""

    def __init__(self) -> None:
        self.sources: list[KnowledgeSource] = []
        self.inactive: set[tuple[str, str]] = set()
        self.states: dict[str, dict[str, str]] = {}
        # Live documents by source_uri: {actor, tags, metadata}.
        self.docs: dict[str, dict[str, Any]] = {}
        self.files: list[dict[str, Any]] = []
        self.recording_lists: list[list[str]] = []
        self.audits: list[dict[str, Any]] = []
        self.upserts: list[Any] = []
        self.transcript_error: Exception | None = None
        # The extraction leg: each extract_document call (uri, actor, attribution), the
        # report it answers (or a callable that makes one, or an exception), the orgs
        # switched off (an exception: the switch unread), the namespaces over their cap
        # share and each cap read's arguments, and the cost rows emitted.
        self.mined: list[tuple[str, str, Any]] = []
        self.mine_answer: Any = None
        self.switched_off: Any = frozenset()
        self.over_cap: set[tuple[str, str]] = set()
        self.cap_reads: list[dict[str, Any]] = []
        self.cost_rows: list[dict[str, Any]] = []
        # The source lease the current turn holds; a test loses it by setting `held` false.
        self.lease = SimpleNamespace(held=True)

    # store
    async def all_active_sources(self) -> list[KnowledgeSource]:
        return [s for s in self.sources if s.active]

    async def pending_delete_sources(self) -> list[KnowledgeSource]:
        return [s for s in self.sources if not s.active and s.pending_delete]

    async def inactive_namespaces(self, pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        return {p for p in pairs if p in self.inactive}

    async def read_states(self, org: str, ns: str, names: list[str]) -> dict[str, Any]:
        return {n: self.states[n] for n in names if n in self.states}

    async def write_state(self, org: str, ns: str, name: str, *, state_value: str, detail: Any):
        self.states[name] = {"state_value": state_value, "detail": json.dumps(detail)}

    async def get_source(self, org: str, ns: str, name: str) -> KnowledgeSource | None:
        return next((s for s in self.sources if s.name == name and s.active), None)

    async def latest_source(self, org: str, ns: str, name: str) -> KnowledgeSource | None:
        return next((s for s in reversed(self.sources) if s.name == name), None)

    async def insert_source(self, src: KnowledgeSource) -> None:
        self.sources = [s for s in self.sources if s.name != src.name] + [src]

    async def source_revisions(
        self,
        org: str,
        ns: str,
        tag: str,
        *,
        searchable_only: bool = True,
        pictures: bool = False,
    ) -> list[dict[str, str]]:
        return [
            {
                "source_uri": uri,
                "file_id": d["metadata"]["ingested_from"],
                "source_revision": d["metadata"]["source_revision"],
                "text_sha256": d["metadata"].get("text_sha256", ""),
                "inline_pictures": "1" if pictures and "]: <data:" in d.get("content", "") else "",
                "stored_content_sha256": sha256_text(d.get("content", ""))
                if pictures and "]: <data:" in d.get("content", "")
                else "",
                "access_json": d.get("access_json", ""),
                "searchable": "1" if d.get("chunks", True) else "",
            }
            for uri, d in self.docs.items()
            if d["actor"] == SYNC_WORKER_ACTOR
            and d.get("namespace", ns) == ns
            and tag in d["tags"]
            and (d.get("chunks", True) or not searchable_only)
        ]

    async def every_copy(self, org: str, ns: str, tag: str) -> list[dict[str, str]]:
        return await self.source_revisions(org, ns, tag, searchable_only=False)

    async def source_documents(self, org: str, ns: str, tag: str) -> list[dict[str, Any]]:
        return await self.source_revisions(org, ns, tag, searchable_only=False)

    async def count_documents(self, org: str, ns: str, tag: str) -> int:
        return len(await self.source_documents(org, ns, tag))


@pytest.fixture
def source_world(monkeypatch: pytest.MonkeyPatch) -> _World:
    w = _World()
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-root-secret")
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 40)
    for name in (
        "all_active_sources",
        "pending_delete_sources",
        "inactive_namespaces",
        "read_states",
        "write_state",
        "get_source",
        "latest_source",
        "insert_source",
        "source_revisions",
        "every_copy",
        "source_documents",
        "count_documents",
    ):
        monkeypatch.setattr(source_sync.store, name, getattr(w, name))
    monkeypatch.setattr(source_ops, "store", source_sync.store)

    async def _registry(**kw: Any) -> list[Any]:
        return []

    monkeypatch.setattr(repo_registry, "refresh_db_repos", _registry)
    monkeypatch.setattr(source_sync, "_repo_tag", lambda src: "repo:abc")

    @contextlib.asynccontextmanager
    async def _holding(name: str, **kw: Any) -> AsyncIterator[bool]:
        yield True

    @contextlib.asynccontextmanager
    async def _cluster(name: str, **kw: Any) -> AsyncIterator[Any]:
        w.lease = SimpleNamespace(held=True)  # each turn's own lease
        yield w.lease

    monkeypatch.setattr(leases, "holding", _holding)
    monkeypatch.setattr(leases, "cluster_lease", _cluster)

    async def _get_one(org: str, ns: str, uri: str, **kw: Any) -> Any:
        d = w.docs.get(uri)
        return (
            None
            if d is None or d.get("namespace", ns) != ns
            else documents.DocumentRow(
                org=org,
                namespace=ns,
                source_uri=uri,
                doc_id="d",
                content=d.get("content", ""),
                version=d.get("version", 1),
                revision=d.get("revision", 1),
                ingested_at="2026-01-01T00:00:00",
                actor=d["actor"],
                tags=d["tags"],
                access_json=d.get("access_json", ""),
            )
        )

    monkeypatch.setattr(documents, "get_one", _get_one)

    async def _upsert(inp: Any, permissions: Any = access_list.UNREAD, **_: Any) -> Any:
        w.upserts.append((inp, pipeline_actor_var.get()))
        w.docs[inp.source_uri] = {
            "actor": pipeline_actor_var.get(),
            "tags": list(inp.tags),
            "metadata": dict(inp.metadata),
            "content": inp.content,
            "namespace": inp.namespace,
            "access_json": access_list.to_json(permissions),
        }

    async def _delete(inp: Any) -> Any:
        gone = w.docs.pop(inp.source_uri, None)
        return writes.KnowledgeDeleteOutput(deleted=gone is not None, chunks_tombstoned=1)

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _upsert)
    monkeypatch.setattr(writes, "handle_knowledge_delete", _delete)

    async def _emit(event: str, **kw: Any) -> str:
        w.audits.append({"event": event, **kw})
        return "h"

    monkeypatch.setattr(audit, "emit", _emit)

    _mock_mining(monkeypatch, w)
    w.sources.append(_source())
    return w


@pytest.fixture
def world(source_world, monkeypatch):
    w = source_world

    async def session(provider, credential):
        assert provider == "zoom" and credential == KEY
        return source_sync_recording.Session("zoom", "fixture-token")

    async def listing(selected, ids, *, stamp):
        assert selected.provider == "zoom" and ids == ["123456789"]
        w.recording_lists.append(ids)
        return [dict(item) for item in w.files], []

    async def transcript(selected, item, *, max_bytes):
        assert selected.provider == "zoom" and max_bytes > 0
        if w.transcript_error is not None:
            raise w.transcript_error
        return "# Decision\n\nKeep the hash join."

    monkeypatch.setattr(source_sync_recording, "open_session", session)
    monkeypatch.setattr(source_sync_recording, "list_recordings", listing)
    monkeypatch.setattr(source_sync_recording, "transcript", transcript)
    return w


def _mock_mining(monkeypatch: pytest.MonkeyPatch, w: _World) -> None:
    from server.llm import customer
    from server.memory.app import decision_extract
    from server.observability import budget, cost_accumulator, events
    from server.workers import cost_event

    async def _extract_document(
        org: str,
        ns: str,
        uri: str,
        *,
        dry_run: bool,
        read_deadline: float,
        may_write: Any,
        writes: Any,
    ) -> Any:
        assert (org, ns, dry_run) == (ORG, NS, False) and read_deadline > 0
        assert may_write()
        w.mined.append((uri, pipeline_actor_var.get(), customer.attribution()))
        cost_accumulator.report_llm("m", 10, 5)
        answer = w.mine_answer
        if isinstance(answer, Exception):
            raise answer
        report = answer(uri) if callable(answer) else None
        return report or decision_extract.ExtractReport(uri, dry_run)

    async def _switched_off() -> frozenset[str]:
        if isinstance(w.switched_off, Exception):
            raise w.switched_off
        return w.switched_off

    async def _over(org: str, ns: str, **kw: Any) -> bool:
        w.cap_reads.append(kw)
        return (org, ns) in w.over_cap

    class _Emitter:
        async def flush_now(self) -> None:
            return None

    def _cost(**kw: Any) -> None:
        acc = cost_accumulator.current()
        spent = acc.drain().llm_input_tokens if acc is not None else 0
        w.cost_rows.append({**kw, "llm_input_tokens": spent})

    monkeypatch.setattr(decision_extract, "no_model_error", lambda: "")
    monkeypatch.setattr(decision_extract, "extract_document", _extract_document)
    monkeypatch.setattr(customer, "switched_off_orgs", _switched_off)
    monkeypatch.setattr(budget, "namespace_over_cap", _over)
    monkeypatch.setattr(events, "get_emitter", lambda: _Emitter())
    monkeypatch.setattr(cost_event, "emit_worker_cost_event_for", _cost)


def _source(name: str = "decks", **kw: Any) -> KnowledgeSource:
    config = kw.pop("config", {"provider": "zoom", "ids": ["123456789"]})
    return KnowledgeSource(
        org=ORG,
        namespace=kw.pop("namespace", NS),
        name=name,
        kind="recording",
        source_id=kw.pop("source_id", f"id-{name}"),
        config=config,
        credential_enc=kw.pop("credential_enc", seal("knowledge_sources.credential", KEY)),
        **kw,
    )


def _detail(world: _World, name: str = "decks") -> dict[str, Any]:
    return json.loads(world.states[name]["detail"])


OLD_EXPORT = "# Design\n\n![][image1]\n\n[image1]: <data:image/png;base64,AAAA>\n"


def test_sources_on_an_inactive_namespace_are_not_synced_and_requests_go_first(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [
        _source("aaa"),
        _source("zzz"),
        _source("gone", namespace="code-gone"),
        _source("plain", namespace="notes"),
    ]
    world.inactive = {(ORG, "code-gone")}
    world.states["zzz"] = {"state_value": "", "detail": json.dumps({"requested_at": "t"})}
    order: list[str] = []

    async def _turn(src: KnowledgeSource, *a: Any) -> Any:
        order.append(src.name)
        return source_sync.TurnResult()

    monkeypatch.setattr(source_sync_recording, "turn", _turn)
    asyncio.run(source_sync.run_sync())
    assert order == ["zzz", "aaa", "plain"]


def test_a_removed_source_s_documents_are_retired_then_the_request_cleared(
    world: _World,
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.sources = [_source(active=False, pending_delete=True, credential_enc="")]
    result = asyncio.run(source_sync.run_sync())
    assert result.retired == 1
    assert world.docs == {}
    assert world.audits[-1]["subject"]["source_name"] == "decks"
    assert not world.sources[-1].pending_delete


def test_a_pass_leaves_a_mirrored_source_of_a_store_without_a_mirror_turn(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.sources = [dataclasses.replace(_source(access=MIRRORED_ACCESS), kind="notion")]
    result = asyncio.run(source_sync.run_sync())
    assert (result.sources, result.synced) == (0, 0) and world.upserts == []
    # Removed with its documents, its request is kept for that build.
    world.sources = [_source(access=MIRRORED_ACCESS, active=False, pending_delete=True)]
    assert asyncio.run(source_sync.run_sync()).retired == 0
    assert world.sources[-1].pending_delete


def test_a_pass_leaves_a_source_whose_access_this_build_does_not_know(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.sources = [_source(access="shared")]  # a later build's value
    result = asyncio.run(source_sync.run_sync())
    assert (result.sources, result.synced, result.failed) == (0, 0, 0)
    assert world.upserts == []
    world.sources = [_source(access="shared", active=False, pending_delete=True)]
    result = asyncio.run(source_sync.run_sync())
    assert (result.retired, result.failed) == (0, 0)
    assert world.sources[-1].pending_delete  # kept for the build that knows it


def test_a_deleted_namespace_s_source_still_has_its_documents_retired(world: _World) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.inactive = {(ORG, NS)}
    world.sources = [_source(active=False, pending_delete=True, credential_enc="")]
    result = asyncio.run(source_sync.run_sync())
    assert result.retired == 1
    assert world.docs == {}
    assert not world.sources[-1].pending_delete


def test_a_source_replaced_during_its_turn_writes_and_records_nothing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    replaced = _source(source_id="another")

    async def _get(org: str, ns: str, name: str) -> KnowledgeSource:
        return replaced

    monkeypatch.setattr(source_sync.store, "get_source", _get)
    result = asyncio.run(source_sync.run_sync())
    assert (result.synced, result.failed) == (0, 0)
    assert world.upserts == []
    assert "decks" not in world.states


def test_a_remove_during_the_turn_stops_it_before_the_next_write(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [
        _file("doc1", DOCS, "2026-01-01T00:00:00.000Z"),
        _file("doc2", DOCS, "2026-01-02T00:00:00.000Z"),
    ]
    real = writes.handle_knowledge_upsert

    async def _upsert_then_remove(inp: Any, **_: Any) -> Any:
        await real(inp)
        # The maintainer removes the source while the turn runs.
        world.sources = [dataclasses.replace(world.sources[0], active=False)]

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _upsert_then_remove)
    asyncio.run(source_sync.run_sync())
    assert [inp.source_uri for inp, _ in world.upserts] == [
        "https://docs.example.com/document/d/doc1/edit"
    ]
    assert "decks" not in world.states


def test_the_worker_row_reads_its_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.workers import registry

    monkeypatch.setattr(settings, "knowledge_source_sync_enabled", True)
    monkeypatch.setattr(settings, "knowledge_source_sync_interval_seconds", 600)
    monkeypatch.setattr(settings, "knowledge_source_sync_idle_interval_seconds", 1200)
    monkeypatch.setattr(settings, "knowledge_source_sync_max_pass_seconds", 300)
    [spec] = [s for s in registry.enabled_workers() if s.name == "knowledge_source_sync"]
    assert (spec.interval_seconds, spec.idle_interval_seconds) == (600, 1200)
    assert spec.deadline_seconds() == 300.0


def _uri(file_id: str, kind: str = "document") -> str:
    return f"https://docs.example.com/{kind}/d/{file_id}/edit"


def test_a_source_another_copy_holds_is_skipped(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("aaa"), _source("bbb")]
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]

    @contextlib.asynccontextmanager
    async def _cluster(name: str, **kw: Any) -> AsyncIterator[Any]:
        if name.endswith("/aaa"):
            raise leases.LeaseBusy(name)
        yield world.lease

    monkeypatch.setattr(leases, "cluster_lease", _cluster)
    result = asyncio.run(source_sync.run_sync())
    assert (result.synced, result.failed) == (1, 0)
    assert "aaa" not in world.states and "bbb" in world.states


def test_a_cut_pass_resumes_at_the_source_it_did_not_start(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("aaa"), _source("bbb"), _source("ccc")]
    cursor = {"at": ""}
    monkeypatch.setattr(source_sync.pass_context, "resume_cursor", lambda: cursor["at"])
    monkeypatch.setattr(
        source_sync.pass_context, "set_cursor", lambda value: cursor.update(at=value)
    )
    order: list[str] = []

    async def _turn(src: KnowledgeSource, state: Any, run: Any) -> Any:
        order.append(src.name)
        run.left -= 1
        return source_sync.TurnResult()

    monkeypatch.setattr(source_sync_recording, "turn", _turn)
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 2)
    first = asyncio.run(source_sync.run_sync())
    assert first.cut and order == ["aaa", "bbb"]
    assert cursor["at"] == f"{ORG}/{NS}/ccc"
    asyncio.run(source_sync.run_sync())
    assert order[2:] == ["ccc", "aaa"]


def test_a_pass_past_its_time_budget_starts_no_source(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The smallest pass the setting allows, with no share of it left to start a source.
    monkeypatch.setattr(settings, "knowledge_source_sync_max_pass_seconds", 180)
    monkeypatch.setattr(source_sync, "_BUDGET_FRACTION", 0.0)
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    result = asyncio.run(source_sync.run_sync())
    assert result.cut and result.synced == 0
    assert world.recording_lists == []


def test_a_failed_retirement_of_a_removed_source_counts_as_a_failure(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.sources = [_source(active=False, pending_delete=True, credential_enc="")]

    async def _fail(inp: Any) -> Any:
        raise RuntimeError("store down")

    monkeypatch.setattr(writes, "handle_knowledge_delete", _fail)
    result = asyncio.run(source_sync.run_sync())
    assert result.failed == 1 and result.retired == 0
    assert world.sources[-1].pending_delete


def test_a_committed_write_that_awaits_publication_counts_as_written(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUBLICATION_PENDING says the write committed and is not to be made again: the
    file counts as written, with no wait that would write it again next pass."""
    from server.core.pg_lease import PublicationPending

    world.files = [_file("doc1", DOCS, REV1)]

    async def _pending(inp: Any, **_: Any) -> Any:
        raise PublicationPending()

    monkeypatch.setattr(writes, "handle_knowledge_upsert", _pending)
    result = asyncio.run(source_sync.run_sync())
    assert (result.changed, result.failed) == (1, 0)
    assert "doc1" not in _detail(world).get("file_waits", {})


def test_a_committed_retirement_that_awaits_publication_counts_as_retired(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.pg_lease import PublicationPending

    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    world.sources = [_source(active=False, pending_delete=True, credential_enc="")]

    async def _pending(inp: Any, **_: Any) -> Any:
        raise PublicationPending()

    monkeypatch.setattr(writes, "handle_knowledge_delete", _pending)
    result = asyncio.run(source_sync.run_sync())
    assert result.failed == 0 and result.retired == 1


def test_the_write_time_owner_read_reads_with_sequential_consistency(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replica that has not seen a person's write during the fetch would answer the
    address is free, so the read just before the write waits for every insert."""
    uri = _file("pdf1", PDF, REV1)["uri"]
    world.files = [_file("pdf1", PDF, "2026-01-01T00:00:00.000Z", size="14")]
    fake = documents.get_one
    sequential: list[bool] = []

    async def _get_one(org: str, ns: str, at: str, **kw: Any) -> Any:
        if at == uri:
            sequential.append(sequential_reads_active())
        return await fake(org, ns, at, **kw)

    monkeypatch.setattr(documents, "get_one", _get_one)
    asyncio.run(source_sync.run_sync())
    assert len(world.upserts) == 1
    assert sequential == [False, True]  # the check before the fetch, then the write's


def test_the_retire_owner_read_reads_with_sequential_consistency(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replica that has not seen a person's write at the address would answer the
    document is still the source's, so the read before a retirement waits for every
    insert."""
    world.files = [_file("doc1", DOCS, "2026-01-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    (uri,) = world.docs
    fake = documents.get_one
    sequential: list[bool] = []

    async def _get_one(org: str, ns: str, at: str, **kw: Any) -> Any:
        if at == uri:
            sequential.append(sequential_reads_active())
        return await fake(org, ns, at, **kw)

    monkeypatch.setattr(documents, "get_one", _get_one)
    assert asyncio.run(source_sync.retire(world.sources[0], uri))
    assert world.docs == {}
    assert sequential == [True]


def test_a_turn_that_raises_records_a_failed_state_and_keeps_the_waits(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.files = [_file("bad1", DOCS, "2026-01-01T00:00:00.000Z")]
    world.transcript_error = ConnectionError("transcript unavailable")
    asyncio.run(source_sync.run_sync())

    async def _down(*a: Any, **kw: Any) -> Any:
        raise ConnectionError("store down")

    monkeypatch.setattr(source_sync.store, "source_revisions", _down)
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    assert (detail["status"], detail["error"]) == ("failed", "ConnectionError")
    assert _detail(world)["file_waits"]["bad1"][1] == 1


def test_a_delete_request_cut_by_the_bound_marks_the_pass_cut(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("gone", active=False, pending_delete=True, credential_enc="")]
    for n in ("old1", "old2"):
        world.docs[_uri(n)] = {
            "actor": SYNC_WORKER_ACTOR,
            "tags": ["source:gone"],
            "metadata": {"ingested_from": n, "source_revision": "r"},
        }
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    result = asyncio.run(source_sync.run_sync())
    assert (result.retired, result.cut) == (1, True)


def test_a_delete_request_past_the_bound_waits_and_marks_the_pass_cut(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bound is spent before the second removed source is reached.
    world.sources = [
        _source(n, active=False, pending_delete=True, credential_enc="") for n in ("aaa", "bbb")
    ]
    for n in ("aaa", "bbb"):
        world.docs[_uri(n)] = {
            "actor": SYNC_WORKER_ACTOR,
            "tags": [f"source:{n}"],
            "metadata": {"ingested_from": n, "source_revision": "r"},
        }
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    result = asyncio.run(source_sync.run_sync())
    assert (result.retired, result.cut) == (1, True)
    assert list(world.docs) == [_uri("bbb")]


# The extraction leg.

DOC1 = "https://docs.example.com/document/d/doc1/edit"
DOC2 = "https://docs.example.com/document/d/doc2/edit"
REV1 = "2026-01-01T00:00:00.000Z"
REV2 = "2026-01-02T00:00:00.000Z"


def _at(world: _World, uri: str, revision: str = "") -> str:
    """The key the extraction leg mines the stored copy at `uri` by: its revision and the
    hash of its text, which every native document's write records."""
    meta = world.docs[uri]["metadata"]
    if revision:
        assert meta["source_revision"] == revision
    return source_sync._mined_revision(
        {"source_revision": meta["source_revision"], "text_sha256": meta.get("text_sha256", "")}
    )


def _two_docs(world: _World) -> None:
    world.files = [_file("doc1", DOCS, REV1), _file("doc2", DOCS, REV2)]


def test_a_written_document_is_mined_in_the_same_pass_as_the_worker(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.observability import cost_accumulator

    monkeypatch.setattr(settings, "knowledge_source_sync_cap_share", 0.4)
    world.files = [_file("doc1", DOCS, REV1)]
    outer = cost_accumulator.begin_op()
    result = asyncio.run(source_sync.run_sync())
    assert [(uri, actor) for uri, actor, _ in world.mined] == [(DOC1, SYNC_WORKER_ACTOR)]
    attribution = world.mined[0][2]
    assert (attribution.org, attribution.namespace) == (ORG, NS)
    assert world.cap_reads == [
        {"operation": "worker.knowledge_source_sync", "share": 0.4, "fail_open": False}
    ]
    # The document's spend bills its namespace, in a row of its own.
    assert [
        (r["org"], r["namespace"], r["operation"], r["llm_input_tokens"]) for r in world.cost_rows
    ] == [(ORG, NS, "worker.knowledge_source_sync", 10)]
    # The pass's own accumulator stays bound, and holds none of it.
    assert cost_accumulator.current() is outer and outer.drain().llm_input_tokens == 0
    assert (result.extracted, result.failed, result.extract_deferred) == (1, 0, 0)
    detail = _detail(world)
    assert (detail["mined"], detail["extract_waits"], detail["pending_extract"]) == (
        {DOC1: _at(world, DOC1)},
        {},
        0,
    )
    assert (detail["extracted"], detail["extract_error"]) == (1, "")
    assert detail["last_extracted_at"]
    # Unchanged on the next pass: nothing is mined again. Changed, it is.
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1
    world.files = [_file("doc1", DOCS, "2026-03-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 2


def test_a_source_retired_after_its_sync_turn_mines_nothing(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source retired between its sync turn and the mining leg is mined nothing: the
    leg's registry check stops it."""
    world.files = [_file("doc1", DOCS, REV1)]
    sync_one = source_sync._sync_one

    async def _then_retired(src: Any, *args: Any) -> None:
        await sync_one(src, *args)
        world.sources = [dataclasses.replace(s, active=False) for s in world.sources]

    monkeypatch.setattr(source_sync, "_sync_one", _then_retired)
    asyncio.run(source_sync.run_sync())
    assert world.upserts and world.mined == []


@pytest.mark.parametrize("stop", ["raises", "stopped"])
def test_a_document_written_by_a_turn_that_did_not_finish_is_mined_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    from server.workers import pass_context

    write_state = source_sync._write_state
    calls: list[int] = []

    async def _first_fails(*a: Any, **kw: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise (
                RuntimeError("state store down") if stop == "raises" else pass_context.PassStopped()
            )
        await write_state(*a, **kw)

    # The turn writes the file, then fails or is stopped before its state is written:
    # nothing records the write but the document itself.
    monkeypatch.setattr(source_sync, "_write_state", _first_fails)
    world.files = [_file("doc1", DOCS, REV1)]
    with contextlib.suppress(pass_context.PassStopped):
        asyncio.run(source_sync.run_sync())
    assert DOC1 in world.docs
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined][-1:] == [DOC1]
    assert _detail(world)["mined"] == {DOC1: _at(world, DOC1)}


def test_pinned_rows_a_run_tags_are_counted_and_kept_through_a_held_turn(
    world: _World,
) -> None:
    from server.core.errors import ModelCallRefused
    from server.memory.app.decision_extract import ExtractReport

    world.mine_answer = lambda uri: ExtractReport(uri, False, flagged=["decision:a", "decision:b"])
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert result.flagged_pinned == 2 and _detail(world)["flagged_pinned"] == 2
    world.mine_answer = lambda uri: ExtractReport(
        uri, False, held=ModelCallRefused("over budget", hold_s=600.0)
    )
    world.files.append(_file("doc2", DOCS, REV2))
    asyncio.run(source_sync.run_sync())
    detail = _detail(world)
    # The held turn tried nothing to completion: the last mining turn's counts stand.
    assert (detail["flagged_pinned"], detail["extracted"]) == (2, 1)
    assert (detail["pending_extract"], detail["extract_error"]) == (1, "held:MODEL_CALL_REFUSED")


@pytest.mark.parametrize("why", ["over_cap", "switched_off", "switch_unread"])
def test_a_capped_or_switched_off_namespace_keeps_its_documents_pending(
    world: _World, why: str
) -> None:
    from server.llm import customer

    if why == "over_cap":
        world.over_cap = {(ORG, NS)}
    elif why == "switched_off":
        world.switched_off = frozenset({ORG})
    else:
        world.switched_off = customer.SwitchUnread("the switch is too old")
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert world.mined == [] and result.extract_deferred == 1 and result.failed == 0
    assert _detail(world)["pending_extract"] == 1
    # Once it may spend again, the next pass mines it with no change at the source.
    world.over_cap, world.switched_off = set(), frozenset()
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert _detail(world)["pending_extract"] == 0


def test_the_cap_is_read_again_before_each_document(world: _World) -> None:
    from server.memory.app.decision_extract import ExtractReport

    def _spend(uri: str) -> ExtractReport:
        world.over_cap = {(ORG, NS)}  # this document's spend reached the share
        return ExtractReport(uri, False)

    world.mine_answer = _spend
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert result.extract_deferred == 1 and _detail(world)["pending_extract"] == 1


def test_a_held_namespace_waits_out_its_hold_with_no_try_counted(world: _World) -> None:
    from server.core.errors import ModelCallRefused
    from server.memory.app.decision_extract import ExtractReport

    world.mine_answer = lambda uri: ExtractReport(
        uri, False, held=ModelCallRefused("over budget", hold_s=600.0)
    )
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    # The hold stops the namespace at its first document.
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert (result.failed, result.extract_deferred) == (0, 1)
    assert result.errors == [f"{ORG}/{NS}/decks: extraction held:MODEL_CALL_REFUSED"]
    detail = _detail(world)
    assert (detail["pending_extract"], detail["extract_waits"], detail["mined"]) == (2, {}, {})
    # While it is held, a later pass makes no call for it.
    result = asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1 and result.extract_deferred == 1


def test_a_hold_on_every_org_stops_the_leg(world: _World) -> None:
    from server.llm import customer
    from server.memory.app.decision_extract import ExtractReport

    world.sources.append(_source("notes", source_id="id-notes"))
    world.docs[DOC2] = {
        "actor": SYNC_WORKER_ACTOR,
        "tags": ["source:notes"],
        "metadata": {"ingested_from": "doc2", "source_revision": REV2},
    }
    world.mine_answer = lambda uri: ExtractReport(uri, False, held=customer.SwitchUnread("old"))
    # Each source lists both files and writes only its own.
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    # The first source's hold ends the leg: the second's document is not tried.
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert result.extract_deferred == 2
    assert _detail(world, "notes")["pending_extract"] == 1


def test_a_failing_extraction_is_given_up_after_its_tries_until_rewritten(
    world: _World,
) -> None:
    from server.memory.app.decision_extract import ExtractReport

    world.mine_answer = lambda uri: ExtractReport(uri, False, error="no chunks")
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert result.failed == 1 and result.errors == [f"{ORG}/{NS}/decks: extraction not_run"]
    detail = _detail(world)
    assert detail["extract_waits"] == {DOC1: [_at(world, DOC1), 1, "not_run"]}
    assert (detail["extract_error"], detail["pending_extract"]) == ("not_run", 1)
    asyncio.run(source_sync.run_sync())
    result = asyncio.run(source_sync.run_sync())
    assert len(world.mined) == source_sync._WAIT_PASSES and result.unextracted == 1
    detail = _detail(world)
    assert (detail["mined"], detail["extract_waits"], detail["pending_extract"]) == (
        {DOC1: _at(world, DOC1)},
        {},
        0,
    )
    # Given up on that revision: a later pass does not try it again.
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == source_sync._WAIT_PASSES
    # Written again, it is tried afresh, and mined.
    world.mine_answer = None
    world.files = [_file("doc1", DOCS, "2026-02-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == source_sync._WAIT_PASSES + 1
    assert (_detail(world)["pending_extract"], _detail(world)["extracted"]) == (0, 1)


@pytest.mark.parametrize("answer", ["refused", "raises", "out_of_time"])
def test_each_failed_document_counts_one_try(
    world: _World, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    from server.memory.app.decision_extract import Dropped, ExtractReport

    if answer == "refused":
        world.mine_answer = lambda uri: ExtractReport(
            uri, False, dropped=[Dropped("s", "decision:x: refused: BUSY", refused=True)]
        )
        reason = "refused"
    elif answer == "raises":
        world.mine_answer = RuntimeError("store down")
        reason = "failed:RuntimeError"
    else:
        world.mine_answer = lambda uri: ExtractReport(
            uri, False, error="time ran out", out_of_time=True
        )
        reason = "out_of_time"
        # Started with less than a whole pass's read time: it may fit in a later pass.
        monkeypatch.setattr(source_sync, "_full_read_s", lambda: 10.0**6)
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    assert result.failed == 2
    assert _detail(world)["extract_waits"] == {
        DOC1: [_at(world, DOC1), 1, reason],
        DOC2: [_at(world, DOC2), 1, reason],
    }


def test_a_document_no_longer_the_source_s_is_not_mined_and_leaves_the_map(
    world: _World,
) -> None:
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    assert set(_detail(world)["mined"]) == {DOC1, DOC2}
    world.docs[DOC1]["actor"] = "agent:someone"
    world.files = [_file("doc2", DOCS, "2026-03-01T00:00:00.000Z")]
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined][2:] == [DOC2]
    assert set(_detail(world)["mined"]) == {DOC2}


def test_mining_shares_the_pass_bound_and_goes_on_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_documents_per_pass", 1)
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    # The one fetch spent the bound: the document waits for the next pass.
    assert world.mined == [] and result.cut and _detail(world)["pending_extract"] == 1
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]


def test_a_document_is_not_started_with_too_little_time_left(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_source_sync_max_pass_seconds", 180)
    # More than the 150 s of read time the smallest pass leaves: no try is spent.
    monkeypatch.setattr(source_sync, "_MIN_MINE_S", 151.0)
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert world.mined == [] and result.cut
    assert (_detail(world)["pending_extract"], _detail(world).get("extract_waits")) == (1, None)


def test_a_sync_listing_shows_the_extraction_counts() -> None:
    from server.knowledge.domain.sources import sync_of

    detail = json.dumps(
        {
            "pending_extract": 2,
            "mined": {DOC1: REV1},
            "extracted": 3,
            "flagged_pinned": 1,
            "extract_error": "",
        }
    )
    assert sync_of(detail) == {
        "pending_extract": 2,
        "extracted": 3,
        "flagged_pinned": 1,
        "extract_error": "",
    }


def test_a_document_out_of_time_with_a_whole_pass_is_given_up_at_once(world: _World) -> None:
    from server.memory.app.decision_extract import ExtractReport

    world.mine_answer = lambda uri: ExtractReport(uri, False, error="late", out_of_time=True)
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert result.unextracted == 1
    detail = _detail(world)
    assert (detail["mined"], detail["extract_waits"]) == ({DOC1: _at(world, DOC1)}, {})
    assert (detail["unextracted"], detail["extract_error"]) == (1, "too_large_for_pass")
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1


def test_a_document_that_ran_out_of_time_goes_first_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app.decision_extract import ExtractReport

    monkeypatch.setattr(source_sync, "_full_read_s", lambda: 10.0**6)
    world.mine_answer = lambda uri: ExtractReport(uri, False, error="late", out_of_time=uri == DOC2)
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    # doc1 failed for another reason, doc2 ran out of time: doc2 goes first.
    world.mined.clear()
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC2, DOC1]


def test_a_permanent_refusal_settles_the_document(world: _World) -> None:
    from server.memory.app.decision_extract import Dropped, ExtractReport

    world.mine_answer = lambda uri: ExtractReport(
        uri,
        False,
        dropped=[Dropped("s", "decision:x: refused: BLACKLISTED", refused=True, permanent=True)],
    )
    world.files = [_file("doc1", DOCS, REV1)]
    result = asyncio.run(source_sync.run_sync())
    assert (result.extracted, result.failed) == (1, 0)
    detail = _detail(world)
    assert (detail["mined"], detail["extract_waits"]) == ({DOC1: _at(world, DOC1)}, {})
    asyncio.run(source_sync.run_sync())
    assert len(world.mined) == 1


def test_a_run_stopped_mid_write_records_nothing_and_is_mined_next_pass(world: _World) -> None:
    from server.memory.app.decision_extract import ExtractReport

    def _lost(uri: str) -> ExtractReport:
        world.lease.held = False  # the lease went while the run wrote
        return ExtractReport(uri, False, error="stopped", write_stopped=True)

    world.mine_answer = _lost
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    # One try, then the source's turn stops: no state write under a lost lease, nothing settled.
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert "mined" not in _detail(world)
    world.lease.held, world.mine_answer = True, None
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined][1:] == [DOC1, DOC2]


@pytest.mark.parametrize(
    "why", ["no_model", "switched_off", "over_cap", "switch_unread", "held:MODEL_CALL_REFUSED"]
)
def test_a_deferred_source_shows_why_it_waits(
    world: _World, monkeypatch: pytest.MonkeyPatch, why: str
) -> None:
    from server.core.errors import ModelCallRefused
    from server.llm import customer
    from server.memory.app import decision_extract
    from server.memory.app.decision_extract import ExtractReport

    if why == "no_model":
        monkeypatch.setattr(decision_extract, "no_model_error", lambda: "no key")
    elif why == "switched_off":
        world.switched_off = frozenset({ORG})
    elif why == "over_cap":
        world.over_cap = {(ORG, NS)}
    elif why == "switch_unread":
        world.switched_off = customer.SwitchUnread("old")
    else:
        world.mine_answer = lambda uri: ExtractReport(
            uri, False, held=ModelCallRefused("budget", hold_s=60.0)
        )
    world.files = [_file("doc1", DOCS, REV1)]
    asyncio.run(source_sync.run_sync())
    assert (_detail(world)["extract_error"], _detail(world)["pending_extract"]) == (why, 1)


def test_the_pass_bound_inside_a_turn_is_why_the_rest_wait(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = [True, True, False]  # the leg's check, the first document's, the second's
    monkeypatch.setattr(source_sync, "_time_for_one", lambda run: answers.pop(0))
    _two_docs(world)
    result = asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert result.cut and result.extract_deferred == 1
    detail = _detail(world)
    assert (detail["pending_extract"], detail["extract_error"]) == (1, "pass_bound")


def test_the_reason_clears_once_nothing_waits(world: _World) -> None:
    world.over_cap = {(ORG, NS)}
    world.files = [_file("doc1", DOCS, REV1)]
    asyncio.run(source_sync.run_sync())
    assert _detail(world)["extract_error"] == "over_cap"
    world.docs[DOC1]["actor"] = "agent:someone"  # no longer the source's: nothing waits
    asyncio.run(source_sync.run_sync())
    assert (_detail(world)["extract_error"], _detail(world)["pending_extract"]) == ("", 0)


def _second_source(world: _World) -> None:
    world.sources.append(_source("notes", source_id="id-notes"))
    world.docs[DOC2] = {
        "actor": SYNC_WORKER_ACTOR,
        "tags": ["source:notes"],
        "metadata": {"ingested_from": "doc2", "source_revision": REV2},
    }


def test_a_lease_lost_mid_write_ends_that_source_and_the_leg_goes_on(world: _World) -> None:
    from server.memory.app.decision_extract import ExtractReport

    _second_source(world)

    def _lost(uri: str) -> ExtractReport | None:
        if uri != DOC1:
            return None
        world.lease.held = False
        return ExtractReport(uri, False, error="stopped", write_stopped=True)

    world.mine_answer = _lost
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    # The first source records nothing; the second, under its own lease, is mined.
    assert [uri for uri, _, _ in world.mined] == [DOC1, DOC2]
    assert "mined" not in _detail(world) and _detail(world, "notes")["mined"] == {DOC2: REV2}


def test_a_stopped_pass_ends_the_leg_with_nothing_recorded(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app.decision_extract import ExtractReport
    from server.workers import pass_context

    _second_source(world)
    stopped: list[bool] = []
    monkeypatch.setattr(pass_context, "stop_requested", lambda: bool(stopped))

    def _stop(uri: str) -> ExtractReport:
        stopped.append(True)
        return ExtractReport(uri, False, error="stopped", write_stopped=True)

    world.mine_answer = _stop
    _two_docs(world)
    asyncio.run(source_sync.run_sync())
    assert [uri for uri, _, _ in world.mined] == [DOC1]
    assert "mined" not in _detail(world) and "mined" not in _detail(world, "notes")


@pytest.mark.parametrize("how", ["lease_lost", "check_stop", "cancelled"])
def test_a_turn_ended_from_outside_writes_no_state_and_is_mined_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    from server.memory.app import decision_extract
    from server.workers import pass_context

    world.files = [_file("doc1", DOCS, REV1)]
    asyncio.run(source_sync.run_sync())  # synced, then mined
    mined_before = _detail(world)["mined"]
    world.files = [_file("doc1", DOCS, "2026-03-01T00:00:00.000Z")]
    started = asyncio.Event()
    checks = (pass_context.check_stop, source_sync._extract_all)
    mined = decision_extract.extract_document

    async def _ends(*a: Any, **kw: Any) -> Any:
        started.set()
        if how == "lease_lost":
            raise asyncio.CancelledError  # as the lease's loss cancels the turn's body
        await asyncio.sleep(5)

    if how == "lease_lost":

        @contextlib.asynccontextmanager
        async def _losing(name: str, **kw: Any) -> AsyncIterator[Any]:
            try:
                yield world.lease
            except asyncio.CancelledError:
                raise leases.LeaseLost(name) from None

        monkeypatch.setattr(leases, "cluster_lease", _losing)
    if how == "check_stop":
        # The supervisor stops the pass once the leg has started: the leg's own check
        # before the document raises.
        leg: list[bool] = []
        real_check, real_all = checks

        def _check() -> None:
            if leg:
                raise pass_context.PassStopped
            real_check()

        async def _all(*a: Any, **kw: Any) -> None:
            leg.append(True)
            await real_all(*a, **kw)

        monkeypatch.setattr(pass_context, "check_stop", _check)
        monkeypatch.setattr(source_sync, "_extract_all", _all)
    else:
        monkeypatch.setattr(decision_extract, "extract_document", _ends)

    async def _run() -> None:
        task = asyncio.create_task(source_sync.run_sync())
        if how == "cancelled":
            await started.wait()
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, pass_context.PassStopped):
            await task

    asyncio.run(_run())
    assert _detail(world)["mined"] == mined_before
    # The next pass, with nothing ending it.
    monkeypatch.setattr(pass_context, "check_stop", checks[0])
    monkeypatch.setattr(source_sync, "_extract_all", checks[1])
    monkeypatch.setattr(decision_extract, "extract_document", mined)
    monkeypatch.setattr(leases, "cluster_lease", _plain_lease(world))
    asyncio.run(source_sync.run_sync())
    assert _detail(world)["mined"] == {DOC1: _at(world, DOC1, "2026-03-01T00:00:00.000Z")}


def _plain_lease(world: _World) -> Any:
    @contextlib.asynccontextmanager
    async def _cluster(name: str, **kw: Any) -> AsyncIterator[Any]:
        yield world.lease

    return _cluster


def test_a_write_unit_a_cancel_leaves_running_is_billed(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app import decision_extract
    from server.observability import cost_accumulator

    started = asyncio.Event()

    async def _unit() -> None:
        started.set()
        await asyncio.sleep(0.05)
        cost_accumulator.report_llm("m", 7, 1)  # the write's embedding, say

    async def _extract(*a: Any, **kw: Any) -> Any:
        # As extract_document holds its units in the run's own set.
        await decision_extract._whole(kw["writes"], _unit())

    monkeypatch.setattr(decision_extract, "extract_document", _extract)
    world.files = [_file("doc1", DOCS, REV1)]

    async def _run() -> None:
        task = asyncio.create_task(source_sync.run_sync())
        await started.wait()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert [(r["org"], r["llm_input_tokens"]) for r in world.cost_rows] == [(ORG, 7)]


def test_a_failed_state_write_of_a_waiting_source_does_not_stop_the_leg(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.errors import OverloadedError

    _second_source(world)
    # The first source has nothing left to mine but a stale count to clear.
    world.states["decks"] = {"state_value": "", "detail": json.dumps({"pending_extract": 3})}
    write_mined = source_sync._write_mined

    async def _busy_once(src: Any, mined: Any, *a: Any, **kw: Any) -> None:
        if mined is None and src.name == "decks":
            raise OverloadedError("the sources lease is busy")
        await write_mined(src, mined, *a, **kw)

    monkeypatch.setattr(source_sync, "_write_mined", _busy_once)
    world.files = [_file("doc2", DOCS, REV2)]  # notes' file: decks lists it and skips it
    result = asyncio.run(source_sync.run_sync())
    assert result.errors == [f"{ORG}/{NS}/decks: extraction OverloadedError"]
    assert result.failed == 1
    assert [uri for uri, _, _ in world.mined] == [DOC2]
    assert _detail(world, "notes")["mined"] == {DOC2: REV2}


def test_a_failed_read_of_a_source_s_documents_is_that_source_s_error(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.workers import pass_context

    _second_source(world)
    world.files = [_file("doc2", DOCS, REV2)]
    revisions = source_sync.store.source_revisions
    noted: list[str] = []

    async def _decks_down(org: str, ns: str, tag: str, **kw: Any) -> Any:
        if tag == "source:decks" and kw.get("searchable_only", True) and noted == ["sync-done"]:
            raise ConnectionError("store down")
        return await revisions(org, ns, tag, **kw)

    write_state = source_sync._write_state

    async def _then(*a: Any, **kw: Any) -> None:
        await write_state(*a, **kw)
        if a[0].name == "notes":
            noted.append("sync-done")  # both turns done: the leg's read of decks fails

    monkeypatch.setattr(source_sync.store, "source_revisions", _decks_down)
    monkeypatch.setattr(source_sync, "_write_state", _then)
    failures: list[str] = []
    monkeypatch.setattr(pass_context, "note_failure", lambda e, what="": failures.append(what))
    result = asyncio.run(source_sync.run_sync())
    assert result.errors == [f"{ORG}/{NS}/decks: extraction ConnectionError"]
    assert result.failed == 1 and failures == [f"{ORG}/{NS}/decks"]
    # The leg goes on: the other source's document is mined.
    assert [uri for uri, _, _ in world.mined] == [DOC2]


def test_only_a_trunk_source_tags_its_documents_with_the_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    def entry(namespace: str, branch: str) -> SimpleNamespace:
        return SimpleNamespace(
            org=ORG,
            repo="acme/app",
            namespace=namespace,
            git_branch=branch,
            git_remote="https://github.com/acme/app",
        )

    entries = [entry("code-acme-app", "main"), entry("code-acme-app-dev", "dev")]
    monkeypatch.setattr(source_sync, "configured_repos", lambda: entries)
    assert source_sync._repo_tag(_source(namespace="code-acme-app")).startswith("repo:")
    assert source_sync._repo_tag(_source(namespace="code-acme-app-dev")) == ""
    assert source_sync._repo_tag(_source(namespace="team-notes")) == ""


# ---- items -------------------------------------------------------------------


def test_every_stop_short_mark_reads_as_incomplete_on_the_dashboard() -> None:
    root = Path(__file__).resolve().parents[2]
    badge = (root / "frontend/src/lib/knowledgeSources.ts").read_text()
    prefixes = re.findall(r'"([^"]+)"', re.search(r"CUT_SHORT = \[([^\]]*)\]", badge).group(1))
    assert all(any(m.startswith(p) for p in prefixes) for m in source_sync.TurnMark)


# Out of time before the walk, or after the listing during the item check.


def test_the_pass_bound_mark_leads_an_earlier_mark() -> None:
    turn = source_sync.TurnResult()
    turn.mark(source_sync.TurnMark.INCOMPLETE_SEARCH)
    turn.mark(source_sync.TurnMark.PASS_BOUND)
    assert turn.cut
    assert turn.state_error == "pass bound reached; listing: incomplete search"


@pytest.mark.parametrize(("starved", "resumes"), [("bbb", "bbb"), ("aaa", "bbb")])
def test_a_source_out_of_time_before_any_file_starts_the_next_pass(
    world: _World, monkeypatch: pytest.MonkeyPatch, starved: str, resumes: str
) -> None:
    world.sources = [_source("aaa"), _source("bbb"), _source("ccc")]
    cursor = {"at": ""}
    monkeypatch.setattr(source_sync.pass_context, "resume_cursor", lambda: cursor["at"])
    monkeypatch.setattr(
        source_sync.pass_context, "set_cursor", lambda value: cursor.update(at=value)
    )

    async def _turn(src: KnowledgeSource, state: Any, run: Any) -> Any:
        turn = source_sync.TurnResult()
        if src.name == starved:
            turn.mark(source_sync.TurnMark.PASS_BOUND)
            run.left = 0
        return turn

    monkeypatch.setattr(source_sync_recording, "turn", _turn)
    asyncio.run(source_sync.run_sync())
    # On leftover time it goes first next; first and still starved, it moves on.
    assert cursor["at"] == f"{ORG}/{NS}/{resumes}"


def test_a_requested_source_out_of_time_leaves_the_rest_their_place(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("aaa"), _source("bbb"), _source("ccc")]
    cursor = {"at": f"{ORG}/{NS}/ccc"}
    monkeypatch.setattr(source_sync.pass_context, "resume_cursor", lambda: cursor["at"])
    monkeypatch.setattr(
        source_sync.pass_context, "set_cursor", lambda value: cursor.update(at=value)
    )
    for name in ("aaa", "bbb"):
        world.states[name] = {
            "state_value": "",
            "detail": json.dumps(
                {"full": True, "requested_at": f"2026-01-0{1 + (name == 'bbb')}T00:00:00+00:00"}
            ),
        }

    async def _turn(src: KnowledgeSource, state: Any, run: Any) -> Any:
        turn = source_sync.TurnResult()
        if src.name == "bbb":
            turn.mark(source_sync.TurnMark.PASS_BOUND)
            run.left = 0
        return turn

    monkeypatch.setattr(source_sync_recording, "turn", _turn)
    asyncio.run(source_sync.run_sync())
    # bbb's request puts it first again; ccc, where the rest resumed, keeps its place.
    assert cursor["at"] == f"{ORG}/{NS}/ccc"


def test_a_slow_first_of_the_rest_does_not_hold_back_the_others_behind_a_request(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.sources = [_source("aaa"), _source("bbb"), _source("ccc")]
    cursor = {"at": ""}
    monkeypatch.setattr(source_sync.pass_context, "resume_cursor", lambda: cursor["at"])
    monkeypatch.setattr(
        source_sync.pass_context, "set_cursor", lambda value: cursor.update(at=value)
    )
    # aaa's request stays waiting (its turn never completes); bbb never has time.
    detail = json.dumps({"full": True, "requested_at": "2026-01-01T00:00:00+00:00"})
    world.states["aaa"] = {"state_value": "", "detail": detail}
    synced: list[str] = []

    async def _turn(src: KnowledgeSource, state: Any, run: Any) -> Any:
        synced.append(src.name)
        turn = source_sync.TurnResult()
        if src.name == "aaa":
            turn.mark(source_sync.TurnMark.INCOMPLETE_SEARCH)
        elif src.name == "bbb":
            turn.mark(source_sync.TurnMark.PASS_BOUND)
            run.left = 0
        return turn

    monkeypatch.setattr(source_sync_recording, "turn", _turn)
    asyncio.run(source_sync.run_sync())
    asyncio.run(source_sync.run_sync())
    # Pass 1: bbb led the rest and ran out, so pass 2 starts the rest at ccc; there bbb,
    # not first, ran out again on what was left, so the next pass starts with it.
    assert synced == ["aaa", "bbb", "aaa", "ccc", "bbb"]
    assert cursor["at"] == f"{ORG}/{NS}/bbb"


@pytest.mark.parametrize(
    ("kind", "provider", "status", "expected_failures"),
    [
        ("recording", "meet", "provider_removed", 0),
        ("recording", "zoom", "provider_removed", 1),
        ("gdrive", "meet", "provider_removed", 1),
        ("recording", "meet", "failed", 1),
        ("recording", "meet", "credential_invalid", 1),
    ],
)
def test_only_a_retired_meet_turn_is_not_a_pass_failure(
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    provider: str,
    status: str,
    expected_failures: int,
) -> None:
    src = dataclasses.replace(_source(), kind=kind, config={"provider": provider})
    world.sources = [src]
    before = {
        "requested_at": "2026-01-01T00:00:00Z",
        "full": True,
        "file_waits": {"old-file": ["old-revision", 2, "old-reason"]},
    }
    world.states[src.name] = {"state_value": "old-stamp", "detail": json.dumps(before)}
    failures: list[Any] = []

    async def _turn(source: Any, state: Any, run: Any) -> source_sync.TurnResult:
        return source_sync.TurnResult(
            status=status,
            complete=False,
            error="retired provider",
        )

    monkeypatch.setattr(source_sync, "_turn_of", lambda source: _turn)
    monkeypatch.setattr(
        source_sync.pass_context, "note_failure", lambda *args: failures.append(args)
    )
    result = source_sync.PassResult()
    run = source_sync.Pass(source_sync._budget(), left=5)
    turn = asyncio.run(source_sync._sync_turn(src, world.states[src.name], run, result))
    assert turn is not None and turn.status == status and not turn.complete
    assert result.failed == len(result.errors) == len(failures) == expected_failures
    assert (result.changed, result.removed, result.unconverted, result.skipped) == (0, 0, 0, 0)
    assert world.states[src.name]["state_value"] == "old-stamp"
    detail = _detail(world, src.name)
    assert detail["status"] == status
    assert {key: detail[key] for key in before} == before
    assert not world.recording_lists and not world.upserts and not world.audits
