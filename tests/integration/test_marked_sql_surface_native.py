"""A real scoped SQL account sees masked events and namespace-day private spend."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from server.cli.sql_access import _login_check
from server.core.ch import CHClient
from server.core.ch_writes import async_insert_settings
from server.insights.app import sql_access
from server.insights.domain.sql_surface import Scope
from server.observability.events import AgentEvent
from tests.integration._event_fixtures import insert_event_rows
from tests.integration._resources import ch_client_from_env, unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.mark.parametrize("private_success", [False, True])
async def test_sql_account_masks_before_predicates_and_folds_before_fine_dimensions(
    private_success: bool,
) -> None:
    client = CHClient()
    reader = None
    attempted = False
    user = "it_marked_sql_" + uuid4().hex[:12]
    org, foreign = unique_org("marked-sql"), unique_org("marked-sql-foreign")
    stamp = datetime.combine(datetime.now(UTC).date(), datetime.min.time(), UTC)
    stamp -= timedelta(hours=12)
    ordinary = AgentEvent(
        ts=stamp,
        org=org,
        namespace="notes",
        agent="ordinary",
        user_id="reader",
        session_id="session",
        operation="memory.ask",
        request_id=uuid4().hex,
        latency_ms=7,
        success=True,
        status_class="ok",
        input_tokens=3,
        output_tokens=2,
        embedding_tokens=4,
        upstream_cost_usd=0.25,
        bytes_written=9,
        model="ordinary",
        llm_calls=1,
    ).to_row()
    placeholder = {
        **ordinary,
        "request_id": uuid4().hex,
        "agent": "-",
        "operation": "-",
        "kind": "-",
        "latency_ms": 11,
        "upstream_cost_usd": 0.5,
    }
    private = {
        **ordinary,
        "request_id": uuid4().hex,
        "mirror_marked": 1,
        "agent": "private",
        "operation": "private.allow" if private_success else "private.deny",
        "kind": "private-kind",
        "success": private_success,
        "error_code": "PRIVATE_OUTCOME",
        "status_class": "ok" if private_success else "server",
        "latency_ms": 999999,
        "input_tokens": 13,
        "output_tokens": 17,
        "embedding_tokens": 19,
        "upstream_cost_usd": 2.5,
        "bytes_written": 999,
        "model": "private-model",
        "llm_calls": 111,
    }
    second = {
        **private,
        "request_id": uuid4().hex,
        "agent": "private-other",
        "upstream_cost_usd": 3.5,
    }
    try:
        await client.connect()
        await insert_event_rows(
            client,
            "loom.agent_events",
            [
                ordinary,
                placeholder,
                private,
                second,
                {**private, "request_id": uuid4().hex, "org": foreign},
                {**private, "request_id": uuid4().hex, "namespace": "other"},
                {**private, "request_id": uuid4().hex, "namespace": "acl-" + "a" * 24},
                {**private, "request_id": uuid4().hex, "ts": stamp - timedelta(days=62)},
            ],
            async_insert_settings(),
            required_columns={"mirror_marked"},
        )
        attempted = True
        granted = await sql_access.grant(
            user, Scope(org, ("notes",)), login_check=_login_check, ch=client
        )
        assert granted.password
        reader = ch_client_from_env(username=user, password=granted.password)
        columns = (
            "agent, latency_ms, success, error_code, status_class, input_tokens, output_tokens, "
            "embedding_tokens, upstream_cost_usd, bytes_written, model, llm_calls"
        )
        events = reader.query(f"SELECT {columns} FROM loom.sql_v1_agent_events").named_results()
        rows = list(events)
        assert len(rows) == 4
        assert {row["agent"] for row in rows} == {"ordinary", "-", "private", "private-other"}
        for row in rows:
            if row["agent"].startswith("private"):
                assert row == {
                    "agent": row["agent"],
                    "latency_ms": 0,
                    "success": False,
                    "error_code": "",
                    "status_class": "",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "embedding_tokens": 0,
                    "upstream_cost_usd": 0,
                    "bytes_written": 0,
                    "model": "",
                    "llm_calls": 0,
                }
        ordinary_row = next(row for row in rows if row["agent"] == "ordinary")
        assert ordinary_row["latency_ms"] == 7 and ordinary_row["upstream_cost_usd"] == 0.25
        assert ordinary_row["model"] == "ordinary" and ordinary_row["llm_calls"] == 1
        probe = (
            "SELECT count() FROM loom.sql_v1_agent_events "
            "WHERE latency_ms > 100 OR upstream_cost_usd > 1 OR model = 'private-model'"
        )
        assert reader.query(probe).result_rows == [(0,)]
        usage = list(reader.query("SELECT * FROM loom.sql_v1_usage_daily").named_results())
        assert len(usage) == 2
        folded = next(row for row in usage if row["agent"] == "-")
        assert (folded["operation"], folded["kind"]) == ("-", "-")
        assert folded["ops"] == 3 and folded["upstream_cost_usd"] == 6.5
        assert (folded["input_tokens"], folded["output_tokens"], folded["embedding_tokens"]) == (
            29,
            36,
            42,
        )
        assert folded["errors"] == 0 and folded["llm_calls"] == 1 and folded["bytes_written"] == 9
        assert (folded["latency_sum_ms"], folded["latency_p50_ms"], folded["latency_p95_ms"]) == (
            11,
            11,
            11,
        )
        assert (
            folded["day"] == stamp.date()
            and folded["org"] == org
            and folded["namespace"] == "notes"
        )
        assert next(row for row in usage if row["agent"] == "ordinary")["ops"] == 1
        assert reader.query(
            "SELECT sum(ops) FROM loom.sql_v1_usage_daily WHERE startsWith(operation, 'private.')"
        ).result_rows == [(0,)]
        with pytest.raises(Exception, match="ACCESS_DENIED|Not enough privileges"):
            reader.query("SELECT count() FROM loom.agent_events")

        await sql_access.grant(user, Scope(org, ("*",)), login_check=_login_check, ch=client)
        scopes = reader.query(
            "SELECT org, namespace, count() FROM loom.sql_v1_agent_events "
            "GROUP BY org, namespace ORDER BY namespace"
        ).result_rows
        assert scopes == [(org, "notes", 4), (org, "other", 1)]
    finally:
        try:
            if reader is not None:
                reader.close()
        finally:
            try:
                if attempted:
                    await sql_access.revoke(user, ch=client)
            finally:
                await client.disconnect()
