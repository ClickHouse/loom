"""Erasure captures consent scopes and raw metadata before the content disappears."""

from __future__ import annotations

import json
import uuid

import pytest

from server.core.ch import get_client
from server.core.errors import ValidationError
from server.erasure.store import refusals as capture
from server.memory.store import refusals as policy
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def ch(monkeypatch):
    client = get_client()
    await client.connect()
    monkeypatch.setattr(capture, "_default_client", lambda: client)
    monkeypatch.setattr(policy, "_writer", lambda: "process:test")
    yield client
    await client.disconnect()


def _row(org, ns, uid, value, *, key="source", **fields):
    return {
        "org": org,
        "namespace": ns,
        "agent": "writer",
        "user_id": uid,
        "session_id": "-",
        "memory_key": key,
        "memory_type": "fact",
        "tags": [],
        "value": json.dumps(value),
        "version": 1,
        **fields,
    }


async def _refused(ch, row):
    with pytest.raises(ValidationError, match="previous deletion"):
        await policy.check_clickhouse(ch, [row])


async def test_namespace_capture_refuses_every_user_only_in_its_namespace(ch):
    org = unique_org("refusal-namespace")
    values = [f"retired namespace fact {uuid.uuid4().hex}" for _ in range(2)]
    await ch.insert(
        "loom.memory_versions",
        [
            _row(org, "closed", uid, value)
            for uid, value in zip(("alice", "bob"), values, strict=True)
        ],
    )
    assert await capture.capture_namespace(org, "closed") == 2
    for value in values:
        await _refused(ch, _row(org, "closed", "future-user", value))
        await policy.check_clickhouse(ch, [_row(org, "elsewhere", "alice", value)])
    await policy.check_clickhouse(ch, [_row(org, "closed", "alice", "unrelated fact")])


async def test_user_capture_reaches_future_namespaces_and_derived_writer_identity(ch):
    org = unique_org("refusal-user")
    parent = _row(org, "first", "alice", "original private fact")
    derived = _row(
        org,
        "first",
        "-",
        "consolidated private fact",
        key="summary",
        tags=["consolidated", "src:source"],
    )
    other = _row(org, "second", "alice", "second private fact")
    await ch.insert("loom.memory", [parent, derived, other])
    assert await capture.capture_user(org, "alice") == 3
    for value in ("original private fact", "consolidated private fact", "second private fact"):
        await _refused(ch, _row(org, "future", "alice", value))
        await policy.check_clickhouse(ch, [_row(org, "future", "bob", value)])


async def test_capture_decodes_structured_metadata_and_keeps_literal_null(ch):
    org = unique_org("refusal-metadata")
    await ch.insert(
        "loom.memory_evidence",
        [
            {
                "org": org,
                "namespace": "facts",
                "agent": "writer",
                "user_id": "alice",
                "session_id": "-",
                "evidence_id": uuid.uuid4(),
                "content": "null",
                "metadata": json.dumps({"credential": "copper lark secret"}),
            }
        ],
    )
    assert await capture.capture_user(org, "alice") == 2
    for value in ("null", {"credential": "copper lark secret"}):
        await _refused(ch, _row(org, "future", "alice", value))
    await policy.check_clickhouse(ch, [_row(org, "future", "alice", {"other": "allowed"})])


async def test_a_large_source_value_never_becomes_a_cursor_parameter(ch, monkeypatch):
    org = unique_org("refusal-cursor")
    value = "x" * 150_000
    await ch.insert("loom.memory_versions", [_row(org, "facts", "alice", value)])
    cursors = []
    query = ch.query_all

    async def observe(sql, params=None):
        if params and params.get("after"):
            cursors.append(params["after"])
        return await query(sql, params=params)

    monkeypatch.setattr(ch, "query_all", observe)
    assert await capture.capture_user(org, "alice") == 1
    assert cursors and all(len(str(cursor)) < 1000 for cursor in cursors)
    await _refused(ch, _row(org, "future", "alice", value))
