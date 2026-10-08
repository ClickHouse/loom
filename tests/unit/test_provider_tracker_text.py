"""Hydration never crosses source identities or embeds text under a stale digest."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest

from server.insights.app import provider_tracker_text as hydration


def row(**changes):
    return {
        "org": "org-a",
        "namespace": "tracker",
        "kind": "issue",
        "number": 7,
        "source_id": "owner/repo#7",
        "tombstoned_at": None,
        "text_sha256": hydration.text_sha256("Title\nBody"),
        **changes,
    }


def item(**changes):
    return {
        "kind": "issue",
        "number": 7,
        "source_id": "owner/repo#7",
        "title": " Title ",
        "body": "Body",
        **changes,
    }


@pytest.fixture
def source(monkeypatch):
    source = Mock()
    source.embed_items_by_number = AsyncMock(return_value=[item()])
    factory = Mock(return_value=source)
    monkeypatch.setattr(hydration, "tracker_source", factory)
    return source, factory


@pytest.mark.asyncio
async def test_hydration_uses_existing_recipe_and_namespace_gate(source):
    source, factory = source
    assert await hydration.hydrate_tracker_page([row()], body_chars=100) == {0: "Title\nBody"}
    factory.assert_called_once_with("org-a", "tracker")
    source.embed_items_by_number.assert_awaited_once_with(
        "org-a", "tracker", kind="issue", numbers=[7], body_chars=100
    )


@pytest.mark.asyncio
async def test_tenant_and_kind_groups_are_not_combined(source):
    source, factory = source
    source.embed_items_by_number.side_effect = [[item()], [item(kind="pr")], [item()]]
    rows = [row(), row(kind="pr"), row(org="org-b")]
    assert await hydration.hydrate_tracker_page(rows, body_chars=100) == dict.fromkeys(
        range(3), "Title\nBody"
    )
    assert [call.args for call in factory.call_args_list] == [
        ("org-a", "tracker"),
        ("org-a", "tracker"),
        ("org-b", "tracker"),
    ]


@pytest.mark.asyncio
async def test_same_number_in_another_repository_is_not_a_match(source):
    source, _ = source
    source.embed_items_by_number.return_value = [item(source_id="other/repo#7")]
    with pytest.raises(ValueError, match="tracker_source_missing"):
        await hydration.hydrate_tracker_page([row()], body_chars=100)


@pytest.mark.asyncio
async def test_changed_text_never_reuses_the_old_digest(source):
    source, _ = source
    source.embed_items_by_number.return_value = [item(body="Changed")]
    with pytest.raises(ValueError, match="tracker_source_changed"):
        await hydration.hydrate_tracker_page([row()], body_chars=100)


@pytest.mark.asyncio
async def test_reconciliation_pairs_new_text_with_new_digest_without_mutating_source(source):
    source, _ = source
    source.embed_items_by_number.return_value = [item(body="Changed")]
    original = row()
    refreshed, texts = await hydration.reconcile_tracker_page([original], body_chars=100)
    assert texts == {0: "Title\nChanged"}
    assert refreshed[0]["text_sha256"] == hydration.text_sha256(texts[0])
    assert original["text_sha256"] == hydration.text_sha256("Title\nBody")
    assert {k: v for k, v in refreshed[0].items() if k != "text_sha256"} == {
        k: v for k, v in original.items() if k != "text_sha256"
    }


@pytest.mark.asyncio
async def test_duplicate_source_identity_is_ambiguous(source):
    source, _ = source
    source.embed_items_by_number.return_value = [item(), item()]
    with pytest.raises(ValueError, match="tracker_source_ambiguous"):
        await hydration.hydrate_tracker_page([row()], body_chars=100)


@pytest.mark.asyncio
async def test_source_errors_do_not_expose_credentials(source):
    source, _ = source
    source.embed_items_by_number.side_effect = RuntimeError("credential-secret source-content")
    with pytest.raises(ValueError) as error:
        await hydration.hydrate_tracker_page([row()], body_chars=100)
    assert str(error.value) == "tracker_source_unavailable"
    assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_tombstones_need_no_source_even_if_identity_is_missing(source):
    source, factory = source
    assert (
        await hydration.hydrate_tracker_page([{"tombstoned_at": datetime.now(UTC)}], body_chars=100)
        == {}
    )
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"org": ""},
        {"namespace": ""},
        {"source_id": ""},
        {"kind": "unknown"},
        {"number": True},
        {"number": 0},
    ],
)
async def test_invalid_source_identity_is_rejected_before_any_read(source, changes):
    _, factory = source
    with pytest.raises(ValueError, match="identity_invalid"):
        await hydration.hydrate_tracker_page([row(**changes)], body_chars=100)
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_page_and_recipe_bounds_are_enforced(source):
    _, factory = source
    with pytest.raises(ValueError, match="bounds_invalid"):
        await hydration.hydrate_tracker_page([row()] * 65, body_chars=100)
    with pytest.raises(ValueError, match="bounds_invalid"):
        await hydration.hydrate_tracker_page([row()], body_chars=True)
    factory.assert_not_called()
