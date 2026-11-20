"""Restore orchestration ends real connected authority before publishing held copies."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

import httpx
import pytest

from server.cli import restore as cli_restore
from server.code.app.index import repo_registry
from server.erasure.app import replay
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import restore_reconcile as knowledge_restore
from server.memory.app import restore_reconcile as memory_restore
from tests.unit.test_connected_source_authority import _KEY
from tests.unit.test_connected_source_operations import _World


class _RestoreWorld(_World):
    """Historical row/lease/Keeper/provider I/O; connection retirement stays real."""

    def __init__(self, monkeypatch, http):
        self.account_history = []
        self.source_history = []
        self.revokes = []
        super().__init__(monkeypatch, http)
        self.account_history = [self.rows[_KEY]]
        self.source_history = [self.source]
        monkeypatch.setattr(accounts.store, "_default_client", lambda: self)

    async def put(self, row, **kwargs):
        await super().put(row, **kwargs)
        self.account_history.append(row)

    async def erase(self, org, subjects, *, kind, **kwargs):
        await super().erase(org, subjects, kind=kind, **kwargs)
        self.account_history = [
            row
            for row in self.account_history
            if not (row.org == org and row.subject in subjects and row.kind == kind)
        ]

    async def query_all(self, query, params, **kwargs):
        if "FROM loom.connected_accounts" in query:
            assert "FINAL" not in query
            assert "credential_enc" not in query and "account_email_enc" not in query
            return [
                {key: asdict(row)[key] for key in ("org", "subject", "kind", "connection_id")}
                for row in self.account_history
            ]
        if "FROM loom.knowledge_sources" in query and "DISTINCT" in query:
            assert "FINAL" not in query
            rows = self.source_history
            if "namespace" not in query.split("FROM", 1)[0]:
                return [{"namespace": row.namespace} for row in rows]
            return [
                {
                    "org": row.org,
                    "namespace": row.namespace,
                    "name": row.name,
                    "source_id": row.source_id,
                    "source_kind": row.kind,
                    "owner_subject": row.owner_subject,
                    "connection_id": row.connection_id,
                }
                for row in rows
            ]
        return await super().query_all(query, params, **kwargs)

    async def query_one(self, query, params=None, **kwargs):
        if "FROM loom.worker_status" in query:
            return {"n": 0}
        if "SELECT count() AS n FROM loom.connected_accounts" in query:
            if params is None:
                return {"n": len(self.account_history)}
            rows = [
                row
                for row in self.account_history
                if row.org == params["org"]
                and row.subject == params["subject"]
                and row.kind == params["kind"]
            ]
            keep = params.get("keep")
            if keep:
                rows = [
                    row
                    for row in rows
                    if not (
                        row.connection_id == keep
                        and not row.active
                        and not row.account_id
                        and not row.workspace_id
                        and not row.account_email_enc
                        and not row.credential_enc
                        and not row.scopes
                        and not row.refresh_id
                        and row.connected_at is None
                    )
                ]
            return {"n": len(rows)}
        if "SELECT count() AS n FROM loom.knowledge_sources" in query:
            assert "FINAL" not in query
            return {"n": len(self.source_history)}
        return await super().query_one(query, params, **kwargs)

    async def execute(self, query, *, params, **kwargs):
        assert query.startswith("DELETE FROM loom.knowledge_sources")
        assert kwargs["settings"]["lightweight_deletes_sync"] == 2

        def matches(row):
            return (
                row.org == params["org"]
                and row.namespace == params["ns"]
                and row.owner_subject == params["owner"]
                and row.kind == params["kind"]
            )

        self.source_history = [row for row in self.source_history if not matches(row)]
        if self.source is not None and matches(self.source):
            self.source = None

    async def handle(self, request):
        if request.url.host == "oauth2.googleapis.com" and request.url.path == "/revoke":
            assert not self.rows and not self.source_history
            assert not any(self.holders.values())
            self.revokes.append(request)
            return httpx.Response(200)
        return await super().handle(request)


def _unrelated_restore_participants(monkeypatch, before_publish):
    """Other planes' recovery participants are external to this connection concern."""

    async def empty(*args, **kwargs):
        return []

    async def zero(*args, **kwargs):
        return 0

    async def readable(*args, **kwargs):
        return True

    async def publish():
        before_publish()
        return 0

    monkeypatch.setattr(repo_registry, "read_registry", readable)
    monkeypatch.setattr(replay, "completed_since", empty)
    monkeypatch.setattr(replay, "still_open", empty)
    monkeypatch.setattr(memory_restore, "redactions_lost_by", empty)
    monkeypatch.setattr(memory_restore, "hold", zero)
    monkeypatch.setattr(memory_restore, "hold_heads_behind", zero)
    monkeypatch.setattr(memory_restore, "publish_held", publish)
    monkeypatch.setattr(knowledge_restore, "hold", zero)
    monkeypatch.setattr(knowledge_restore, "hold_documents_behind", zero)
    monkeypatch.setattr(knowledge_restore, "held_sources", empty)
    monkeypatch.setattr(knowledge_restore, "dataset_sources_lost", empty)
    monkeypatch.setattr(knowledge_restore, "publish_held", publish)


@pytest.mark.parametrize("restored", ("clickhouse", "postgres"))
async def test_reconcile_ends_connected_owners_before_any_publication(monkeypatch, restored):
    async def provider(request):
        return await state.handle(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        state = _RestoreWorld(monkeypatch, http)
        published = []

        def before_publish():
            assert not state.rows and not state.source_history
            published.append(True)

        _unrelated_restore_participants(monkeypatch, before_publish)
        result = await cli_restore.reconcile(restored, datetime.now(UTC), apply=True, ch=state)
        assert result == 0
        assert not state.rows and not state.source_history
        assert state.retirement_rows
        assert all(value.partition("/")[2].startswith("0") for value in state.fences.values())
        assert len(state.revokes) == 1
        assert len(published) == (2 if restored == "clickhouse" else 0)
