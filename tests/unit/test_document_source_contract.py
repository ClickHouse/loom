"""Recording admission and retained opaque source privacy."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from server.core.errors import BadConfigError, ValidationError
from server.knowledge.api.models_sources import (
    KnowledgeSourceAddInput,
    KnowledgeSourceAddOutput,
    KnowledgeSourceCheckOutput,
    KnowledgeSourceEntry,
    KnowledgeSourceInspectOutput,
    KnowledgeSourceProbe,
    KnowledgeSourceRemoveInput,
)
from server.knowledge.domain import sources as domain


def _recording(**kwargs: Any) -> domain.KnowledgeSource:
    return domain.KnowledgeSource(
        org="org", namespace="notes", name="meetings", source_id="source-id", **kwargs
    )


def test_recording_config_deduplicates_valid_ids_without_changing_order() -> None:
    assert domain.parse_config(
        "recording", {"provider": "zoom", "ids": [123456789, "987654321", "123456789"]}
    ) == {"provider": "zoom", "ids": ["123456789", "987654321"]}


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"provider": "unknown", "ids": ["123456789"]},
        {"provider": "zoom", "ids": []},
        {"provider": "zoom", "ids": [True]},
        {"provider": "zoom", "ids": ["not-an-id"]},
        {"provider": "zoom", "ids": ["123456789"], "include": ["docs"]},
        {"provider": "zoom", "ids": [str(100000000 + n) for n in range(21)]},
    ],
)
def test_recording_config_refuses_invalid_selection(config: dict[str, Any]) -> None:
    with pytest.raises(BadConfigError):
        domain.parse_config("recording", config)


@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear", "mcp", "unknown"])
def test_unavailable_kinds_have_no_native_config_admission(kind: str) -> None:
    with pytest.raises(BadConfigError, match="unavailable"):
        domain.parse_config(kind, {"provider": "zoom", "ids": ["123456789"]})


def test_recording_api_keeps_config_and_probe_without_native_output_fields() -> None:
    config = {"provider": "zoom", "ids": ["123456789"]}
    request = KnowledgeSourceAddInput(
        org="org", namespace="notes", name="meetings", config=config, credential="private-key"
    )
    assert request.kind == "recording"
    assert request.config == config
    assert "private-key" not in repr(request)
    assert "credential" not in request.model_dump()
    output = KnowledgeSourceAddOutput(
        name="meetings",
        kind="recording",
        namespace="notes",
        config=config,
        probe=KnowledgeSourceProbe(documents=2, sample=["Meeting"], more=True),
    )
    assert output.config == config
    assert output.probe.model_dump() == {"documents": 2, "more": True, "sample": ["Meeting"]}
    for model in (KnowledgeSourceAddOutput, KnowledgeSourceEntry, KnowledgeSourceInspectOutput):
        assert "items" not in model.model_fields
        assert "include" not in model.model_fields
    for model in (KnowledgeSourceProbe, KnowledgeSourceCheckOutput):
        assert "checked" not in model.model_fields
        assert "sampled" not in model.model_fields


def test_native_add_arguments_are_removed_fields_for_ignored_args_reporting() -> None:
    request = KnowledgeSourceAddInput(
        org="org",
        namespace="notes",
        name="meetings",
        config={"provider": "zoom", "ids": ["123456789"]},
        credential="recording-key",
        items=[{"type": "folder", "id": "legacy-folder"}],
        links=["https://example.com/legacy"],
        include=["docs"],
        credential_mode="connected",
    )
    assert request.kind == "recording"
    for field in ("items", "links", "include", "credential_mode"):
        assert field not in KnowledgeSourceAddInput.model_fields
        assert field not in request.model_dump()


@pytest.mark.parametrize(
    ("kind", "mode", "version", "access"),
    [
        ("gdrive", "shared", 0, domain.NAMESPACE_ACCESS),
        ("notion", "shared", 0, domain.NAMESPACE_ACCESS),
        ("linear", "connected", 0, domain.MIRRORED_ACCESS),
        ("unknown", "shared", 0, domain.NAMESPACE_ACCESS),
        ("recording", "connected", 0, domain.NAMESPACE_ACCESS),
        ("recording", "shared", 1, domain.NAMESPACE_ACCESS),
        ("recording", "shared", 0, domain.MIRRORED_ACCESS),
    ],
)
def test_retained_registrations_are_unavailable_and_redact_sync_details(
    kind: str, mode: str, version: int, access: str
) -> None:
    source = _recording(
        kind=kind, credential_mode=mode, source_contract_version=version, access=access
    )
    assert not domain.served(source)
    assert not domain.mirror_synced(source)
    assert domain.id_list_config(source) is None
    assert domain.sync_shown(
        source,
        '{"status":"failed","requested_at":"requested","last_synced_at":"finished",'
        '"documents_seen":9,"error":"private title","unconverted_reasons":{"private":1}}',
    ) == {"status": "failed", "requested_at": "requested", "last_synced_at": "finished"}


def test_recording_retains_its_config_and_operational_sync_counts() -> None:
    config = {"provider": "zoom", "ids": ["123456789"]}
    source = _recording(kind="recording", config=config)
    assert domain.served(source)
    assert not domain.opaque_payload(kind=source.kind, mode=source.credential_mode, version=0)
    assert domain.id_list_config(source) == config
    assert domain.stored_config(source) == config
    assert domain.sync_shown(source, '{"status":"ok","documents_seen":2,"cursor":"private"}') == {
        "status": "ok",
        "documents_seen": 2,
    }


def test_only_current_mcp_contract_can_sync_protected_copies() -> None:
    source = _recording(
        kind="mcp",
        credential_mode="connected",
        source_contract_version=1,
        access=domain.MIRRORED_ACCESS,
    )
    assert domain.mirror_synced(source)
    assert not domain.served(source)
    assert not domain.mirror_synced(replace(source, source_contract_version=2))


@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear", "mcp", "unknown"])
@pytest.mark.parametrize("native_kind_column", [True, False])
@pytest.mark.parametrize("mode", ["shared", "connected"])
def test_opaque_lifecycle_roundtrip_preserves_bytes_identity_and_sealed_columns(
    monkeypatch: pytest.MonkeyPatch, kind: str, native_kind_column: bool, mode: str
) -> None:
    from server.knowledge.store import sources as store

    row = {
        "org": "org",
        "namespace": "notes",
        "name": "retained",
        "source_id": "immutable-id",
        "kind": kind if native_kind_column else "",
        "source_kind": "" if native_kind_column else kind,
        "config_json": ' { "unknown" : [3,2,1], "private" : "original" } ',
        "items_json": ' [ { "opaque" : "item" } ] ',
        "credential_enc": "sealed-original",
        "secret_enc": "sealed-items",
        "access": domain.MIRRORED_ACCESS,
        "credential_mode": mode,
        "owner_subject": "canonical-owner",
        "connection_id": "account-key",
        "connection_slot": "generic-slot",
        "source_contract_version": 0,
        "created_by": "writer",
        "active": False,
        "pending_delete": True,
    }
    written: list[dict[str, Any]] = []
    stamp = datetime(2024, 1, 1)
    row.update(created_at=stamp, updated_at=stamp)
    monkeypatch.setattr(store, "naive_utc_now_seconds", lambda: stamp)

    class Client:
        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            assert table == store.TABLE
            written.extend(rows)

    monkeypatch.setattr(store, "_default_client", Client)
    source = store._source(row)
    assert source.kind == kind
    assert source.config == {}
    assert source.items == []
    assert "original" not in repr(source)
    assert '"opaque"' not in repr(source)
    assert not domain.served(source)
    assert not domain.mirror_synced(source)
    asyncio.run(store.insert_source(source))
    assert len(written) == 1
    saved = written[0]
    for key in (
        "org",
        "namespace",
        "name",
        "source_id",
        "config_json",
        "items_json",
        "credential_enc",
        "secret_enc",
        "access",
        "credential_mode",
        "owner_subject",
        "connection_id",
        "connection_slot",
        "source_contract_version",
        "created_by",
        "active",
        "pending_delete",
    ):
        assert saved[key] == row[key], key
    assert saved["kind"] == ""
    assert saved["source_kind"] == kind
    assert store._source(saved) == source


def test_recording_probe_checks_selected_ids_and_returns_recent_titles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.app import source_probes, source_sync_recording

    config = {"provider": "zoom", "ids": ["123456789", "987654321"]}
    session = object()
    open_session = AsyncMock(return_value=session)
    list_recordings = AsyncMock(
        return_value=(
            [
                {"name": "Earlier meeting", "modifiedTime": "2024-01-01T00:00:00Z"},
                {"name": "Later meeting", "modifiedTime": "2024-01-02T00:00:00Z"},
            ],
            [],
        )
    )
    monkeypatch.setattr(source_sync_recording, "open_session", open_session)
    monkeypatch.setattr(source_sync_recording, "list_recordings", list_recordings)
    result = asyncio.run(source_probes.probe("recording", config, "recording-key"))
    open_session.assert_awaited_once_with("zoom", "recording-key")
    list_recordings.assert_awaited_once_with(session, config["ids"])
    assert result.documents == 2
    assert result.sample == ["Later meeting", "Earlier meeting"]
    assert not result.more


def test_recording_row_roundtrip_keeps_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.knowledge.store import sources as store

    config = {"provider": "zoom", "ids": ["123456789"]}
    source = _recording(kind="recording", config=config, credential_enc="sealed-recording")
    written: list[dict[str, Any]] = []

    class Client:
        async def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
            assert table == store.TABLE
            written.extend(rows)

    monkeypatch.setattr(store, "_default_client", Client)
    asyncio.run(store.insert_source(source))
    restored = store._source(written[0])
    assert restored.config == config
    assert restored.items == []
    assert restored.credential_enc == "sealed-recording"
    assert not restored.secret_enc
    assert not restored.generic_config_json
    assert domain.served(restored)


def test_recording_credential_uses_its_column_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.knowledge.app import sources as app
    from server.settings import settings

    monkeypatch.setattr(settings, "jwt_secret", "unit-test-root-secret")
    source = _recording(kind="recording", config={"provider": "zoom", "ids": ["123456789"]})
    sealed = app._with_credential(source, "recording-key")
    assert sealed.credential_enc
    assert not sealed.secret_enc
    assert app.credential_of(sealed) == "recording-key"
    assert (
        app.credential_of(replace(sealed, credential_enc="", secret_enc=sealed.credential_enc))
        == ""
    )
    retained = replace(
        source, kind="gdrive", secret_enc=app.seal(app._SECRET_PURPOSE, "retained-key")
    )
    assert retained.secret_enc and app.credential_of(retained) == ""
    with pytest.raises(ValidationError):
        app._with_credential(retained, "replacement-key")


@pytest.mark.parametrize(
    ("access", "delete_documents", "active"),
    [
        (domain.NAMESPACE_ACCESS, False, True),
        (domain.NAMESPACE_ACCESS, True, True),
        (domain.NAMESPACE_ACCESS, True, False),
        (domain.MIRRORED_ACCESS, False, True),
        (domain.MIRRORED_ACCESS, True, True),
    ],
)
def test_explicit_legacy_retirement_preserves_opaque_metadata_and_hides_mirror_counts(
    monkeypatch: pytest.MonkeyPatch, access: str, delete_documents: bool, active: bool
) -> None:
    from server.knowledge.app import source_push
    from server.knowledge.app import sources as app

    source = _recording(
        kind="gdrive",
        access=access,
        active=active,
        credential_enc="sealed-original",
        secret_enc="sealed-items",
        owner_subject="stored-owner",
        connection_id="stored-account",
        generic_config_json=' { "opaque" : "original config" } ',
        generic_items_json=' [ { "opaque" : "original item" } ] ',
    )

    @contextlib.asynccontextmanager
    async def lease(org: str, namespace: str) -> AsyncIterator[None]:
        assert (org, namespace) == (source.org, source.namespace)
        yield

    get_source = AsyncMock(return_value=source if active else None)
    latest_source = AsyncMock(return_value=source)
    count_documents = AsyncMock(return_value=7)
    insert_source = AsyncMock()
    queue_retirement = AsyncMock(return_value=True)
    retire = AsyncMock()
    monkeypatch.setattr(app, "sources_lease", lease)
    monkeypatch.setattr(app, "sequential_reads", contextlib.nullcontext)
    monkeypatch.setattr(app.authz, "row_in_scope", AsyncMock(return_value=True))
    monkeypatch.setattr(app.store, "get_source", get_source)
    monkeypatch.setattr(app.store, "latest_source", latest_source)
    monkeypatch.setattr(app.store, "count_documents", count_documents)
    monkeypatch.setattr(app.store, "insert_source", insert_source)
    monkeypatch.setattr(app, "_queue_retirement", queue_retirement)
    monkeypatch.setattr(source_push, "retire", retire)
    output = asyncio.run(
        app.handle_knowledge_source_remove(
            KnowledgeSourceRemoveInput(
                org=source.org,
                namespace=source.namespace,
                name=source.name,
                confirm=source.name,
                delete_documents=delete_documents,
            )
        )
    )
    insert_source.assert_awaited_once()
    retired = insert_source.await_args.args[0]
    assert retired == replace(
        source,
        active=False,
        credential_enc="",
        secret_enc="",
        pending_delete=delete_documents or access == domain.MIRRORED_ACCESS,
    )
    retire.assert_awaited_once_with(source)
    if access == domain.MIRRORED_ACCESS:
        queue_retirement.assert_awaited_once_with(source)
        count_documents.assert_not_awaited()
        assert output.documents_pending == 0
    else:
        queue_retirement.assert_not_awaited()
        if delete_documents:
            count_documents.assert_awaited_once_with(
                source.org, source.namespace, domain.source_tag(source.name)
            )
        else:
            count_documents.assert_not_awaited()
        assert output.documents_pending == (7 if delete_documents else 0)
    if active:
        latest_source.assert_not_awaited()
    else:
        latest_source.assert_awaited_once_with(source.org, source.namespace, source.name)
