"""An older reader cannot admit a source format it only preserves for cleanup."""

from types import SimpleNamespace
from typing import Any

import pytest

from server.knowledge.app import connected_accounts as accounts
from server.knowledge.app import source_access
from server.knowledge.domain.sources import KnowledgeSource


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "mode", "version", "names"),
    [
        ("mcp", "mcp", 1, []),
        ("mcp", "shared", 0, []),
        ("gdrive", "shared", 2, []),
        ("gdrive", "connected", 2, []),
        ("gdrive", "shared", 0, ["collection-one"]),
    ],
)
async def test_unsupported_format_never_enters_a_native_read_plan(
    monkeypatch: pytest.MonkeyPatch, kind: str, mode: str, version: int, names: list[str]
) -> None:
    async def mirrors(org: str, selected: list[str], **kwargs: Any) -> dict[str, str]:
        assert org == "org-one"
        assert selected == names
        return {}

    async def listings(*args: Any) -> list[Any]:
        return []

    async def forbid_native_read(*args: Any) -> None:
        pytest.fail("an unsupported format entered native source authorization")

    from server.knowledge.store import sources

    monkeypatch.setattr(sources, "latest_source", forbid_native_read)
    monkeypatch.setattr(source_access.mirrors_repo, "mirrors_of", mirrors)
    monkeypatch.setattr(source_access.store, "listings", listings)
    plan = SimpleNamespace(org="org-one", _connected_pins={}, _scope_frozen=False, _auth=object())
    await source_access._prepare(
        plan,
        [
            {
                "namespace": "collection-one",
                "name": "selected-documents",
                "source_id": "source-one",
                "source_kind": kind,
                "credential_mode": mode,
                "source_contract_version": version,
            }
        ],
        "",
    )
    assert plan.sources == {}
    assert plan.listing_starts == {}


@pytest.mark.asyncio
async def test_a_format_change_after_listing_cannot_enter_native_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.store import sources

    async def changed(*args: Any) -> KnowledgeSource:
        return KnowledgeSource(
            org="org-one",
            namespace="collection-one",
            name="selected-documents",
            source_id="source-one",
            kind="gdrive",
            credential_mode="connected",
            source_contract_version=2,
        )

    async def forbid_native_read(*args: Any) -> None:
        pytest.fail("a changed format entered native source authorization")

    async def mirrors(org: str, names: list[str], **kwargs: Any) -> dict[str, str]:
        assert names == []
        return {}

    async def listings(*args: Any) -> list[Any]:
        return []

    monkeypatch.setattr(sources, "latest_source", changed)
    monkeypatch.setattr(accounts, "validate_indexed", forbid_native_read)
    monkeypatch.setattr(source_access.mirrors_repo, "mirrors_of", mirrors)
    monkeypatch.setattr(source_access.store, "listings", listings)
    plan = SimpleNamespace(org="org-one", _connected_pins={}, _scope_frozen=False, _auth=object())
    await source_access._prepare(
        plan,
        [
            {
                "namespace": "collection-one",
                "name": "selected-documents",
                "source_id": "source-one",
                "source_kind": "gdrive",
                "credential_mode": "connected",
                "source_contract_version": 0,
            }
        ],
        "",
    )
    assert plan.sources == {}


def native_connection_source(version: int) -> KnowledgeSource:
    return KnowledgeSource(
        org="org-one",
        namespace="collection-one",
        name="selected-documents",
        source_id="source-one",
        kind="gdrive",
        credential_mode="connected",
        source_contract_version=version,
        access="mirrored",
        owner_subject="owner-one",
        connection_id="c" * 32 + "/1" + "d" * 31,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_after_check", [False, True])
async def test_native_connection_standing_refuses_unknown_format_before_checkout(
    monkeypatch: pytest.MonkeyPatch, changed_after_check: bool
) -> None:
    from server.core.errors import ValidationError
    from server.knowledge.app import sources as source_ops

    original = native_connection_source(0 if changed_after_check else 2)
    account = SimpleNamespace(
        org=original.org,
        subject=original.owner_subject,
        kind=original.kind,
        connection_id=original.connection_id,
        active=True,
        credential_enc="sealed-test-credential",
        connected_at=None,
    )

    async def fail_if_reached(*args: Any) -> None:
        pytest.fail("an unknown source format passed native connection standing")

    async def allow(*args: Any) -> None:
        return None

    async def writer(*args: Any) -> Any:
        return accounts.Role.WRITER

    async def changed(*args: Any) -> KnowledgeSource:
        return native_connection_source(2)

    monkeypatch.setattr(
        accounts,
        "settings",
        SimpleNamespace(
            auth_actually_required=lambda: True, knowledge_mirrored_sources_enabled=True
        ),
    )
    monkeypatch.setattr(
        accounts, "_require_source_fence", allow if changed_after_check else fail_if_reached
    )
    monkeypatch.setattr(accounts, "_standing", allow)
    monkeypatch.setattr(accounts.aliases, "verified_spellings", allow)
    monkeypatch.setattr(accounts.aliases, "invalidate", lambda *args: None)
    monkeypatch.setattr(accounts.authz, "grant_role_for", writer)
    monkeypatch.setattr(accounts.source_store, "latest_source", changed)
    monkeypatch.setattr(source_ops, "_require_source_namespace", fail_if_reached)
    with pytest.raises(ValidationError, match="the connected account is unavailable"):
        await accounts._source_standing(original, account)
