"""Mirror billing reaches its own ledger without altering shared accounting."""

from __future__ import annotations

import os

import pytest

from tests.integration._resources import unique_org


async def test_private_costs_are_confined_to_the_mirror() -> None:
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.core import request_work
    from server.core.ch import get_client, set_query_tag
    from server.core.naming import new_mirror_name
    from server.knowledge.app import private_sync
    from server.observability import cost_accumulator, events

    client = get_client()
    org, mirror = unique_org("private-costs"), new_mirror_name()
    set_query_tag("test.private_source_accounting")
    before = events.get_emitter().stats()
    try:
        await client.connect()
        with cost_accumulator.isolated_op() as outer:
            cost_accumulator.report_embed("fake-ngram-1024", 2)
            bag = request_work.begin()
            with private_sync.scope():
                async with private_sync.mirror_costs(org, mirror, "knowledge_source_sync"):
                    cost_accumulator.report_embed("fake-ngram-1024", 17)
                    cost_accumulator.report_charge("fake-echo-1", 0.25)
                    request_work.note_ch_summary({"read_rows": 10000, "read_bytes": 90000})
            assert outer.embed_tokens == 2 and outer.cost_usd == 0.0
            assert bag.ch_statements == bag.ch_read_bytes == 0
        rows = await client.query_all(
            "SELECT * FROM loom.agent_events_private WHERE org = {org:String}",
            {"org": org},
        )
        assert len(rows) == 1
        row = rows[0]
        assert (row["org"], row["namespace"], row["agent"], row["user_id"], row["session_id"]) == (
            org,
            mirror,
            "-",
            "-",
            "-",
        )
        assert int(row["embedding_tokens"]) == 17
        assert int(row["llm_calls"]) == 1
        assert float(row["upstream_cost_usd"]) == 0.25
        assert row["operation"] == "worker.knowledge_source_sync"
        assert (
            await client.query_all(
                "SELECT * FROM loom.agent_events WHERE org = {org:String}", {"org": org}
            )
            == []
        )
        assert events.get_emitter().stats() == before
    finally:
        await client.disconnect()
