"""A conversation chunk that holds a credential is stored as written with no vector,
never returned by `memory.search` or handed to `memory.ask`, and served by
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
from typing import Any

import httpx
import pytest

from tests.integration._eventually import wait_until
from tests.integration._resources import unique_org

ORG = unique_org("raw-evidence")
_NONCE = os.urandom(3).hex()
# One namespace per test, so one test's identical passage never ranks in another's search.
NS_CHUNKS, NS_LEGACY, NS_DIGEST, NS_TURNS, NS_SAVED, NS_TIE, NS_CUT = (
    f"raw-{name}-{_NONCE}"
    for name in ("chunks", "legacy", "digest", "turns", "saved", "tie", "cut")
)

# Credential shapes `reads_high` matches, with nonce bodies. Never real secrets.
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
        namespaces=[NS_CHUNKS, NS_LEGACY, NS_DIGEST, NS_TURNS, NS_SAVED, NS_TIE, NS_CUT],
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


def _chunks(ch_client, ns: str, session_id: str) -> list[tuple[int, str, int]]:
    return [
        (int(i), str(content), int(dim))
        for i, content, dim in ch_client.query(
            "SELECT chunk_index, content, embedding_dim FROM loom.memory_chunks FINAL "
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
        NS_CHUNKS,
        session,
        [{"role": "user", "content": _PLAIN_TURN}, {"role": "user", "content": _SECRET_TURN}],
    )
    chunks = _chunks(ch_client, NS_CHUNKS, session)
    high = [c for c in chunks if _API_KEY in c[1] or _AWS_KEY_ID in c[1]]
    plain = [c for c in chunks if c not in high]
    assert high and plain, chunks
    # Stored as written and without a vector; the plain chunk is embedded.
    assert all(dim == 0 for _i, _c, dim in high), high
    assert all(dim > 0 for _i, _c, dim in plain), plain
    assert any(_API_KEY in content for _i, content, _d in high)

    # memory.search finds the plain chunk and never returns the credential.
    r = _search(control_plane_url, token, NS_CHUNKS, "tomatoes along the south fence in the garden")
    assert f"chunk:{session}:{plain[0][0]}" in _keys(r), _keys(r)
    for query in ("nightly export billing job key", "access key id for the billing job"):
        r = _search(control_plane_url, token, NS_CHUNKS, query)
        assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text
        assert not {f"chunk:{session}:{i}" for i, *_ in high} & set(_keys(r))

    # memory.ask never hands it to its model or the caller.
    r = _post(
        control_plane_url,
        token,
        NS_CHUNKS,
        "memory.ask",
        {"query": "Which key does the nightly export of the billing job use?"},
    )
    assert r.status_code == 200, r.text
    assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text

    # A keyed read serves it and leaves an audit row; a plain neighbour's read leaves it out.
    high_key = f"chunk:{session}:{high[0][0]}"
    r = _post(control_plane_url, token, NS_CHUNKS, "memory.get", {"memory_key": high_key})
    assert r.status_code == 200 and r.json().get("found") is True, r.text
    assert _API_KEY in r.json()["value"]["content"] or _AWS_KEY_ID in r.json()["value"]["content"]
    _wait(lambda: _sensitive_reads(ch_client, NS_CHUNKS, high_key) >= 1)

    plain_key = f"chunk:{session}:{plain[0][0]}"
    r = _post(control_plane_url, token, NS_CHUNKS, "memory.get", {"memory_key": plain_key})
    assert r.status_code == 200 and r.json().get("found") is True, r.text
    assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text
    assert _sensitive_reads(ch_client, NS_CHUNKS, plain_key) == 0


def test_a_chunk_an_earlier_build_embedded_is_held_back_by_its_text(
    control_plane_url: str, ch_client, token: str
) -> None:
    """A chunk an earlier build wrote has a vector whatever it holds: every read that
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
    [(_i, _c, dim)] = _chunks(ch_client, NS_LEGACY, legacy)
    assert dim > 0

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
    """Imported so the digest's text is known: a plain digest is embedded and found, one
    that reads as a credential is stored as written with no vector and never a hit."""
    plain, secret = f"s-{uuid.uuid4().hex[:8]}", f"s-{uuid.uuid4().hex[:8]}"
    r = _post(
        control_plane_url,
        token,
        NS_DIGEST,
        "memory.import_evidence",
        {
            "digests": [
                {"session_id": plain, "summary_text": _PLAIN_TURN[:300]},
                {"session_id": secret, "summary_text": _SECRET_TURN[:300]},
            ]
        },
    )
    assert r.status_code == 200 and r.json()["digests_written"] == 2, r.text
    dims = dict(
        ch_client.query(
            "SELECT session_id, embedding_dim FROM loom.session_summaries FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL",
            parameters={"o": ORG, "n": NS_DIGEST},
        ).result_rows
    )
    assert int(dims[secret]) == 0 and int(dims[plain]) > 0, dims
    r = _search(control_plane_url, token, NS_DIGEST, "nightly export billing job key")
    assert f"summary:{secret}" not in _keys(r)
    assert _API_KEY not in r.text and _AWS_KEY_ID not in r.text
    r = _search(control_plane_url, token, NS_DIGEST, "tomatoes along the south fence")
    assert f"summary:{plain}" in _keys(r), _keys(r)


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


def _insert_turns(
    ch_client: Any,
    ns: str,
    thread: str,
    turns: list[tuple[str, str, bool]],
    *,
    role_of: dict[str, str] | None = None,
    agent_of: dict[str, str] | None = None,
) -> None:
    """Transcript rows as an earlier build wrote them, all at one `ts`."""
    from datetime import UTC, datetime

    ts = datetime.now(UTC).replace(microsecond=0, tzinfo=None)
    for msg_id, content, saved in turns:
        ch_client.command(
            "INSERT INTO loom.conversations (org, namespace, agent, thread_id, msg_id, role, "
            "content, user_id, is_saved, ts) VALUES ({o:String}, {n:String}, {a:String}, "
            "{t:String}, {m:String}, {r:String}, {c:String}, 'u-1', {saved:Bool}, "
            "{ts:DateTime64(3)})",
            parameters={
                "o": ORG,
                "n": ns,
                "t": thread,
                "m": msg_id,
                "r": (role_of or {}).get(msg_id, "user"),
                "a": (agent_of or {}).get(msg_id, "a"),
                "c": content,
                "saved": saved,
                "ts": ts,
            },
        )


def test_a_saved_turn_an_earlier_build_kept_is_never_a_keyword_hit_or_listed(
    control_plane_url: str, ch_client, token: str
) -> None:
    """A credential turn saved before the save refused one: the keyword leg never returns
    it, and the saved list shows it without its text."""
    thread = f"t-{uuid.uuid4().hex[:8]}"
    _insert_turns(
        ch_client,
        NS_SAVED,
        thread,
        [
            ("m-key", f"the access key id is {_AWS_KEY_ID}", True),
            ("m-plain", "deploy plan notes", True),
        ],
    )
    body = {"user_id": "u-1", "query": _AWS_KEY_ID, "search_mode": "lexical", "top_k": 5}
    r = _post(control_plane_url, token, NS_SAVED, "conversation.search_saved_messages", body)
    assert r.status_code == 200, r.text
    assert _AWS_KEY_ID not in r.text and r.json()["hits"] == []
    r = _post(
        control_plane_url,
        token,
        NS_SAVED,
        "conversation.search_saved_messages",
        {**body, "query": "access key deploy", "search_mode": "hybrid"},
    )
    assert r.status_code == 200 and _AWS_KEY_ID not in r.text, r.text
    r = _post(
        control_plane_url, token, NS_SAVED, "conversation.list_saved_messages", {"user_id": "u-1"}
    )
    assert r.status_code == 200, r.text
    listed = {row["message_id"]: row["content_excerpt"] for row in r.json()["rows"]}
    assert listed["m-key"] == "" and listed["m-plain"] == "deploy plan notes"


def test_the_thread_list_names_one_last_turn_on_a_ts_tie(
    control_plane_url: str, ch_client, token: str
) -> None:
    """Two last turns at one `ts`: the listing's agent, role and excerpt come from the
    same one (`msg_id` breaks the tie), and that one is checked."""
    thread = f"t-{uuid.uuid4().hex[:8]}"
    _insert_turns(
        ch_client,
        NS_TIE,
        thread,
        [("m-a", "a plain last turn", False), ("m-b", f"key {_API_KEY}", False)],
        role_of={"m-a": "assistant"},
        agent_of={"m-a": "b"},
    )
    r = _post(
        control_plane_url,
        token,
        NS_TIE,
        "conversation.threads",
        {"window_days": 30, "agent": "*", "limit": 50},
    )
    assert r.status_code == 200, r.text
    [row] = [t for t in r.json()["threads"] if t["thread_id"] == thread]
    assert row["last_content_excerpt"] == "" and _API_KEY not in r.text
    assert (row["last_role"], row["agent"]) == ("user", "a")  # m-b's, not m-a's


def test_a_key_that_crosses_the_excerpt_cut_is_listed_empty(
    control_plane_url: str, ch_client, token: str
) -> None:
    """A ~2 KB last turn whose key starts before character 240 and ends after it: the
    listing checks past the cut and shows no excerpt; a plain one of the same size shows
    its first 240 characters."""
    secret_thread, plain_thread = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
    head = "x" * 230 + " "  # the key starts at character 232 and runs past 240
    secret = head + _AWS_KEY_ID + " " + "y" * 1800
    plain = head + "z" * 1820
    _insert_turns(ch_client, NS_CUT, secret_thread, [("m-1", secret, False)])
    _insert_turns(ch_client, NS_CUT, plain_thread, [("m-1", plain, False)])
    r = _post(
        control_plane_url,
        token,
        NS_CUT,
        "conversation.threads",
        {"window_days": 30, "agent": "*", "limit": 50},
    )
    assert r.status_code == 200, r.text
    listed = {t["thread_id"]: t["last_content_excerpt"] for t in r.json()["threads"]}
    assert listed[secret_thread] == "" and _AWS_KEY_ID[:8] not in r.text
    assert listed[plain_thread] == plain[:240]
