"""Known-parent status filters exclude retired text beside live and session-only chunks."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

import pytest

from server.memory.app.retrieval.chunks import _chunk_parent_pred
from tests.integration._resources import unique_org
from tests.unit.test_memory_search_service import _q

ORG = unique_org("chunk-parent-status")
_BASE = {"active", "conflicted", "session-only", "point-write"}
_VECTOR = [1.0] + [0.0] * 3071


def _parent(ch, ns, key, status, *, user="reader", session="session", invalid_at=None):
    columns = [
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "memory_key",
        "memory_type",
        "value",
        "status",
        "created_at",
        "invalid_at",
    ]
    ch.insert(
        "loom.memory",
        [
            [
                ORG,
                ns,
                "writer",
                user,
                session,
                key,
                "semantic",
                '"parent text"',
                status,
                datetime(2020, 1, 1),
                invalid_at,
            ]
        ],
        column_names=columns,
    )


def _chunk(ch, ns, key, index, *, user="reader", session="session", content=None):
    content = content or key
    columns = [
        "org",
        "namespace",
        "agent",
        "user_id",
        "session_id",
        "chunk_index",
        "content_sha256",
        "content",
        "parent_memory_key",
        "embedding_model",
        "embedding_dim",
        "embedding",
    ]
    ch.insert(
        "loom.memory_chunks",
        [
            [
                ORG,
                ns,
                "writer",
                user,
                session,
                index,
                hashlib.sha256(content.encode()).hexdigest(),
                content,
                key,
                "fixture-model",
                len(_VECTOR),
                _VECTOR,
            ]
        ],
        column_names=columns,
    )


def _read(ch, ns, **options):
    q = _q(org=ORG, namespace=ns, agent="*", **options)
    params = {"org": ORG, "ns": ns}
    pred = _chunk_parent_pred(q, params)
    return {
        r[0]
        for r in ch.query(
            "SELECT c.content FROM loom.memory_chunks AS c FINAL "
            "WHERE c.org = {org:String} AND c.namespace = {ns:String} " + pred,
            parameters=params,
        ).result_rows
    }


@pytest.fixture
def parents(ch_client):
    ns = "parents-" + uuid.uuid4().hex
    for index, status in enumerate(
        ["active", "conflicted", "rejected", "superseded", "duplicate", "proposed", "tombstoned"]
    ):
        _parent(ch_client, ns, status, status)
        _chunk(ch_client, ns, status, index)
        ch_client.insert(
            "loom.memory_embeddings",
            [
                [
                    ORG,
                    ns,
                    "writer",
                    "reader",
                    "session",
                    status,
                    "semantic",
                    "private",
                    "parent text",
                    "fixture-model",
                    status,
                    ["topic"],
                    len(_VECTOR),
                    _VECTOR,
                    len(_VECTOR),
                    _VECTOR,
                ]
            ],
            column_names=[
                "org",
                "namespace",
                "agent",
                "user_id",
                "session_id",
                "memory_key",
                "memory_type",
                "scope",
                "content_excerpt",
                "embedding_model",
                "status",
                "tags",
                "embedding_dim",
                "embedding",
                "memory_key_embedding_dim",
                "memory_key_embedding",
            ],
        )
    _chunk(ch_client, ns, "no-extracted-parent", 8, content="session-only")
    _chunk(ch_client, ns, "rejected", 0, session="memory.set:rejected", content="point-write")
    # Another identity's row cannot hide a live passage or revive rejected text.
    _parent(ch_client, ns, "active", "rejected", user="other-reader")
    _parent(ch_client, ns, "conflicted", "rejected", session="other-session")
    _parent(ch_client, ns, "rejected", "active", user="other-reader")
    return ns


def test_default_parent_status_keeps_populated_neighbours_and_session_evidence(ch_client, parents):
    assert _read(ch_client, parents) == _BASE


@pytest.mark.parametrize(
    "flag,status",
    [
        ("include_proposed", "proposed"),
        ("include_duplicates", "duplicate"),
        ("include_superseded", "superseded"),
    ],
)
def test_parent_status_opt_in_is_specific_to_the_requested_state(ch_client, parents, flag, status):
    assert _read(ch_client, parents, **{flag: True}) == _BASE | {status}


def test_hard_tags_require_a_matching_parent_and_still_exclude_retired_text(ch_client, parents):
    assert _read(ch_client, parents, tags=["topic"]) == {"active", "conflicted"}


def test_knowledge_time_keeps_only_parents_that_were_served_at_the_anchor(ch_client):
    ns = "history-" + uuid.uuid4().hex
    for index, (key, retirement) in enumerate(
        [
            ("retired-after", datetime(2020, 1, 3)),
            ("retired-before", datetime(2020, 1, 1, 12)),
        ]
    ):
        _parent(ch_client, ns, key, "superseded", invalid_at=retirement)
        _chunk(ch_client, ns, key, index)
    _parent(ch_client, ns, "active", "active")
    _chunk(ch_client, ns, "active", 2)
    assert _read(ch_client, ns) == {"active"}
    assert _read(ch_client, ns, as_of=datetime(2020, 1, 2), as_of_basis="knowledge_time") == {
        "active",
        "retired-after",
    }


@pytest.mark.parametrize("user,session", [("", "session"), ("reader", ""), ("", "")])
def test_explicit_blank_parent_identity_still_excludes_rejected_text(ch_client, user, session):
    ns = "blank-parent-" + uuid.uuid4().hex
    for index, status in enumerate(["active", "rejected"]):
        _parent(ch_client, ns, status, status, user=user, session=session)
        _chunk(ch_client, ns, status, index, user=user or "-", session=session or "-")
    assert _read(ch_client, ns) == {"active"}
