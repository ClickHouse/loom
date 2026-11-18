"""Native database logs stay privileged while application SQL capture omits private work."""

from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest

from tests.integration._resources import ch_client_from_env, unique_org


async def test_native_private_query_log_is_inaccessible_to_sql_and_analyst(
    control_plane_url, mint_test_token, monkeypatch, tmp_path
):
    os.environ.get("LOOM_CH_HOST") or pytest.skip("LOOM_CH_HOST not set")
    from server.cli.sql_access import _login_check
    from server.core.ch import get_client, set_query_tag
    from server.core.diagnostics import private_work
    from server.insights.app import sql_access
    from server.insights.domain.sql_surface import Scope
    from server.settings import settings

    org, namespace = unique_org("private-log"), "docs"
    token = mint_test_token(org=org, namespaces=[namespace], roles=["writer"])
    ch = get_client()
    set_query_tag("test.private_native_log_boundary")
    user = "it_docsrc_sql_" + uuid4().hex[:12]
    reader = None
    created = False
    try:
        await ch.connect()
        capture = tmp_path / "statements.jsonl"
        with monkeypatch.context() as patch:
            patch.setattr(settings, "ch_capture_statements", str(capture))
            query_id = "docsrc-private-log-" + uuid4().hex
            with private_work():
                value = await ch.raw_bytes(
                    "SELECT 'synthetic-private-code' FORMAT TabSeparated",
                    settings={"query_id": query_id},
                )
            assert value.strip() == b"synthetic-private-code"
            assert not capture.exists()
            await ch.query_one("SELECT 1 AS public_control")
            assert "public_control" in capture.read_text()
        await ch.execute("SYSTEM FLUSH LOGS")
        native = await ch.query_all(
            "SELECT query, result_rows FROM system.query_log "
            "WHERE query_id = {qid:String} AND type = 'QueryFinish' LIMIT 2",
            {"qid": query_id},
        )
        assert len(native) == 1 and native[0]["result_rows"] == 1
        assert "synthetic-private-code" in native[0]["query"]
        async with httpx.AsyncClient(
            base_url=control_plane_url, headers={"Authorization": "Bearer " + token}, timeout=60
        ) as http:
            written = await http.post(
                "/v1/memory.set",
                json={
                    "org": org,
                    "namespace": namespace,
                    "agent": "control",
                    "memory_key": "public-control",
                    "value": "synthetic control",
                },
            )
            assert written.status_code == 200, written.text
            positive = await http.post(
                "/v1/insights.query",
                json={
                    "org": org,
                    "namespace": namespace,
                    "sql": "SELECT memory_key FROM loom.memory",
                },
            )
            assert positive.status_code == 200, positive.text
            assert positive.json()["rows"] == [["public-control"]]
            granted = await sql_access.grant(
                user, Scope(org, (namespace,)), login_check=_login_check, ch=ch
            )
            created = True
            assert granted.password
            reader = ch_client_from_env(username=user, password=granted.password)
            assert reader.query("SELECT memory_key FROM loom.sql_v1_memories").result_rows == [
                ("public-control",)
            ]
            available = await ch.query_all(
                "SELECT name FROM system.tables WHERE database = 'system' AND name IN "
                "('query_log','text_log','error_log','trace_log','query_views_log',"
                "'query_thread_log','processors_profile_log','opentelemetry_span_log')"
            )
            assert "query_log" in {r["name"] for r in available}
            for row in available:
                table = row["name"]
                with pytest.raises(Exception, match="ACCESS_DENIED|Not enough privileges"):
                    reader.query(f"SELECT count() FROM system.{table}")
                refused = await http.post(
                    "/v1/insights.query",
                    json={
                        "org": org,
                        "namespace": namespace,
                        "sql": f"SELECT count() FROM system.{table}",
                    },
                )
                assert refused.status_code in {403, 422}, refused.text
                assert "synthetic-private-code" not in refused.text
    finally:
        if reader is not None:
            reader.close()
        if created:
            await sql_access.revoke(user, ch=ch)
        await ch.disconnect()
