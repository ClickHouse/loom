"""Retired Meet recording sources refuse work after authorization."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any
from unittest.mock import Mock

import pytest

from server.core.errors import AuthInsufficientError, ValidationError
from server.knowledge.api.models_sources import (
    KnowledgeSourceCheckInput,
    KnowledgeSourceRotateInput,
    KnowledgeSourceSyncNowInput,
    KnowledgeSourceUpdateInput,
)
from server.knowledge.app import sources as app
from server.knowledge.domain.sources import KnowledgeSource

_OPERATIONS = ("check", "rotate", "sync_now", "update")
_MESSAGE = "Meet transcripts and notes use a gdrive shared-folder source"


@dataclasses.dataclass
class _Boundary:
    source: KnowledgeSource
    order: list[str]
    downstream: dict[str, Mock]


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> _Boundary:
    state = _Boundary(
        KnowledgeSource(
            org="acme",
            namespace="team",
            name="legacy-meet",
            kind="recording",
            source_id="00000000-0000-4000-8000-000000000001",
            config={"provider": "meet", "ids": ["legacy-recording"]},
        ),
        [],
        {},
    )

    async def source(org: str, namespace: str, name: str) -> KnowledgeSource:
        assert (org, namespace, name) == ("acme", "team", "legacy-meet")
        state.order.append("source")
        return state.source

    async def admin(*args: Any) -> bool:
        assert args == ("acme", "team", app.Role.ADMIN)
        state.order.append("admin")
        return True

    def served(src: KnowledgeSource) -> None:
        assert src is state.source
        state.order.append("served")

    monkeypatch.setattr(app, "_source_or_404", source)
    monkeypatch.setattr(app.authz, "row_in_scope", admin)
    monkeypatch.setattr(app, "_require_served", served)
    for name in (
        "credential_of",
        "_now_iso",
        "_require_sealable",
        "_require_source_namespace",
        "sources_lease",
        "_update_plan",
        "_read_state",
        "_update_state",
    ):
        spy = Mock(side_effect=AssertionError(f"unexpected {name}"))
        state.downstream[name] = spy
        monkeypatch.setattr(app, name, spy)
    for module, name in (
        (app.source_probes, "sample"),
        (app.source_probes, "probe"),
        (app.store, "insert_source"),
    ):
        spy = Mock(side_effect=AssertionError(f"unexpected {name}"))
        state.downstream[name] = spy
        monkeypatch.setattr(module, name, spy)
    return state


def _call(operation: str) -> Any:
    values: dict[str, Any] = {"org": "acme", "namespace": "team", "name": "legacy-meet"}
    models = {
        "check": KnowledgeSourceCheckInput,
        "rotate": KnowledgeSourceRotateInput,
        "sync_now": KnowledgeSourceSyncNowInput,
        "update": KnowledgeSourceUpdateInput,
    }
    if operation == "rotate":
        values["credential"] = "unused-credential"
    if operation == "update":
        values["include"] = ["pdf"]
    return getattr(app, f"handle_knowledge_source_{operation}")(models[operation](**values))


def _no_downstream_work(boundary: _Boundary) -> None:
    for spy in boundary.downstream.values():
        spy.assert_not_called()


@pytest.mark.parametrize("operation", _OPERATIONS)
def test_legacy_meet_refuses_before_credentials_probes_or_writes(
    boundary: _Boundary, operation: str
) -> None:
    before = dataclasses.asdict(boundary.source)
    with pytest.raises(ValidationError, match=_MESSAGE) as error:
        asyncio.run(_call(operation))
    assert error.value.to_detail()["code"] == "BAD_CONFIG"
    assert boundary.order == (
        ["source", "served"] if operation == "rotate" else ["source", "admin", "served"]
    )
    assert dataclasses.asdict(boundary.source) == before
    _no_downstream_work(boundary)


@pytest.mark.parametrize("operation", _OPERATIONS)
def test_source_authorization_precedes_legacy_provider_refusal(
    boundary: _Boundary, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    async def denied(*args: Any) -> KnowledgeSource:
        boundary.order.append("source")
        raise AuthInsufficientError("source access denied")

    monkeypatch.setattr(app, "_source_or_404", denied)
    with pytest.raises(AuthInsufficientError, match="source access denied"):
        asyncio.run(_call(operation))
    assert boundary.order == ["source"]
    _no_downstream_work(boundary)


@pytest.mark.parametrize("operation", ("check", "update", "sync_now"))
def test_shared_admin_authorization_precedes_legacy_provider_refusal(
    boundary: _Boundary, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    async def denied(*args: Any) -> bool:
        boundary.order.append("admin")
        return False

    monkeypatch.setattr(app.authz, "row_in_scope", denied)
    with pytest.raises(AuthInsufficientError, match="namespace administrator"):
        asyncio.run(_call(operation))
    assert boundary.order == ["source", "admin"]
    _no_downstream_work(boundary)


@pytest.mark.parametrize("operation", _OPERATIONS)
def test_serving_authorization_precedes_legacy_provider_refusal(
    boundary: _Boundary, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    def denied(src: KnowledgeSource) -> None:
        boundary.order.append("served")
        raise AuthInsufficientError("source is not served")

    monkeypatch.setattr(app, "_require_served", denied)
    with pytest.raises(AuthInsufficientError, match="source is not served"):
        asyncio.run(_call(operation))
    assert boundary.order == (
        ["source", "served"] if operation == "rotate" else ["source", "admin", "served"]
    )
    _no_downstream_work(boundary)


@pytest.mark.parametrize(
    ("operation", "kind", "provider", "next_step"),
    [
        ("check", "recording", "zoom", "_now_iso"),
        ("check", "gdrive", "meet", "_now_iso"),
        ("rotate", "recording", "zoom", "_require_sealable"),
        ("rotate", "gdrive", "meet", "_require_sealable"),
        ("sync_now", "recording", "zoom", "_require_source_namespace"),
        ("sync_now", "gdrive", "meet", "_require_source_namespace"),
        ("update", "gdrive", "meet", "_update_plan"),
    ],
)
def test_other_sources_reach_the_existing_handler_work(
    boundary: _Boundary, operation: str, kind: str, provider: str, next_step: str
) -> None:
    boundary.source = dataclasses.replace(boundary.source, kind=kind, config={"provider": provider})
    with pytest.raises(AssertionError, match=f"unexpected {next_step}"):
        asyncio.run(_call(operation))
    boundary.downstream[next_step].assert_called_once()


def test_other_recording_updates_keep_the_existing_bad_kind_refusal(boundary: _Boundary) -> None:
    boundary.source = dataclasses.replace(boundary.source, config={"provider": "zoom"})
    with pytest.raises(ValidationError) as error:
        asyncio.run(_call("update"))
    assert error.value.to_detail()["code"] == "BAD_KIND"
    _no_downstream_work(boundary)
