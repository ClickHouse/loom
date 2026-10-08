"""View rebinding keeps future inserts attached to the replacement source UUID."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.insights.domain.provider_views import RebuildView
from server.insights.store.provider_views import capture_rebuild_views, rebind_rebuild_views


def plan():
    return RebuildView(
        "chunk_term_freq_mv",
        uuid4(),
        uuid4(),
        "CREATE MATERIALIZED VIEW loom.chunk_term_freq_mv TO loom.chunk_term_freq "
        "AS SELECT org FROM loom.chunks",
    )


def test_plan_preserves_select_and_explicit_destination_and_pins_new_uuid():
    view = plan()
    sql = view.creation_sql()
    assert sql.startswith(
        f"CREATE MATERIALIZED VIEW `loom`.`{view.name}` UUID '{view.replacement}' "
    )
    assert "TO loom.chunk_term_freq AS SELECT org FROM loom.chunks" in sql
    assert RebuildView.from_dict(view.to_dict()) == view


def test_implicit_inner_data_view_cannot_be_dropped():
    view = plan()
    unsafe = RebuildView(
        view.name,
        view.original,
        view.replacement,
        f"CREATE MATERIALIZED VIEW loom.{view.name} ENGINE=MergeTree "
        "ORDER BY tuple() AS SELECT org FROM loom.chunks",
    )
    with pytest.raises(ValueError, match="explicit-TO"):
        unsafe.creation_sql()


@pytest.mark.asyncio
async def test_rebind_preflights_uuid_before_drop_and_confirms_replacement():
    view = plan()
    ch = AsyncMock()
    ch.query_all.side_effect = [
        [{"name": view.name, "uuid": view.original}],
        [{"name": view.name, "uuid": view.replacement}],
    ]
    await rebind_rebuild_views((view,), ch=ch)
    assert [call.args[0] for call in ch.execute.await_args_list] == [
        f"DROP VIEW `loom`.`{view.name}` SYNC",
        view.creation_sql(),
    ]


@pytest.mark.asyncio
async def test_rebind_resumes_after_lost_drop_or_create_response():
    view = plan()
    ch = AsyncMock()
    ch.query_all.side_effect = [[], [{"name": view.name, "uuid": view.replacement}]]
    await rebind_rebuild_views((view,), ch=ch)
    ch.execute.assert_awaited_once_with(view.creation_sql())
    ch.execute.reset_mock()
    ch.query_all.side_effect = None
    ch.query_all.return_value = [{"name": view.name, "uuid": view.replacement}]
    await rebind_rebuild_views((view,), ch=ch)
    ch.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_view_identity_prevents_every_drop():
    view = plan()
    ch = AsyncMock()
    ch.query_all.return_value = [{"name": view.name, "uuid": uuid4()}]
    with pytest.raises(ValueError, match="durable rebuild"):
        await rebind_rebuild_views((view,), ch=ch)
    ch.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_follows_source_insert_dependencies_not_loading_dependencies():
    view = plan()
    ch = AsyncMock()
    ch.query_all.side_effect = [
        [{"dependencies_database": ["loom"], "dependencies_table": [view.name]}],
        [
            {
                "name": view.name,
                "uuid": view.original,
                "create_table_query": view.ddl,
                "engine": "MaterializedView",
            }
        ],
    ]
    captured = await capture_rebuild_views(ch=ch)
    assert len(captured) == 1 and captured[0].original == view.original
    assert captured[0].replacement != view.original
    assert "dependencies_table" in ch.query_all.await_args_list[0].args[0]
    assert "loading_dependencies" not in ch.query_all.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_cross_database_dependency_cannot_be_silently_left_attached_to_backup():
    ch = AsyncMock()
    ch.query_all.return_value = [
        {"dependencies_database": ["elsewhere"], "dependencies_table": ["foreign_view"]}
    ]
    with pytest.raises(ValueError, match="Cross-database"):
        await capture_rebuild_views(ch=ch)
    ch.query_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_view_metadata_is_not_an_empty_recipe():
    ch = AsyncMock()
    ch.query_all.side_effect = [
        [{"dependencies_database": ["loom"], "dependencies_table": ["missing_view"]}],
        [],
    ]
    with pytest.raises(ValueError, match="incomplete"):
        await capture_rebuild_views(ch=ch)
