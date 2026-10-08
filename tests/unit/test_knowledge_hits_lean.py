"""Loom_search's knowledge hits drop what no reader uses —
the fusion `score`, the constant `source`, the ingestion `tags` — and keep
the chunk text whole (a 300-char window loses the answer in about 30% of
relevant hits). `bodies` decides tags and text."""

from __future__ import annotations

from typing import Any

import pytest

from server.transport import lean_results as lr


def _payload() -> dict[str, Any]:
    return {
        "memory": {"results": []},
        "knowledge": {
            "results": [
                {
                    "source_uri": "loom://a",
                    "content": "The freeze starts two days before the cut.",
                    "tags": ["kind:runbook", "team:sre"],
                    "similarity": 0.52,
                    "namespace": "home",
                    "section": "Release > Freeze",
                    "score": 0.031,
                    "source": "knowledge",
                },
                {
                    "source_uri": "loom://b",
                    "content": "Other text.",
                    "tags": ["x"],
                    "similarity": 0.41,
                    "namespace": "team-alerts",
                    "score": 0.02,
                    "source": "knowledge",
                },
            ]
        },
    }


def test_default_drops_score_source_tags_and_home_namespace_keeps_text() -> None:
    out = lr.lean_search_result(_payload(), {"query": "q"}, home_ns="home")
    a, b = out["knowledge"]["results"]
    assert a == {
        "source_uri": "loom://a",
        "content": "The freeze starts two days before the cut.",
        "similarity": 0.52,
        "section": "Release > Freeze",
    }
    assert b["namespace"] == "team-alerts" and "tags" not in b and "score" not in b


@pytest.mark.parametrize("bodies", ["full", "unit"])
def test_full_bodies_keep_the_tags(bodies: str) -> None:
    out = lr.lean_search_result(_payload(), {"query": "q", "bodies": bodies}, home_ns="home")
    assert out["knowledge"]["results"][0]["tags"] == ["kind:runbook", "team:sre"]
    assert "score" not in out["knowledge"]["results"][0]


def test_no_bodies_drop_the_chunk_text() -> None:
    out = lr.lean_search_result(_payload(), {"query": "q", "bodies": "none"}, home_ns="home")
    assert all("content" not in h for h in out["knowledge"]["results"])
    assert out["knowledge"]["results"][0]["source_uri"] == "loom://a"


def test_conversation_hits_drop_msg_id_trim_ts_and_home_namespace() -> None:
    p = {
        "memory": {"results": []},
        "conversations": {
            "results": [
                {
                    "thread_id": "t1",
                    "msg_id": "m1",
                    "role": "user",
                    "content_excerpt": "is this a bug?",
                    "ts": "2026-07-14T16:56:50.532000",
                    "namespace": "home",
                },
                {
                    "thread_id": "t2",
                    "msg_id": "m2",
                    "role": "assistant",
                    "content_excerpt": "yes",
                    "ts": "2026-07-15T01:02:03",
                    "namespace": "other",
                },
            ]
        },
    }
    out = lr.lean_search_result(p, {"query": "q"}, home_ns="home")
    a, b = out["conversations"]["results"]
    assert a == {
        "thread_id": "t1",
        "role": "user",
        "content_excerpt": "is this a bug?",
        "ts": "2026-07-14T16:56",
    }
    assert b["namespace"] == "other" and "msg_id" not in b
    full = lr.lean_search_result(
        {
            "memory": {"results": []},
            "conversations": {"results": [{"thread_id": "t", "msg_id": "m"}]},
        },
        {"query": "q", "bodies": "full"},
    )
    assert full["conversations"]["results"][0]["msg_id"] == "m"
