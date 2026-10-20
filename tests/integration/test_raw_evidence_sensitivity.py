"""A conversation chunk that holds a credential is labelled 'high', stored as written with
no embedding, never returned by `memory.search` or handed to `memory.ask`, and served by
`memory.get` on its `chunk:` key only with a `memory.sensitive_read` audit row. A plain
chunk of the same conversation is still found. A `conversation.append` turn that holds a
credential is stored as written with no embedding row, so `conversation.search` never
returns it, while the thread's own read still lists it.

Runs against a real server and ClickHouse (LOOM_BASE_URL, LOOM_CH_*).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

import httpx
import pytest

from server.memory.domain.sensitivity import reads_high
from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

ORG = unique_org("raw-evidence")
_NONCE = os.urandom(3).hex()
# One namespace per test, so one test's identical passage never ranks in another's search.
NS_LABELLED, NS_LEGACY, NS_DIGEST, NS_TURNS = (
    f"raw-{name}-{_NONCE}" for name in ("labelled", "legacy", "digest", "turns")
)

# Credential shapes the detector reads as 'high', with nonce bodies. Never real secrets.
_API_KEY = "sk-" + "proj-" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
_AWS_KEY_ID = "AKIA" + uuid.uuid4().hex[:16].upper()

_PLAIN_TURN = (
    "We planned the garden for the spring: tomatoes along the south fence, basil beside "
    "them, and a row of marigolds to keep pests away. "
) * 5
_SECRET_TURN = (
    f"For the billing job use the key {_API_KEY} and the access key id {_AWS_KEY_ID} "
    "when you run the nightly export. "
) * 5


def _wait(predicate: Callable[[], bool], *, timeout: float = 20.0) -> None:
    wait_until(predicate, max_wait=timeout, interval=0.5)


@pytest.fixture(scope="module")
def token() -> str:
    from server.auth.jwt import mint_token

    return mint_token(
        subject="raw-evidence",
        org=ORG,
        namespaces=[NS_LABELLED, NS_LEGACY, NS_DIGEST, NS_TURNS],
        roles=["admin", "writer"],  # admin: conversation.set_logging
        ttl_seconds=600,
    )


def _post(base: str, token: str, ns: str, op: str, payload: dict) -> httpx.Response:
    body = {"org": ORG, "namespace": ns, "agent": "a", **payload}
    with httpx.Client(base_url=base, timeout=60.0) as c:
        return c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})


def _ingest(base: str, token: str, ns: str, session_id: str, messages: list[dict]) -> None:
    r = _post(
        base,
        token,
        ns,
        "memory.set_from_messages",
        {"session_id": session_id, "messages": messages},
    )
    assert r.status_code == 200, r.text


def _chunks(ch_client, ns: str, session_id: str) -> list[tuple[int, str, str, int]]:
    return [
        (int(i), str(content), str(level), int(dim))
        for i, content, level, dim in ch_client.query(
            "SELECT chunk_index, content, sensitivity, embedding_dim FROM loom.memory_chunks FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} AND session_id = {s:String} "
            "AND tombstoned_at IS NULL ORDER BY chunk_index",
            parameters={"o": ORG, "n": ns, "s": session_id},
        ).result_rows
    ]


def _search(base: str, token: str, ns: str, query: str) -> httpx.Response:
    r = _post(base, token, ns, "memory.search", {"query": query, "top_k": 10, "fresh": True})
    assert r.status_code == 200, r.text
    return r


def _keys(r: httpx.Response) -> list[str]:
    return [str(h.get("memory_key")) for h in r.json().get("results", [])]


def _sensitive_reads(ch_client, ns: str, memory_key: str) -> int:
    return int(
        ch_client.query(
            "SELECT count() FROM loom.audit_log WHERE org = {o:String} AND namespace = {n:String} "
            "AND event_type = 'memory.sensitive_read' "
            "AND JSONExtractString(subject, 'memory_key') = {k:String}",
            parameters={"o": ORG, "n": ns, "k": memory_key},
        ).result_rows[0][0]
    )


def test_a_chunk_holding_a_credential_is_stored_unembedded_and_read_only_by_key(
    control_plane_url: str, ch_client, token: str
) -> None:
    session = f"s-{uuid.uuid4().hex[:8]}"
    _ingest(
        control_plane_url,
        token,
        NS_LABELLED,
        session,
        [{"role": "user", "content": _PLAIN_TURN}, {"role": "user", "content": _SECRET_TURN}],
    )
    chunks = _chunks(ch_client, NS_LABELLED, session)
    high = [c for c in chunks if _API_KEY in c[1] or _AWS_KEY_ID in c[1]]
    plain = [c for c in chunks if c not in high]
    assert high and plain, chunks
    # Stored as written, labelled, and without a vector; the plain chunk is embedded.
    assert all(level == "high" and dim == 0 for _i, _c, level, dim in high), high
    assert all(level == "" and dim > 0 for _i, _c, level, dim in plain), plain
    assert any(_API_KEY in content for _i, content, _l, _d in high)

    # memory.search finds the plain chunk and never returns the credential.
    r = _search(
        control_plane_url, token, NS_LABELLED, "tomatoes along the south fence in the garden"
    )
    assert f"chunk:{session}:{plain[0][0]}" in _keys(r), _keys(r)
    for query in ("nightly export billing job key", "access key id for the billing job"):
        r = _search(control_plane_url, token, NS_LABELLED, query)
        assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text
        assert not {f"chunk:{session}:{i}" for i, *_ in high} & set(_keys(r))

    # memory.ask never hands it to its model or the caller.
    r = _post(
        control_plane_url,
        token,
        NS_LABELLED,
        "memory.ask",
        {"query": "Which key does the nightly export of the billing job use?"},
    )
    assert r.status_code == 200, r.text
    assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text

    # A keyed read serves it and leaves an audit row; a plain neighbour's read leaves it out.
    high_key = f"chunk:{session}:{high[0][0]}"
    r = _post(control_plane_url, token, NS_LABELLED, "memory.get", {"memory_key": high_key})
    assert r.status_code == 200 and r.json().get("found") is True, r.text
    assert _API_KEY in r.json()["value"]["content"] or _AWS_KEY_ID in r.json()["value"]["content"]
    _wait(lambda: _sensitive_reads(ch_client, NS_LABELLED, high_key) >= 1)

    plain_key = f"chunk:{session}:{plain[0][0]}"
    r = _post(control_plane_url, token, NS_LABELLED, "memory.get", {"memory_key": plain_key})
    assert r.status_code == 200 and r.json().get("found") is True, r.text
    assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text
    assert _sensitive_reads(ch_client, NS_LABELLED, plain_key) == 0


def test_a_chunk_written_before_the_label_is_held_back_by_its_text(
    control_plane_url: str, ch_client, token: str
) -> None:
    """A chunk an earlier build wrote carries no label and has a vector: every read that
    serves chunk text checks it. It is given the plain chunk's own vector, so it would
    rank beside it."""
    session = f"s-{uuid.uuid4().hex[:8]}"
    legacy = f"legacy-{uuid.uuid4().hex[:8]}"
    _ingest(
        control_plane_url, token, NS_LEGACY, session, [{"role": "user", "content": _PLAIN_TURN}]
    )
    content = f"[user] {_PLAIN_TURN[:300]} The deploy key is {_API_KEY}."
    ch_client.command(
        "INSERT INTO loom.memory_chunks (org, namespace, agent, user_id, session_id, run_id, "
        "chunk_index, content_sha256, content, embedding, embedding_model, embedding_dim, "
        "parent_memory_key, source_url, created_at) "
        "SELECT org, namespace, agent, user_id, {legacy:String}, run_id, 0, "
        "lower(hex(SHA256({content:String}))), {content:String}, embedding, embedding_model, "
        "embedding_dim, '', '', created_at FROM loom.memory_chunks FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND session_id = {s:String} "
        "AND chunk_index = 0",
        parameters={"o": ORG, "n": NS_LEGACY, "s": session, "legacy": legacy, "content": content},
    )
    [(_i, _c, level, dim)] = _chunks(ch_client, NS_LEGACY, legacy)
    assert (level, dim > 0) == ("", True)

    r = _search(control_plane_url, token, NS_LEGACY, "tomatoes along the south fence in the garden")
    assert f"chunk:{session}:0" in _keys(r), _keys(r)
    assert f"chunk:{legacy}:0" not in _keys(r) and _API_KEY not in r.text

    legacy_key = f"chunk:{legacy}:0"
    r = _post(control_plane_url, token, NS_LEGACY, "memory.get", {"memory_key": legacy_key})
    assert r.status_code == 200 and _API_KEY in r.json()["value"]["content"], r.text
    _wait(lambda: _sensitive_reads(ch_client, NS_LEGACY, legacy_key) >= 1)


def test_a_session_digest_that_reads_as_a_credential_has_no_vector(
    control_plane_url: str, ch_client, token: str
) -> None:
    session = f"s-{uuid.uuid4().hex[:8]}"
    _ingest(
        control_plane_url,
        token,
        NS_DIGEST,
        session,
        [{"role": "user", "content": _SECRET_TURN}, {"role": "user", "content": _PLAIN_TURN}],
    )
    digests = ch_client.query(
        "SELECT summary_text, embedding_dim FROM loom.session_summaries FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} AND session_id = {s:String} "
        "AND tombstoned_at IS NULL",
        parameters={"o": ORG, "n": NS_DIGEST, "s": session},
    ).result_rows
    assert digests
    for text, dim in digests:
        assert (int(dim) == 0) == reads_high(str(text)), (text, dim)
    r = _search(control_plane_url, token, NS_DIGEST, "nightly export billing job key")
    assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text


def test_a_conversation_turn_holding_a_credential_is_never_a_search_hit(
    control_plane_url: str, ch_client, token: str
) -> None:
    thread = f"t-{uuid.uuid4().hex[:8]}"
    r = _post(control_plane_url, token, NS_TURNS, "conversation.set_logging", {"enabled": True})
    assert r.status_code == 200, r.text
    for msg_id, content in (("m-plain", _PLAIN_TURN), ("m-key", _SECRET_TURN)):
        r = _post(
            control_plane_url,
            token,
            NS_TURNS,
            "conversation.append",
            {"thread_id": thread, "msg_id": msg_id, "role": "user", "content": content},
        )
        assert r.status_code == 200 and r.json().get("reason", "") == "", r.text

    embedded = {
        str(msg_id)
        for (msg_id,) in ch_client.query(
            "SELECT msg_id FROM loom.conversation_embeddings FINAL WHERE org = {o:String} "
            "AND namespace = {n:String} AND thread_id = {t:String} AND tombstoned_at IS NULL",
            parameters={"o": ORG, "n": NS_TURNS, "t": thread},
        ).result_rows
    }
    assert embedded == {"m-plain"}

    def search(query: str) -> httpx.Response:
        r = _post(
            control_plane_url, token, NS_TURNS, "conversation.search", {"query": query, "top_k": 10}
        )
        assert r.status_code == 200, r.text
        return r

    for query in ("tomatoes along the south fence", "nightly export billing job key"):
        r = search(query)
        assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text
        assert "m-key" not in {h.get("msg_id") for h in r.json().get("hits", [])}
    r = search("tomatoes along the south fence")
    assert "m-plain" in {h.get("msg_id") for h in r.json().get("hits", [])}, r.text

    # The thread's own read lists the turn as written.
    r = _post(control_plane_url, token, NS_TURNS, "conversation.list", {"thread_id": thread})
    assert r.status_code == 200, r.text
    assert any(_API_KEY in row["content"] for row in r.json()["rows"])
