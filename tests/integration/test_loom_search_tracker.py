"""General tracker search uses the live contract and ordinary authorization gates."""

from __future__ import annotations

import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.integration._resources import ch_target, unique_org

pytestmark = pytest.mark.usefixtures("ch_target_required")


@pytest.fixture
async def tracker_record(ch_client: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from server.auth import keyring
    from server.auth.context import current_auth, set_current_auth
    from server.boot import _register_tracker_namespaces_once
    from server.core.ch import get_client, set_query_tag
    from server.settings import settings
    from server.tracker.domain.contract import REQUIRED
    from server.tracker.store.source import qa_intel

    monkeypatch.setattr(keyring, "_view", keyring._view)
    ch = get_client()
    await ch.connect()
    set_query_tag("test.tracker_search")
    suffix = uuid.uuid4().hex[:12]
    database, user = f"tracker_search_{suffix}", f"tracker_read_{suffix}"
    org = unique_org("tracker-search")
    standard, second, restricted = (f"tracker-{suffix}-{n}" for n in ("a", "b", "private"))
    password = secrets.token_hex(24)
    old_auth = current_auth()
    ch_client.command(f"CREATE DATABASE {database}")
    try:
        for table, fields in REQUIRED.items():
            columns = {
                name: "UInt64"
                if kind == "UInt"
                else "DateTime64(3)"
                if kind == "DateTime"
                else kind
                for name, kind in fields.items()
            }
            if table == "github_issues":
                columns["version_affected"] = "String"
            columns["version"] = "UInt64 DEFAULT 1"
            ddl = ", ".join(f"`{name}` {kind}" for name, kind in columns.items())
            key = "source_id" if table != "edges" else "(from_id, to_id, rel_type)"
            ch_client.command(
                f"CREATE TABLE {database}.{table} ({ddl}) ENGINE=ReplacingMergeTree(version) ORDER BY {key}"
            )
        ch_client.command(
            f"CREATE USER {user} IDENTIFIED BY {{password:String}} SETTINGS readonly=2",
            parameters={"password": password},
        )
        ch_client.command(f"GRANT SELECT ON {database}.* TO {user}")
        for table, rows in (
            (
                "github_issues",
                [
                    ("Acme/widgets#11", 11, "Retention decision", 0),
                    ("Acme/widgets#12", 12, "Retention private canary", 1),
                    ("Other/widgets#13", 13, "Retention other repository canary", 0),
                ],
            ),
            ("github_prs", [("Acme/widgets#21", 21, "Retention implementation", 0)]),
        ):
            ch_client.insert(
                f"{database}.{table}",
                [
                    [
                        uuid.uuid4(),
                        source_id,
                        number,
                        f"https://example.test/{'pull' if table == 'github_prs' else 'issues'}/{number}",
                        title,
                        "retention policy decision",
                        private,
                        datetime.now(UTC),
                    ]
                    for source_id, number, title, private in rows
                ],
                column_names=[
                    "id",
                    "source_id",
                    "number",
                    "url",
                    "title",
                    "body",
                    "is_private",
                    "created_at",
                ],
            )
        target = ch_target()
        monkeypatch.setattr(settings, "tracker_org", org)
        monkeypatch.setattr(settings, "tracker_live_serve", True)
        monkeypatch.setattr(settings, "tracker_self_service_allow_private_hosts", True)
        monkeypatch.setattr(
            settings,
            "tracker_sources_json",
            json.dumps(
                {
                    "sources": {
                        suffix: {
                            "host": target["host"],
                            "port": target["port"],
                            "secure": target["secure"],
                            "database": database,
                        }
                    },
                    "namespaces": {
                        ns: {
                            "source": suffix,
                            "repos": ["Acme/widgets"],
                            "restricted": ns == restricted,
                        }
                        for ns in (standard, second, restricted)
                    },
                }
            ),
        )
        monkeypatch.setattr(
            settings,
            "tracker_credentials_json",
            json.dumps({suffix: {"user": user, "password": password}}),
        )
        assert not await _register_tracker_namespaces_once(ch)
        yield {
            "org": org,
            "standard": standard,
            "second": second,
            "restricted": restricted,
            "database": database,
            "user": user,
        }
    finally:
        set_current_auth(old_auth)
        await qa_intel.close()
        await ch.disconnect()
        ch_client.command(f"DROP USER IF EXISTS {user}")
        ch_client.command(f"DROP DATABASE IF EXISTS {database}")


def _reader(record: dict[str, str]) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    set_current_auth(
        AuthInfo(
            subject="tracker-reader",
            org=record["org"],
            namespaces=(record["standard"], record["second"], record["restricted"]),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )


async def test_general_tracker_search_cites_public_issues_and_prs_only(
    tracker_record: dict[str, str],
    ch_client: Any,
    mint_test_token: Any,
    record_property: Any,
    control_plane_url: str,
) -> None:
    from server.memory.app.loom_search.tracker_leg import (
        prepare_tracker_search,
        tracker_leg,
        tracker_text_lines,
    )

    _reader(tracker_record)
    plan = await prepare_tracker_search("retention", org=tracker_record["org"])
    assert plan is not None
    assert set(plan.namespaces) == {tracker_record["standard"], tracker_record["second"]}
    result = await tracker_leg("retention", plan=plan)
    assert result is not None and result["mode"] == "search"
    assert {(row["namespace"], row["kind"], row["number"]) for row in result["results"]} == {
        (ns, kind, number)
        for ns in (tracker_record["standard"], tracker_record["second"])
        for kind, number in (("issue", 11), ("pr", 21))
    }
    text = "\n".join(tracker_text_lines(result))
    assert "https://example.test/issues/11" in text and "https://example.test/pull/21" in text
    assert "canary" not in json.dumps(result)

    # A token namespace claim alone cannot open a restricted record. Its live grant can.
    now = datetime.now(UTC)
    fields = [
        "org",
        "namespace",
        "subject",
        "role",
        "granted_at",
        "granted_by",
        "revoked_at",
        "version",
    ]
    base = [
        tracker_record["org"],
        tracker_record["restricted"],
        "tracker-reader",
        "viewer",
        now,
        "test",
    ]
    ch_client.insert("loom.namespace_grants", [[*base, None, time.time_ns()]], column_names=fields)
    _reader(tracker_record)
    granted = await prepare_tracker_search("retention", org=tracker_record["org"])
    assert granted is not None and tracker_record["restricted"] in granted.namespaces
    granted_result = await tracker_leg("retention", plan=granted, top_k=20)
    assert granted_result is not None
    assert any(
        r["namespace"] == tracker_record["restricted"] and r["number"] == 12
        for r in granted_result["results"]
    )
    assert all(r["number"] != 13 for r in granted_result["results"])
    ch_client.insert("loom.namespace_grants", [[*base, now, time.time_ns()]], column_names=fields)
    _reader(tracker_record)
    revoked = await prepare_tracker_search("retention", org=tracker_record["org"])
    assert revoked is not None and tracker_record["restricted"] not in revoked.namespaces

    import httpx

    from server.main import app

    token = mint_test_token(
        org=tracker_record["org"],
        namespaces=[
            tracker_record["standard"],
            tracker_record["second"],
            tracker_record["restricted"],
        ],
        roles=["viewer"],
        subject="tracker-reader",
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=control_plane_url
        ) as client:
            response = await client.post(
                "/v1/search.query",
                json={
                    "org": tracker_record["org"],
                    "namespace": tracker_record["standard"],
                    "query": "retention",
                    "namespace_scope": "this",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            mcp = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "loom_search", "arguments": {"query": "retention"}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
    assert mcp.status_code == 200, mcp.text
    answer = mcp.json()["result"]
    assert not answer.get("isError"), answer
    assert answer["structuredContent"]["tracker"]["mode"] == "search"
    rendered = "\n".join(part.get("text", "") for part in answer["content"])
    assert (
        "https://example.test/issues/11" in rendered and "https://example.test/pull/21" in rendered
    )
    assert "canary" not in json.dumps(answer["structuredContent"]["tracker"])
    assert response.status_code == 200, response.text
    assert response.json()["tracker"]["mode"] == "search"
    assert "canary" not in json.dumps(response.json()["tracker"])

    ch_client.command("SYSTEM FLUSH LOGS")
    metrics = ch_client.query(
        "SELECT count(), sum(read_rows), sum(read_bytes), max(memory_usage), max(query_duration_ms) "
        "FROM system.query_log WHERE user={user:String} AND type='QueryFinish'",
        parameters={"user": tracker_record["user"]},
    ).result_rows[0]
    assert metrics[0] > 0
    record_property(
        "tracker_sql_metrics",
        json.dumps(
            dict(
                zip(
                    ("queries", "read_rows", "read_bytes", "peak_memory_bytes", "slowest_query_ms"),
                    metrics,
                    strict=True,
                )
            )
        ),
    )
