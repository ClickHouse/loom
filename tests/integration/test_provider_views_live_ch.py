"""A rebuilt view routes new inserts from the replacement Atomic table."""

import os
from uuid import UUID, uuid4

import pytest

from server.core.repo_base import _default_client
from server.insights.domain.provider_views import RebuildView
from server.insights.store.provider_views import capture_rebuild_views, rebind_rebuild_views


@pytest.mark.asyncio
async def test_rebound_view_tracks_new_source_after_atomic_rename():
    if os.environ.get("LOOM_PROVIDER_STAGING_LIVE_TEST") != "1":
        pytest.skip("Set LOOM_PROVIDER_STAGING_LIVE_TEST=1 for a disposable migrated database")
    ch = _default_client()
    await ch.connect()
    prefix = f"_provider_view_test_{uuid4().hex}"
    source, replacement, backup, destination, view = (
        f"{prefix}_{suffix}"
        for suffix in ("source", "replacement", "backup", "destination", "view")
    )
    owned = set()
    new_view = uuid4()
    try:
        captured = await capture_rebuild_views(ch=ch)
        assert {"chunk_term_freq_mv", "code_doc_stats_mv", "code_term_freq_mv"} <= {
            plan.name for plan in captured
        }
        for name in (source, replacement, destination):
            await ch.execute(f"CREATE TABLE loom.{name} (org String) ENGINE=MergeTree ORDER BY org")
            row = await ch.query_one(
                "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                {"name": name},
            )
            owned.add(UUID(str(row["uuid"])))
        await ch.execute(
            f"CREATE MATERIALIZED VIEW loom.{view} TO loom.{destination} "
            f"AS SELECT org FROM loom.{source}"
        )
        row = await ch.query_one(
            "SELECT uuid, create_table_query FROM system.tables WHERE database='loom' "
            "AND name={name:String}",
            {"name": view},
        )
        original = UUID(str(row["uuid"]))
        owned.update((original, new_view))
        plan = RebuildView(view, original, new_view, row["create_table_query"])
        await ch.execute(f"RENAME TABLE loom.{source} TO loom.{backup}")
        await ch.execute(f"RENAME TABLE loom.{replacement} TO loom.{source}")
        await rebind_rebuild_views((plan,), ch=ch)
        await ch.execute(f"INSERT INTO loom.{source} VALUES ('after-rebuild')")
        result = await ch.query_all(f"SELECT org FROM loom.{destination}")
        assert result == [{"org": "after-rebuild"}]
        await rebind_rebuild_views((plan,), ch=ch)  # Lost CREATE response is idempotent.
        await ch.execute(f"DROP TABLE loom.{backup} SYNC")  # No retained source dependency.
    finally:
        try:
            # Drop the view first; never infer cleanup ownership from a prefix.
            for name in (view, source, replacement, backup, destination):
                row = await ch.query_one(
                    "SELECT uuid FROM system.tables WHERE database='loom' AND name={name:String}",
                    {"name": name},
                )
                if row and UUID(str(row["uuid"])) in owned:
                    await ch.execute(f"DROP TABLE loom.{name} SYNC")
        finally:
            await ch.disconnect()
