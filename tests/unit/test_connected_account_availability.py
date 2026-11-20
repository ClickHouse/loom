"""Consent availability reflects deployment configuration without provider requests."""

from typing import Any

import pytest

from server.core.errors import AuthInsufficientError, ValidationError
from server.knowledge.api.models_connections import ConnectionListInput
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
