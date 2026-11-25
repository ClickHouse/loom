"""Own-account availability and source-list ownership without provider requests."""

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from server.core.errors import AuthInsufficientError, ValidationError
from server.knowledge.api.models_connections import ConnectionListInput
from server.knowledge.api.models_sources import KnowledgeSourceListInput, KnowledgeSourceSync
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import connections


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(accounts.settings, "public_base_url", "https://app.example")
    monkeypatch.setattr(accounts.settings, "connect_notion_read_content_only", True)
    for kind in ("gdrive", "notion", "linear"):
        monkeypatch.setattr(accounts.settings, f"connect_{kind}_client_id", "private-client-id")
        monkeypatch.setattr(accounts.settings, f"connect_{kind}_client_secret", "private-secret")


@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear"])
@pytest.mark.parametrize("missing", ["", "client_id", "client_secret"])
def test_consent_availability_checks_each_provider_registration(
    configured: None, monkeypatch: pytest.MonkeyPatch, kind: str, missing: str
) -> None:
    if missing:
        monkeypatch.setattr(accounts.settings, f"connect_{kind}_{missing}", "")
    assert connections._connect_available(kind) is (not missing)


@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear"])
def test_consent_availability_requires_a_valid_public_callback_base(
    configured: None, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(accounts.settings, "public_base_url", "http://app.example")
    assert not connections._connect_available(kind)


def test_notion_consent_requires_operator_read_content_attestation(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(accounts.settings, "connect_notion_read_content_only", False)
    assert not connections._connect_available("notion")
    assert connections._connect_available("gdrive")
    assert connections._connect_available("linear")


async def test_list_reports_configuration_separately_from_own_connection_status(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def own_subject(_auth: Any) -> tuple[str, str]:
        return "team", "person"

    async def owned(subjects: list[str], *, org: str) -> list[Any]:
        assert subjects == ["person"] and org == "team"
        return []

    monkeypatch.setattr(accounts, "own_subject", own_subject)
    monkeypatch.setattr(connections.store, "owned", owned)
    monkeypatch.setattr(accounts.settings, "connect_linear_client_secret", "")
    result = await connections.handle_connection_list(ConnectionListInput(org="team"))
    assert [
        (entry.kind, entry.connected, entry.connect_available) for entry in result.connections
    ] == [
        ("gdrive", False, True),
        ("linear", False, False),
        ("notion", False, True),
    ]
    wire = result.model_dump_json()
    assert "private-client-id" not in wire and "private-secret" not in wire
    assert all(entry.account_email is None and entry.scopes == [] for entry in result.connections)


@pytest.mark.parametrize("denied", ["identity", "organization"])
async def test_list_authorizes_before_reading_provider_configuration(
    monkeypatch: pytest.MonkeyPatch, denied: str
) -> None:
    async def own_subject(_auth: Any) -> tuple[str, str]:
        if denied == "identity":
            raise AuthInsufficientError("signed-in session required")
        return "other", "person"

    def unexpected(_kind: str) -> bool:
        raise AssertionError("configuration read before authorization")

    monkeypatch.setattr(accounts, "own_subject", own_subject)
    monkeypatch.setattr(connections, "_connect_available", unexpected)
    with pytest.raises((AuthInsufficientError, ValidationError)):
        await connections.handle_connection_list(ConnectionListInput(org="team"))


def test_unexpected_configuration_validation_is_not_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    def client(_kind: str) -> Any:
        raise ValidationError("unexpected validation")

    monkeypatch.setattr(accounts, "client", client)
    with pytest.raises(ValidationError, match="unexpected validation"):
        connections._connect_available("gdrive")


@pytest.fixture
def normal_source_list(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, SimpleNamespace]:
    from server.knowledge.app import sources

    source = SimpleNamespace(
        org="team",
        namespace="docs",
        name="selected",
        kind="gdrive",
        credential_mode="connected",
        access="mirrored",
        owner_subject="canonical-person",
        items=[{"type": "file", "id": "selected-file"}],
        created_by="private-creator",
        created_at=None,
    )

    async def listed(org: str, namespace: str) -> list[Any]:
        assert (org, namespace) == ("team", "docs")
        return [source]

    async def states(org: str, namespace: str, names: list[str]) -> dict[str, Any]:
        assert (org, namespace, names) == ("team", "docs", ["selected"])
        return {}

    async def refusal(org: str, namespace: str) -> None:
        assert (org, namespace) == ("team", "docs")

    monkeypatch.setattr(sources.store, "list_sources", listed)
    monkeypatch.setattr(sources.store, "read_states", states)
    monkeypatch.setattr(sources, "_source_refusal", refusal)
    monkeypatch.setattr(sources.ch, "set_query_tag", lambda _tag: None)
    monkeypatch.setattr(sources, "_answered_include", lambda _source: ["pdf"])
    monkeypatch.setattr(sources.domain, "id_list_config", lambda _source: {"selected": "file"})
    monkeypatch.setattr(sources, "_iso", lambda _value: "")
    monkeypatch.setattr(sources, "_sync", lambda _source, _state: KnowledgeSourceSync(status="ok"))
    # These tests exercise the predicate's decisions, not private-work authority admission.
    monkeypatch.setattr(sources, "private_work", nullcontext)
    monkeypatch.setattr(sources.request_work, "mark_mirrored", lambda: True)
    return sources, source


@pytest.mark.parametrize("subject", ["canonical-person", "person-alias"])
async def test_normal_source_list_uses_verified_canonical_ownership_for_aliases(
    normal_source_list: tuple[Any, SimpleNamespace], monkeypatch: pytest.MonkeyPatch, subject: str
) -> None:
    from server.knowledge.store import connected_accounts as account_store

    sources, source = normal_source_list
    caller = SimpleNamespace(org="team", subject=subject)
    account = object()
    checked = []

    async def canonical(auth: Any) -> tuple[str, str]:
        assert auth is caller
        return "team", "canonical-person"

    async def get(org: str, owner: str, kind: str) -> Any:
        assert (org, owner, kind) == ("team", "canonical-person", "gdrive")
        return account

    async def standing(found: Any, connected: Any) -> None:
        assert found is source and connected is account
        checked.append(True)

    monkeypatch.setattr(sources, "current_auth", lambda: caller)
    monkeypatch.setattr(accounts, "own_subject", canonical)
    monkeypatch.setattr(account_store, "get", get)
    monkeypatch.setattr(accounts, "_source_standing", standing)
    result = await sources.handle_knowledge_source_list(
        KnowledgeSourceListInput(org="team", namespace="docs")
    )
    entry = result.model_dump()["sources"][0]
    assert entry["owned_by_caller"] is True
    assert entry["owner_subject"] == "canonical-person"
    assert entry["items"] == [{"type": "file", "id": "selected-file"}]
    assert entry["sync"]["status"] == "ok"
    assert checked == [True]


@pytest.mark.parametrize("refused", ["foreign", "other-org", "missing", "standing", "unmarked"])
async def test_normal_source_list_never_marks_redacted_connected_details_as_owned(
    normal_source_list: tuple[Any, SimpleNamespace], monkeypatch: pytest.MonkeyPatch, refused: str
) -> None:
    from server.knowledge.store import connected_accounts as account_store

    sources, _source = normal_source_list
    canonical_reads = []
    account_reads = []
    standing_reads = []

    async def canonical(_auth: Any) -> tuple[str, str]:
        canonical_reads.append(True)
        if refused == "unmarked":
            raise AssertionError("unmarked work must not read account authority")
        return (
            "other-team" if refused == "other-org" else "team",
            "other-person" if refused == "foreign" else "canonical-person",
        )

    async def get(org: str, owner: str, kind: str) -> Any:
        account_reads.append((org, owner, kind))
        assert refused in {"missing", "standing"}
        assert (org, owner, kind) == ("team", "canonical-person", "gdrive")
        return None if refused == "missing" else object()

    async def standing(_source: Any, _account: Any) -> None:
        standing_reads.append(True)
        assert refused == "standing"
        raise ValidationError("standing cannot be verified")

    monkeypatch.setattr(accounts, "own_subject", canonical)
    monkeypatch.setattr(account_store, "get", get)
    monkeypatch.setattr(accounts, "_source_standing", standing)
    monkeypatch.setattr(sources.request_work, "mark_mirrored", lambda: refused != "unmarked")
    result = await sources.handle_knowledge_source_list(
        KnowledgeSourceListInput(org="team", namespace="docs")
    )
    entry = result.model_dump()["sources"][0]
    assert entry["owned_by_caller"] is False
    assert entry["owner_subject"] == entry["created_by"] == ""
    assert entry["items"] == entry["include"] == []
    assert entry["config"] == {}
    assert entry["sync"]["status"] == ""
    assert canonical_reads == ([] if refused == "unmarked" else [True])
    assert account_reads == (
        [("team", "canonical-person", "gdrive")] if refused in {"missing", "standing"} else []
    )
    assert standing_reads == ([True] if refused == "standing" else [])


async def test_normal_source_list_shared_details_do_not_mean_a_personal_connection_is_owned(
    normal_source_list: tuple[Any, SimpleNamespace], monkeypatch: pytest.MonkeyPatch
) -> None:
    sources, source = normal_source_list
    source.credential_mode = "shared"
    source.access = "namespace"
    source.owner_subject = ""

    async def no_personal_account(_auth: Any) -> tuple[str, str]:
        raise AssertionError("shared details do not resolve personal account ownership")

    monkeypatch.setattr(accounts, "own_subject", no_personal_account)
    result = await sources.handle_knowledge_source_list(
        KnowledgeSourceListInput(org="team", namespace="docs")
    )
    entry = result.model_dump()["sources"][0]
    assert entry["owned_by_caller"] is False
    assert entry["config"] == {"selected": "file"}
    assert entry["created_by"] == "private-creator"
    assert entry["sync"]["status"] == "ok"
