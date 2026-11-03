"""Knowledge sharing end to end (docs/architecture/sharing.md), against a live
server: a gateway's end users, collections and their inheritance, SCIM groups,
documents, saved messages and memories in `@me`, revocation, and a user's erasure.

Skips without LOOM_BASE_URL."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest

from tests import end_user_issuer
from tests.integration._eventually import ACL_OPS, acl_change_adopted, wait_until
from tests.integration._resources import unique_org

pytestmark = pytest.mark.slow


class Gateway:
    """The service in front of Loom: it signs a short-lived token for the user
    (and the agent) of each call, and pushes its groups as the org's admin."""

    def __init__(self, base: str, org: str, admin_token: str) -> None:
        self.base, self.org, self.admin_token = base, org, admin_token

    def call(
        self, user: str, op: str, payload: dict[str, Any], *, agent: str = ""
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {end_user_issuer.mint(self.org, user, agent=agent)}"}
        r = httpx.post(
            f"{self.base}/v1/{op}", json={"org": self.org, **payload}, headers=headers, timeout=60
        )
        if op in ACL_OPS and r.status_code == 200:
            acl_change_adopted()
        return r

    def ok(self, user: str, op: str, payload: dict[str, Any], **kw: str) -> dict[str, Any]:
        r = self.call(user, op, payload, **kw)
        assert r.status_code == 200, f"{op} as {user}: {r.status_code} {r.text}"
        return r.json()

    def scim(self, method: str, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        r = httpx.request(
            method,
            f"{self.base}/scim/v2/{path}",
            json=body,
            headers={"Authorization": f"Bearer {self.admin_token}"},
            timeout=30,
        )
        if method != "GET" and r.status_code < 300:
            acl_change_adopted()
        return r


def _search(gw: Gateway, user: str, namespace: str, query: str, **kw: str) -> list[dict]:
    r = gw.call(
        user,
        "knowledge.search",
        {"namespace": namespace, "query": query, "namespace_scope": "all_mine", "top_k": 20},
        **kw,
    )
    return r.json().get("results", []) if r.status_code == 200 else []


def _nonce() -> str:
    return f"zqshare{uuid.uuid4().hex[:10]}"


@pytest.fixture
def gw(control_plane_url: str, mint_test_token: Any) -> Gateway:
    if not end_user_issuer.configured():
        pytest.skip("no test end-user issuer: run `python -m tests.end_user_issuer <dir>`")
    org = unique_org("share")
    admin = mint_test_token(org=org, namespaces=["*"], roles=["org_admin"], subject="idp")
    return Gateway(control_plane_url, org, admin)


def _collection(gw: Gateway, owner: str, name: str, parent: str = "") -> str:
    gw.ok(owner, "collection.create", {"namespace": name, "parent": parent})
    return name


def _add(gw: Gateway, user: str, namespace: str, text: str, uri: str = "") -> str:
    uri = uri or f"doc/{uuid.uuid4().hex[:8]}"
    gw.ok(user, "knowledge.add", {"namespace": namespace, "source_uri": uri, "content": text})
    return uri


def test_a_document_added_to_a_shared_collection_reaches_the_group_at_once(gw: Gateway) -> None:
    q3 = _collection(gw, "alice", f"col-q3-{uuid.uuid4().hex[:6]}")
    plans = _collection(gw, "alice", f"col-plans-{uuid.uuid4().hex[:6]}", parent=q3)
    r = gw.scim("POST", "Groups", {"externalId": "finance", "members": [{"value": "bob"}]})
    assert r.status_code == 201, r.text
    gw.ok("alice", "grants.create", {"namespace": q3, "subject": "group:finance", "role": "viewer"})
    nonce = _nonce()
    _add(gw, "alice", plans, f"Q3 plan {nonce} for the renewal")
    hits = _search(gw, "bob", plans, nonce)
    assert any(nonce in h["content"] for h in hits), "inherited from the parent, at once"
    listed = gw.ok("bob", "collection.list", {})["collections"]
    assert {(c["namespace"], c["parent"], c["role"]) for c in listed if not c["personal"]} == {
        (q3, "", "viewer"),
        (plans, q3, "viewer"),
    }
    assert [c["role"] for c in listed if c["personal"]] == ["admin"], "bob's own"
    assert not any(nonce in h["content"] for h in _search(gw, "carol", "@me", nonce))
    # A writer role is needed to add; the group holds viewer.
    assert (
        gw.call(
            "bob", "knowledge.add", {"namespace": plans, "source_uri": "x", "content": "y"}
        ).status_code
        == 403
    )


def test_a_removed_member_and_a_revoked_grant_read_nothing(gw: Gateway) -> None:
    col = _collection(gw, "alice", f"col-hr-{uuid.uuid4().hex[:6]}")
    gw.scim(
        "POST", "Groups", {"externalId": "people", "members": [{"value": "bob"}, {"value": "dan"}]}
    )
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "group:people", "role": "viewer"})
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "user:erin", "role": "viewer"})
    nonce = _nonce()
    _add(gw, "alice", col, f"salary bands {nonce}")
    for user in ("bob", "dan", "erin"):
        assert any(nonce in h["content"] for h in _search(gw, user, col, nonce)), user
    patch = {
        "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
        "Operations": [{"op": "remove", "path": 'members[value eq "dan"]'}],
    }
    assert gw.scim("PATCH", "Groups/people", patch).status_code == 200
    gw.ok("alice", "grants.revoke", {"namespace": col, "subject": "user:erin"})
    for user in ("dan", "erin"):
        search = {"namespace": col, "query": nonce}
        assert gw.call(user, "knowledge.search", search).status_code == 403, user
        assert not any(nonce in h["content"] for h in _search(gw, user, "@me", nonce)), user
    assert any(nonce in h["content"] for h in _search(gw, "bob", col, nonce)), "bob stays"


def test_cycles_and_depth_are_refused(gw: Gateway) -> None:
    tag = uuid.uuid4().hex[:6]
    names = [_collection(gw, "alice", f"col-l0-{tag}")]
    for i in range(1, 9):
        names.append(_collection(gw, "alice", f"col-l{i}-{tag}", parent=names[-1]))
    r = gw.call("alice", "collection.create", {"namespace": f"col-l9-{tag}", "parent": names[-1]})
    assert r.status_code == 422 and "nest at most" in r.text
    r = gw.call("alice", "collection.move", {"namespace": names[0], "parent": names[3]})
    assert r.status_code == 422 and "cannot hold itself" in r.text


def test_no_request_field_reaches_another_users_personal_collection_or_widens_a_grant(
    gw: Gateway, control_plane_url: str, mint_test_token: Any
) -> None:
    nonce = _nonce()
    gw.ok(
        "alice",
        "knowledge.upsert",
        {"namespace": "@me", "source_uri": "doc/mine", "content": nonce},
    )
    alices = _personal(gw, "alice")
    # Bob names Alice's personal collection: refused, read and write.
    assert (
        gw.call(
            "bob", "knowledge.add", {"namespace": alices, "source_uri": "x", "content": "y"}
        ).status_code
        == 403
    )
    assert (
        gw.call("bob", "knowledge.search", {"namespace": alices, "query": nonce}).status_code == 403
    )
    # Bob's own `@me` is his, whatever the body says about users.
    assert not any(
        nonce in h["content"]
        for h in gw.ok(
            "bob", "knowledge.search", {"namespace": "@me", "query": nonce, "user_id": "alice"}
        )["results"]
    )
    # A personal collection takes no other grant, and Bob cannot grant on Alice's.
    r = gw.call(
        "alice", "grants.create", {"namespace": alices, "subject": "user:bob", "role": "viewer"}
    )
    assert r.status_code == 422, r.text
    r = gw.call(
        "bob", "grants.create", {"namespace": alices, "subject": "user:bob", "role": "admin"}
    )
    assert r.status_code == 403
    # An end-user token names its user in its signed `sub`; the header that a
    # trusted caller sends names no one else for it.
    r = httpx.post(
        f"{control_plane_url}/v1/knowledge.search",
        json={"org": gw.org, "namespace": "@me", "query": nonce},
        headers={
            "Authorization": f"Bearer {end_user_issuer.mint(gw.org, 'bob')}",
            "X-Loom-On-Behalf-Of": "user:alice",
        },
    )
    assert r.status_code == 200 and nonce not in r.text, r.text
    # A token no registered key signed is refused.
    stranger_key, _ = end_user_issuer.new_key()
    r = httpx.post(
        f"{control_plane_url}/v1/knowledge.search",
        json={"org": gw.org, "namespace": "@me", "query": nonce},
        headers={
            "Authorization": f"Bearer {end_user_issuer.mint(gw.org, 'alice', private_pem=stranger_key)}"
        },
    )
    assert r.status_code == 401, r.text
    # `@me` is only a gateway user's.
    tok = mint_test_token(org=gw.org, namespaces=["*"], roles=["writer"], subject="sa:other")
    r = httpx.post(
        f"{control_plane_url}/v1/knowledge.search",
        json={"org": gw.org, "namespace": "@me", "query": nonce},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400
    # An org admin token reaches no collection without a grant.
    admin = mint_test_token(
        org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="root"
    )
    r = httpx.post(
        f"{control_plane_url}/v1/knowledge.search",
        json={"org": gw.org, "namespace": alices, "query": nonce, "namespace_scope": "this"},
        headers={"Authorization": f"Bearer {admin}"},
    )
    assert r.status_code == 403


def test_a_long_document_is_chunked_whole_and_its_tail_is_found(gw: Gateway) -> None:
    tail = _nonce()
    body = ("The renewal clause covers notice periods and pricing. " * 380) + f"Final note {tail}."
    assert len(body) > 20_000
    gw.ok(
        "alice",
        "knowledge.upsert",
        {"namespace": "@me", "source_uri": "doc/long", "content": body},
    )
    rows = gw.ok("alice", "knowledge.list", {"namespace": "@me", "uri_prefix": ["doc/long"]})[
        "rows"
    ]
    assert rows and rows[0]["chunk_count"] > 10
    for mode in ("lexical", "hybrid"):
        hits = gw.ok(
            "alice",
            "knowledge.search",
            {"namespace": "@me", "query": tail, "search_mode": mode, "namespace_scope": "this"},
        )["results"]
        assert hits and hits[0]["source_uri"] == "doc/long" and tail in hits[0]["content"], mode


def test_an_end_user_saves_a_chat_message_in_me_and_no_one_else_reads_it(gw: Gateway) -> None:
    nonce = _nonce()
    turn = {"conversation_id": "c9", "message_id": "m9"}
    content = f"The renewal note {nonce}."
    body = {**turn, "role": "assistant", "content": content, "user_id": "alice"}
    saved = _chat_ok(gw, "alice", "conversation.save_message", body)
    assert saved["indexed"] is True, saved
    mine = {"user_id": "alice", "conversation_id": "c9"}
    rows = _chat_ok(gw, "alice", "conversation.list_saved_messages", mine)["rows"]
    assert [(r["conversation_id"], r["message_id"], r["role"]) for r in rows] == [
        ("c9", "m9", "assistant")
    ], rows
    assert rows[0]["content_excerpt"] == content, rows
    # A saved-only row is written with its save time as its turn time, so the
    # listing's saved_at matches the turn ts the save answers (both from the
    # server's clock, not this host's).
    listed_at = datetime.fromisoformat(rows[0]["ts"]).replace(tzinfo=None)
    saved_at = datetime.fromisoformat(saved["ts"]).replace(tzinfo=None)
    assert abs(listed_at - saved_at) < timedelta(seconds=1), (rows, saved)
    status = {**mine, "message_ids": ["m9", "m8"]}
    assert _chat_ok(gw, "alice", "conversation.saved_status", status)["saved"] == ["m9"]
    search = {"user_id": "alice", "query": nonce}
    hits = _chat_ok(gw, "alice", "conversation.search_saved_messages", search)["hits"]
    assert [h["message_id"] for h in hits] == ["m9"], hits
    # The transcript ops refuse an end-user token: `@me` holds saved messages only.
    append = {"thread_id": "c9", "role": "user", "content": "hi", "user_id": "alice"}
    assert _chat(gw, "alice", "conversation.append", append).status_code == 403
    assert _chat(gw, "alice", "conversation.get", {"thread_id": "c9"}).status_code == 403
    # Bob's `@me` is his own; naming Alice's collection or Alice is refused.
    bobs = {"user_id": "bob", "query": nonce}
    assert _chat_ok(gw, "bob", "conversation.search_saved_messages", bobs)["hits"] == []
    r = _chat(gw, "bob", "conversation.search_saved_messages", search)
    assert r.status_code == 403 and nonce not in r.text, r.text
    bobs_status = {"user_id": "bob", "conversation_id": "c9", "message_ids": ["m9"]}
    assert _chat_ok(gw, "bob", "conversation.saved_status", bobs_status)["saved"] == []
    assert _chat(gw, "bob", "conversation.saved_status", status).status_code == 403
    bobs_list = {"user_id": "bob", "conversation_id": "c9"}
    assert _chat_ok(gw, "bob", "conversation.list_saved_messages", bobs_list)["rows"] == []
    r = _chat(gw, "bob", "conversation.list_saved_messages", mine)
    assert r.status_code == 403 and nonce not in r.text, r.text
    r = gw.call(
        "bob",
        "conversation.search_saved_messages",
        {"namespace": _personal(gw, "alice"), "agent": "chat", **bobs},
        agent="chat",
    )
    assert r.status_code == 403 and nonce not in r.text, r.text
    # Bob's writes naming Alice are refused and leave her saved message in place.
    assert _chat(gw, "bob", "conversation.save_message", body).status_code == 403
    unsave = {**turn, "user_id": "alice"}
    assert _chat(gw, "bob", "conversation.unsave_message", unsave).status_code == 403
    rows = _chat_ok(gw, "alice", "conversation.list_saved_messages", mine)["rows"]
    assert [row["message_id"] for row in rows] == ["m9"], rows
    # A personal collection records no transcript, so unsaving removes the turn.
    unsaved = _chat_ok(gw, "alice", "conversation.unsave_message", unsave)
    assert unsaved["removed"] is True, unsaved
    assert _chat_ok(gw, "alice", "conversation.list_saved_messages", mine)["rows"] == []


def _chat(gw: Gateway, user: str, op: str, payload: dict[str, Any]) -> httpx.Response:
    """`op` on `user`'s `@me`, as the chat service calls it inside its agent `chat`."""
    return gw.call(user, op, {"namespace": "@me", "agent": "chat", **payload}, agent="chat")


def _chat_ok(gw: Gateway, user: str, op: str, payload: dict[str, Any]) -> dict[str, Any]:
    r = _chat(gw, user, op, payload)
    assert r.status_code == 200, f"{op} as {user}: {r.status_code} {r.text}"
    return r.json()


def _remember(gw: Gateway, user: str, nonce: str) -> list[str]:
    """`user`'s chat writes a fact holding `nonce` at `pref/drink` and a
    conversation's extractions to `@me`, then waits until the fact is
    searchable. Returns the extracted keys."""
    _chat_ok(gw, user, "memory.set", {"memory_key": "pref/drink", "value": nonce})
    extracted = _chat_ok(
        gw,
        user,
        "memory.set_from_messages",
        {
            "session_id": "c1",
            "messages": [
                {"role": "user", "content": f"I live in Lisbon, note {nonce}."},
                {"role": "assistant", "content": "Noted."},
            ],
        },
    )
    assert extracted["evidence_ids"], extracted
    keys = [w["memory_key"] for w in extracted["written"]]
    assert keys, extracted
    wait_until(
        lambda: any(
            h["memory_key"] == "pref/drink" and nonce in h["content_excerpt"]
            for h in _chat_ok(gw, user, "memory.search", {"query": nonce})["results"]
        ),
        max_wait=30.0,
        description=f"{user}'s pref/drink is searchable",
    )
    return keys


_STAMPED = ("namespace", "agent", "user_id", "session_id", "memory_key", "actor", "source_class")


def test_an_end_user_writes_memories_to_me_and_reads_them_back(gw: Gateway) -> None:
    alices = _personal(gw, "alice")
    nonce = _nonce()
    keys = _remember(gw, "alice", nonce)
    row = _chat_ok(gw, "alice", "memory.get", {"memory_key": "pref/drink"})
    assert row["found"], row
    by_id = _chat_ok(gw, "alice", "memory.get_by_id", {"memory_id": row["memory_id"]})
    assert tuple(by_id[k] for k in _STAMPED) == (
        alices,
        "chat",
        "alice",
        "-",
        "pref/drink",
        "human:user:alice",
        "asserted",
    ), by_id
    assert by_id["value"] == nonce, by_id
    for key in keys:
        got = _chat_ok(gw, "alice", "memory.get", {"session_id": "c1", "memory_key": key})
        assert got["found"], key
        by_id = _chat_ok(gw, "alice", "memory.get_by_id", {"memory_id": got["memory_id"]})
        assert tuple(by_id[k] for k in _STAMPED) == (
            alices,
            "chat",
            "alice",
            "c1",
            key,
            "human:user:alice",
            "extracted",
        ), by_id
        # The fake extractor's fact text is its own; the evidence ties it to the turn.
        assert by_id["value"], by_id
        evidence = _chat_ok(gw, "alice", "memory.evidence", {"session_id": "c1", "memory_key": key})
        assert any(nonce in item["content"] for item in evidence["items"]), evidence
    hits = _chat_ok(gw, "alice", "memory.search", {"query": nonce})["results"]
    assert any(h["memory_key"] == "pref/drink" and h["namespace"] == alices for h in hits), hits
    assert nonce in _chat_ok(gw, "alice", "context.pack", {"query": nonce})["rendered"]


def test_another_end_user_reads_none_of_my_memories(gw: Gateway) -> None:
    alices, bobs = _personal(gw, "alice"), _personal(gw, "bob")
    alice_nonce, bob_nonce = _nonce(), _nonce()
    alice_keys = _remember(gw, "alice", alice_nonce)
    bob_keys = _remember(gw, "bob", bob_nonce)
    alice_ids = [
        _chat_ok(gw, "alice", "memory.get", {"session_id": session, "memory_key": key})["memory_id"]
        for session, key in [("-", "pref/drink")] + [("c1", k) for k in alice_keys]
    ]
    # Both hold `pref/drink`; Bob's `@me` answers with his own.
    got = _chat_ok(gw, "bob", "memory.get", {"memory_key": "pref/drink"})
    assert got["value"] == bob_nonce, got
    for memory_id in alice_ids:
        by_id = _chat_ok(gw, "bob", "memory.get_by_id", {"memory_id": memory_id})
        assert not by_id["found"], by_id
    for query in (alice_nonce, bob_nonce):
        search = _chat(gw, "bob", "memory.search", {"query": query, "namespace_scope": "all_mine"})
        assert search.status_code == 200 and alice_nonce not in search.text, search.text
        assert {h["namespace"] for h in search.json()["results"]} <= {bobs}, search.text
        packed = _chat_ok(gw, "bob", "context.pack", {"query": query})["rendered"]
        assert alice_nonce not in packed, packed
    for key in set(alice_keys) | set(bob_keys):
        evidence = _chat(gw, "bob", "memory.evidence", {"session_id": "c1", "memory_key": key})
        assert evidence.status_code == 200 and alice_nonce not in evidence.text, evidence.text
    # The same reads answer Bob with his own rows.
    bob_search = {"query": bob_nonce, "namespace_scope": "all_mine"}
    hits = _chat_ok(gw, "bob", "memory.search", bob_search)["results"]
    assert any(h["memory_key"] == "pref/drink" for h in hits), hits
    assert bob_nonce in _chat_ok(gw, "bob", "context.pack", {"query": bob_nonce})["rendered"]
    for key in bob_keys:
        evidence = _chat_ok(gw, "bob", "memory.evidence", {"session_id": "c1", "memory_key": key})
        assert any(bob_nonce in item["content"] for item in evidence["items"]), evidence
    # Naming Alice's collection, or Alice in the body, is refused.
    r = _chat(gw, "bob", "memory.search", {"namespace": alices, "query": alice_nonce})
    assert r.status_code == 403, r.text
    r = _chat(gw, "bob", "memory.get", {"memory_key": "pref/drink", "user_id": "alice"})
    assert r.status_code == 403, r.text
    r = _chat(gw, "bob", "memory.set", {"memory_key": "k", "value": "v", "user_id": "alice"})
    assert r.status_code == 403, r.text


def test_an_agent_grant_reaches_whoever_runs_the_agent(gw: Gateway) -> None:
    col = _collection(gw, "alice", f"col-agent-{uuid.uuid4().hex[:6]}")
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "agent:helper", "role": "viewer"})
    nonce = _nonce()
    _add(gw, "alice", col, f"agent knowledge {nonce}")
    assert any(nonce in h["content"] for h in _search(gw, "bob", col, nonce, agent="helper"))
    assert gw.call("bob", "knowledge.search", {"namespace": col, "query": nonce}).status_code == 403


def test_a_users_erasure_leaves_no_trace_and_keeps_others_documents(
    gw: Gateway, control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    col = _collection(gw, "alice", f"col-team-{uuid.uuid4().hex[:6]}")
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "user:bob", "role": "writer"})
    gw.scim("POST", "Groups", {"externalId": "crew", "members": [{"value": "bob"}]})
    mine, theirs = _nonce(), _nonce()
    bobs = _add(gw, "bob", col, f"bob wrote {mine}")
    _add(gw, "alice", col, f"alice wrote {theirs}")
    # An edit by someone else keeps the document bob's.
    gw.ok(
        "alice",
        "knowledge.update_metadata",
        {"namespace": col, "source_uri": bobs, "title": "t", "reindex": True},
    )
    gw.ok(
        "bob", "knowledge.upsert", {"namespace": "@me", "source_uri": "doc/mine", "content": mine}
    )
    saved = {"conversation_id": "c1", "message_id": "m1", "user_id": "bob"}
    r = _chat_ok(gw, "bob", "conversation.save_message", {**saved, "role": "user", "content": mine})
    assert r["indexed"] is True, r
    admin = mint_test_token(
        org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="ops"
    )
    root = mint_test_token(org=gw.org, namespaces=["*"], roles=["platform_admin"], subject="root")

    def _exported(token: str) -> set[tuple[str, str]]:
        r = httpx.post(
            f"{control_plane_url}/v1/erasure.export",
            json={"org": gw.org, "scope_type": "user_id", "scope_value": "bob", "requester": "ops"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
        assert r.status_code == 200, r.text
        return {(row["namespace"], row["source_uri"]) for row in r.json()["knowledge"]["rows"]}

    # The org admin may erase bob's rows in collections it cannot read, not read
    # them; bob's own personal collection is his to have exported.
    assert {u for _, u in _exported(admin)} == {"doc/mine"}
    everything = _exported(root)
    assert (col, bobs) in everything and {u for _, u in everything} == {bobs, "doc/mine"}
    r = httpx.post(
        f"{control_plane_url}/v1/erasure.request",
        json={
            "org": gw.org,
            "scope_type": "user_id",
            "scope_value": "bob",
            "requester": "ops@x.io",
        },
        headers={"Authorization": f"Bearer {admin}"},
        timeout=180,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed", r.json()
    live = ch_client.query(
        "SELECT count() FROM loom.documents FINAL WHERE org = {o:String} AND user_id = 'bob' "
        "AND superseded_at IS NULL",
        parameters={"o": gw.org},
    ).result_rows[0][0]
    grants = ch_client.query(
        "SELECT count() FROM loom.namespace_grants FINAL WHERE org = {o:String} "
        "AND subject = 'user:bob' AND revoked_at IS NULL",
        parameters={"o": gw.org},
    ).result_rows[0][0]
    groups = ch_client.query(
        "SELECT count() FROM loom.principal_groups FINAL WHERE org = {o:String} "
        "AND has(members, 'user:bob') AND NOT deleted",
        parameters={"o": gw.org},
    ).result_rows[0][0]
    edited = ch_client.query(
        "SELECT count() FROM loom.documents FINAL WHERE org = {o:String} AND namespace = {n:String}"
        " AND source_uri = {u:String} AND superseded_at IS NULL",
        parameters={"o": gw.org, "n": col, "u": bobs},
    ).result_rows[0][0]
    assert (live, edited, grants, groups) == (0, 0, 0, 0)
    assert any(theirs in h["content"] for h in _search(gw, "alice", col, theirs)), "alice's stays"
    wait_until(
        lambda: (
            gw.call("bob", "knowledge.search", {"namespace": col, "query": theirs}).status_code
            == 403
        ),
        description="bob's grant revoked",
    )
    # Bob, back later, starts from an empty personal collection.
    gw.ok(
        "bob",
        "knowledge.upsert",
        {"namespace": "@me", "source_uri": "doc/new", "content": "new"},
    )
    left = gw.ok(
        "bob", "knowledge.search", {"namespace": "@me", "query": mine, "namespace_scope": "this"}
    )
    assert not any(mine in h["content"] for h in left["results"])
    listed = {"user_id": "bob", "conversation_id": "c1"}
    assert _chat_ok(gw, "bob", "conversation.list_saved_messages", listed)["rows"] == []
    # The erasure tombstoned the saved turn, so saving it again is refused.
    again = _chat_ok(
        gw, "bob", "conversation.save_message", {**saved, "role": "user", "content": mine}
    )
    assert (again["indexed"], again["reason"]) == (False, "deleted"), again


def test_a_create_cut_short_is_finished_by_its_creator_alone(gw: Gateway, ch_client: Any) -> None:
    import time

    name = f"col-cut-{uuid.uuid4().hex[:6]}"
    # The tree row and the creator's grant landed; the directory row did not.
    ch_client.insert(
        "loom.collections",
        [[gw.org, name, "", "user:alice", 0, time.time_ns()]],
        column_names=["org", "namespace", "parent", "owner", "deleted", "version"],
    )
    assert gw.call("bob", "collection.create", {"namespace": name}).status_code == 409
    out = gw.ok("alice", "collection.create", {"namespace": name})
    assert out["created"] is True
    assert gw.ok("alice", "collection.create", {"namespace": name})["created"] is False


def test_a_grant_row_on_a_personal_collection_opens_nothing(gw: Gateway, ch_client: Any) -> None:
    import time

    from server.core.principals import personal_name

    nonce = _nonce()
    gw.ok(
        "alice",
        "knowledge.upsert",
        {"namespace": "@me", "source_uri": "doc/mine", "content": nonce},
    )
    alices = personal_name(gw.org, "user:alice")
    ch_client.insert(
        "loom.namespace_grants",
        [[gw.org, alices, "user:bob", "admin", "test", time.time_ns(), "active"]],
        column_names=["org", "namespace", "subject", "role", "granted_by", "version", "status"],
    )
    r = gw.call("bob", "knowledge.search", {"namespace": alices, "query": nonce})
    assert r.status_code == 403
    assert gw.call("bob", "namespace.join", {"namespace": alices}).status_code in (403, 422)


def test_an_end_user_gets_no_directory_self_service(gw: Gateway) -> None:
    for op, payload in (
        ("namespace.register", {"namespace": f"mine-{uuid.uuid4().hex[:6]}", "owner_email": "x"}),
        ("namespace.join", {"namespace": "anything"}),
        ("namespace.browse", {}),
    ):
        assert gw.call("alice", op, payload).status_code == 403, op


def test_an_id_with_capitals_is_refused(gw: Gateway) -> None:
    """Ids are lowercase, so two spellings never name one user: the token itself
    does not verify."""
    assert (
        gw.call("Alice", "knowledge.search", {"namespace": "@me", "query": "q"}).status_code == 401
    )


def test_collection_list_names_each_collection_with_its_description(gw: Gateway) -> None:
    tag = uuid.uuid4().hex[:6]
    described = f"col-handbook-{tag}"
    bare = f"col-bare-{tag}"
    gw.ok(
        "alice",
        "collection.create",
        {"namespace": described, "description": "Team handbook and onboarding"},
    )
    _collection(gw, "alice", bare)
    gw.ok(
        "alice", "grants.create", {"namespace": described, "subject": "user:bob", "role": "viewer"}
    )
    mine = {c["namespace"]: c for c in gw.ok("alice", "collection.list", {})["collections"]}
    assert mine[described]["description"] == "Team handbook and onboarding"
    assert mine[bare]["description"] == ""
    bobs = {c["namespace"]: c for c in gw.ok("bob", "collection.list", {})["collections"]}
    assert bobs[described]["description"] == "Team handbook and onboarding"
    # bob reads only what his grant opens, so only that description reaches him.
    assert bare not in bobs
    assert [c["description"] for c in bobs.values() if not c["personal"]] == [
        "Team handbook and onboarding"
    ]


def test_a_deleted_collection_leaves_its_children_top_level(gw: Gateway) -> None:
    tag = uuid.uuid4().hex[:6]
    top = _collection(gw, "alice", f"col-top-{tag}")
    child = _collection(gw, "alice", f"col-child-{tag}", parent=top)
    gw.ok("alice", "grants.create", {"namespace": child, "subject": "user:bob", "role": "viewer"})
    assert gw.ok("alice", "collection.delete", {"namespace": top})["deleted"] is True
    listed = {c["namespace"]: c for c in gw.ok("bob", "collection.list", {})["collections"]}
    assert listed[child]["parent"] == "", "the child keeps its own grant, at the top"
    assert gw.call("bob", "collection.delete", {"namespace": child}).status_code == 403


def test_a_grant_planted_on_a_name_keeps_it_from_becoming_a_collection(
    gw: Gateway, ch_client: Any
) -> None:
    import time

    name = f"col-planted-{uuid.uuid4().hex[:6]}"
    ch_client.insert(
        "loom.namespace_grants",
        [[gw.org, name, "user:mallory", "admin", "test", time.time_ns(), "active"]],
        column_names=["org", "namespace", "subject", "role", "granted_by", "version", "status"],
    )
    assert gw.call("alice", "collection.create", {"namespace": name}).status_code == 409
    assert (
        gw.call("mallory", "knowledge.search", {"namespace": name, "query": "q"}).status_code == 403
    )


def test_a_malformed_collection_name_is_refused_not_crashed(gw: Gateway) -> None:
    for name in ("col-Bad_Name", "col-me-0123", "notacollection"):
        assert gw.call("alice", "collection.create", {"namespace": name}).status_code == 422, name


def test_a_deleted_collection_is_closed_even_by_name(gw: Gateway) -> None:
    col = _collection(gw, "alice", f"col-closed-{uuid.uuid4().hex[:6]}")
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "user:bob", "role": "writer"})
    nonce = _nonce()
    _add(gw, "alice", col, f"before the delete {nonce}")
    gw.ok("alice", "collection.delete", {"namespace": col})
    assert gw.call("bob", "knowledge.search", {"namespace": col, "query": nonce}).status_code == 403
    r = gw.call("bob", "knowledge.add", {"namespace": col, "source_uri": "x", "content": "y"})
    assert r.status_code == 403
    # The name is never a collection again: what it kept (its webhooks) opens nothing.
    assert gw.call("alice", "collection.create", {"namespace": col}).status_code == 409


def test_a_deleted_group_takes_no_grant_and_keeps_none(gw: Gateway) -> None:
    col = _collection(gw, "alice", f"col-gone-{uuid.uuid4().hex[:6]}")
    gw.scim("POST", "Groups", {"externalId": "temps", "members": [{"value": "bob"}]})
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "group:temps", "role": "viewer"})
    assert gw.scim("DELETE", "Groups/temps").status_code == 204
    again = gw.call(
        "alice", "grants.create", {"namespace": col, "subject": "group:temps", "role": "viewer"}
    )
    assert again.status_code == 422 and "no group" in again.text
    gw.scim("POST", "Groups", {"externalId": "temps", "members": [{"value": "bob"}]})
    search = {"namespace": col, "query": "x"}
    assert gw.call("bob", "knowledge.search", search).status_code == 403, "inherits nothing"


def _erase_user(base: str, admin: str, org: str, user_id: str) -> None:
    r = httpx.post(
        f"{base}/v1/erasure.request",
        json={
            "org": org,
            "scope_type": "user_id",
            "scope_value": user_id,
            "requester": "ops@x.io",
        },
        headers={"Authorization": f"Bearer {admin}"},
        timeout=180,
    )
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text


def _live_documents(ch_client: Any, org: str, namespace: str, uri: str) -> int:
    return ch_client.query(
        "SELECT count() FROM loom.documents FINAL WHERE org = {o:String} AND namespace = {n:String}"
        " AND source_uri = {u:String} AND superseded_at IS NULL",
        parameters={"o": org, "n": namespace, "u": uri},
    ).result_rows[0][0]


def test_a_users_erasure_never_brings_back_the_version_they_replaced(
    gw: Gateway, control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    col = _collection(gw, "alice", f"col-redo-{uuid.uuid4().hex[:6]}")
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "user:bob", "role": "writer"})
    _add(gw, "alice", col, f"alice's first draft {_nonce()}", uri="doc/plan")
    gw.ok("bob", "knowledge.upsert", {"namespace": col, "source_uri": "doc/plan", "content": "b"})
    admin = mint_test_token(org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="o")
    _erase_user(control_plane_url, admin, gw.org, "bob")
    # Bob's rewrite is gone; alice's draft it replaced does not read as live again.
    assert _live_documents(ch_client, gw.org, col, "doc/plan") == 0


def test_a_users_erasure_deletes_the_originals_no_one_else_shares(
    gw: Gateway, control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    import base64
    import hashlib

    col = _collection(gw, "alice", f"col-files-{uuid.uuid4().hex[:6]}")
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "user:bob", "role": "writer"})
    own, shared = f"bob's own {_nonce()}".encode(), f"both {_nonce()}".encode()
    for user, uri, data in (
        ("bob", "f/own", own),
        ("bob", "f/b", shared),
        ("alice", "f/a", shared),
    ):
        gw.ok(
            user,
            "knowledge.add",
            {
                "namespace": col,
                "source_uri": uri,
                "content": data.decode(),
                "original_bytes_b64": base64.b64encode(data).decode(),
                "original_filename": "f.txt",
                "blob_content_type": "text/plain",
            },
        )
    # Alice rewrites a document bob uploaded with new text and no file: his
    # original does not pass to her row, so his erasure still finds it.
    rewritten = f"bob's rewritten {_nonce()}".encode()
    gw.ok(
        "bob",
        "knowledge.add",
        {
            "namespace": col,
            "source_uri": "f/r",
            "content": rewritten.decode(),
            "original_bytes_b64": base64.b64encode(rewritten).decode(),
        },
    )
    gw.ok("alice", "knowledge.upsert", {"namespace": col, "source_uri": "f/r", "content": "new"})
    # A merge keeps only alice's version of f/r: no document row names bob's
    # upload there any more, and his erasure still finds it.
    ch_client.command("OPTIMIZE TABLE loom.documents FINAL")
    assert (
        ch_client.query(
            "SELECT count() FROM loom.documents WHERE org = {o:String} AND namespace = {n:String}"
            " AND source_uri = 'f/r' AND user_id = 'bob'",
            parameters={"o": gw.org, "n": col},
        ).result_rows[0][0]
        == 0
    ), "the merge took bob's version of f/r"
    admin = mint_test_token(org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="o")
    _erase_user(control_plane_url, admin, gw.org, "bob")

    def _stored(data: bytes) -> int:
        return ch_client.query(
            "SELECT count() FROM loom.document_blobs FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND sha256 = {s:String}",
            parameters={"o": gw.org, "n": col, "s": hashlib.sha256(data).hexdigest()},
        ).result_rows[0][0]

    assert (_stored(own), _stored(shared)) == (0, 1), "bob's own file goes; the shared one stays"
    assert _stored(rewritten) == 0, "his file under a document alice rewrote goes too"
    uploads = ch_client.query(
        "SELECT count() FROM loom.blob_uploads WHERE org = {o:String} AND user_id = 'bob'",
        parameters={"o": gw.org},
    ).result_rows[0][0]
    assert uploads == 0, "the record of what he uploaded goes with it"


def test_a_resave_of_the_same_text_keeps_the_document_its_writers(
    gw: Gateway, control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    col = _collection(gw, "alice", f"col-resave-{uuid.uuid4().hex[:6]}")
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "user:bob", "role": "writer"})
    text = f"bob's notes {_nonce()}"
    _add(gw, "bob", col, text, uri="doc/notes")
    resave = {"namespace": col, "source_uri": "doc/notes", "content": text, "tags": ["synced"]}
    gw.ok("alice", "knowledge.upsert", resave)
    admin = mint_test_token(org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="o")
    _erase_user(control_plane_url, admin, gw.org, "bob")
    assert _live_documents(ch_client, gw.org, col, "doc/notes") == 0


def test_an_erased_users_id_leaves_the_collections_and_grants_they_made(
    gw: Gateway, control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    col = _collection(gw, "bob", f"col-bobs-{uuid.uuid4().hex[:6]}")
    gw.ok("bob", "grants.create", {"namespace": col, "subject": "user:carol", "role": "viewer"})

    def _ledgered() -> int:
        return ch_client.query(
            "SELECT count() FROM loom.agent_events WHERE org = {o:String} AND subject = 'user:bob'",
            parameters={"o": gw.org},
        ).result_rows[0][0]

    wait_until(lambda: _ledgered() > 0, max_wait=30, description="bob's calls are ledgered")
    admin = mint_test_token(org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="o")
    _erase_user(control_plane_url, admin, gw.org, "bob")
    left = {
        table: ch_client.query(
            f"SELECT count() FROM {table} FINAL WHERE org = {{o:String}} AND {where}",
            parameters={"o": gw.org},
        ).result_rows[0][0]
        for table, where in (
            ("loom.namespace_grants", "granted_by = 'user:bob'"),
            ("loom.collections", "owner = 'user:bob'"),
            ("loom.namespaces", "owner_email = 'user:bob'"),
            ("loom.subject_last_seen", "subject = 'user:bob'"),
        )
    }
    assert left == dict.fromkeys(left, 0), left
    assert _ledgered() == 0, "bob's calls stay in the ledger for cost, with no subject"
    carol = gw.ok("carol", "collection.list", {})["collections"]
    assert [c["namespace"] for c in carol if not c["personal"]] == [col], "carol's grant stays"


def test_an_org_admin_erases_a_deleted_collection_and_follows_the_erasure(
    gw: Gateway, control_plane_url: str, mint_test_token: Any
) -> None:
    col = _collection(gw, "alice", f"col-old-{uuid.uuid4().hex[:6]}")
    _add(gw, "alice", col, f"to be erased {_nonce()}")
    gw.ok("alice", "collection.delete", {"namespace": col})
    admin = mint_test_token(org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="o")

    def _as_admin(op: str, body: dict[str, Any]) -> dict[str, Any]:
        r = httpx.post(
            f"{control_plane_url}/v1/{op}",
            json={"org": gw.org, **body},
            headers={"Authorization": f"Bearer {admin}"},
            timeout=180,
        )
        assert r.status_code == 200, r.text
        return r.json()

    erasure = _as_admin(
        "erasure.request", {"scope_type": "namespace", "scope_value": col, "requester": "ops"}
    )
    status = _as_admin("erasure.status", {"erasure_id": erasure["erasure_id"]})
    assert status["found"] and status["scope_value"] == col
    listed = _as_admin("erasure.list", {})
    assert erasure["erasure_id"] in {e["erasure_id"] for e in listed["rows"]}
    # A gateway's end user holding the id reads nothing of it: its reads are
    # pinned to its own rows, which a saga's status is not.
    theirs = gw.call("bob", "erasure.status", {"erasure_id": erasure["erasure_id"]})
    assert theirs.status_code == 403, theirs.text


def _download(base: str, token: str, org: str, namespace: str, uri: str) -> httpx.Response:
    return httpx.get(
        f"{base}/v1/knowledge.download",
        params={"org": org, "namespace": namespace, "source_uri": uri},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


def test_a_download_reads_as_the_end_user_the_token_names(
    gw: Gateway, control_plane_url: str, mint_test_token: Any
) -> None:
    import base64

    data = f"bob's file {_nonce()}".encode()
    gw.ok(
        "bob",
        "knowledge.add",
        {
            "namespace": "@me",
            "source_uri": "f/mine",
            "content": data.decode(),
            "original_bytes_b64": base64.b64encode(data).decode(),
            "original_filename": "mine.txt",
        },
    )
    bob, alice = end_user_issuer.mint(gw.org, "bob"), end_user_issuer.mint(gw.org, "alice")
    own = _download(control_plane_url, bob, gw.org, "@me", "f/mine")
    assert own.status_code == 200 and own.content == data
    bobs = _personal(gw, "bob")
    assert _download(control_plane_url, alice, gw.org, bobs, "f/mine").status_code == 403
    loom = mint_test_token(org=gw.org, namespaces=["*"], roles=["writer"], subject="sa:other")
    assert _download(control_plane_url, loom, gw.org, "@me", "f/mine").status_code == 400


def test_an_agents_files_reach_whoever_runs_it_and_its_search_reads_them_alone(
    gw: Gateway,
) -> None:
    """ClickHouse-Agents-style file search: an agent's files live in its own
    collection, granted to the agent; a chat's attachments in the user's @me."""
    agent_files = _collection(gw, "alice", f"col-agent-helper-{uuid.uuid4().hex[:6]}")
    other_agent = _collection(gw, "alice", f"col-agent-other-{uuid.uuid4().hex[:6]}")
    for col, agent in ((agent_files, "helper"), (other_agent, "other")):
        gw.ok(
            "alice",
            "grants.create",
            {"namespace": col, "subject": f"agent:{agent}", "role": "viewer"},
        )
    nonce = _nonce()
    _add(gw, "alice", agent_files, f"helper's handbook {nonce}")
    _add(gw, "alice", other_agent, f"other's handbook {nonce}")
    gw.ok(
        "bob",
        "knowledge.upsert",
        {
            "namespace": "@me",
            "source_uri": "chat/c7/a1",
            "content": f"attached {nonce}",
            "session_id": "c7",
        },
    )
    search = {"namespace": agent_files, "query": nonce, "namespaces": ["@me"], "top_k": 20}
    hits = gw.ok("bob", "knowledge.search", search, agent="helper")["results"]
    assert {h["namespace"] for h in hits} == {agent_files, _personal(gw, "bob")}, hits
    # Without the agent signed in, its collection is closed to bob.
    assert gw.call("bob", "knowledge.search", search).status_code == 403
    # Another agent's collection, named in the list, is refused, not searched.
    other = {**search, "namespaces": [other_agent]}
    assert gw.call("bob", "knowledge.search", other, agent="helper").status_code == 403


def _personal(gw: Gateway, user: str) -> str:
    """The name of `user`'s personal collection, which `@me` maps to."""
    from server.core.principals import personal_name

    return personal_name(gw.org, f"user:{user}")


def test_a_grant_to_everyone_opens_a_collection_to_every_user_of_the_org(gw: Gateway) -> None:
    col = _collection(gw, "alice", f"col-all-{uuid.uuid4().hex[:6]}")
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "everyone", "role": "viewer"})
    nonce = _nonce()
    _add(gw, "alice", col, f"for everyone {nonce}")
    assert any(nonce in h["content"] for h in _search(gw, "zed", col, nonce))
    assert (
        gw.call(
            "zed", "knowledge.add", {"namespace": col, "source_uri": "x", "content": "y"}
        ).status_code
        == 403
    )


def test_deleting_a_collection_erases_what_it_held(gw: Gateway, ch_client: Any) -> None:
    col = _collection(gw, "alice", f"col-gone-{uuid.uuid4().hex[:6]}")
    _add(gw, "alice", col, f"to go {_nonce()}", uri="doc/a")
    out = gw.ok("alice", "collection.delete", {"namespace": col})
    assert out["deleted"] and out["erasure_status"] == "completed", out
    left = {
        table: ch_client.query(
            f"SELECT count() FROM {table} WHERE org = {{o:String}} AND namespace = {{n:String}}",
            parameters={"o": gw.org, "n": col},
        ).result_rows[0][0]
        for table in ("loom.chunks", "loom.documents")
    }
    assert left == {"loom.chunks": 0, "loom.documents": 0}, "purged off the disk, not superseded"


def test_a_users_purge_removes_every_version_they_wrote(
    gw: Gateway, control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    col = _collection(gw, "alice", f"col-purge-{uuid.uuid4().hex[:6]}")
    gw.ok("alice", "grants.create", {"namespace": col, "subject": "user:bob", "role": "writer"})
    _add(gw, "alice", col, f"alice's draft {_nonce()}", uri="doc/plan")
    gw.ok("bob", "knowledge.upsert", {"namespace": col, "source_uri": "doc/plan", "content": "b"})
    _add(gw, "bob", col, f"bob's own {_nonce()}", uri="doc/bobs")
    admin = mint_test_token(org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="o")
    r = httpx.post(
        f"{control_plane_url}/v1/erasure.request",
        json={
            "org": gw.org,
            "scope_type": "user_id",
            "scope_value": "bob",
            "requester": "ops@x.io",
            "purge": True,
        },
        headers={"Authorization": f"Bearer {admin}"},
        timeout=180,
    )
    assert r.status_code == 200 and r.json()["status"] == "completed", r.text
    left = {
        table: ch_client.query(
            f"SELECT count() FROM {table} WHERE org = {{o:String}} AND user_id = 'bob'",
            parameters={"o": gw.org},
        ).result_rows[0][0]
        for table in ("loom.documents", "loom.chunks")
    }
    assert left == {"loom.documents": 0, "loom.chunks": 0}, left
    assert _live_documents(ch_client, gw.org, col, "doc/plan") == 0, "alice's draft stays replaced"


def test_a_document_says_which_agent_and_session_wrote_it_and_what_it_is(gw: Gateway) -> None:
    col = _collection(gw, "alice", f"col-prov-{uuid.uuid4().hex[:6]}")
    nonce = _nonce()
    gw.ok(
        "alice",
        "knowledge.add",
        {
            "namespace": col,
            "source_uri": "up",
            "content": f"uploaded {nonce}",
            "session_id": "chat-1",
        },
    )
    gw.ok(
        "alice",
        "knowledge.add",
        {
            "namespace": col,
            "source_uri": "summary",
            "content": f"summarised {nonce}",
            "session_id": "run-9",
            "source_class": "derived",
        },
        agent="helper",
    )
    hits = {
        h["source_uri"]: h
        for h in gw.ok(
            "alice",
            "knowledge.search",
            {"namespace": col, "query": nonce, "namespace_scope": "this"},
        )["results"]
    }
    assert (hits["up"]["agent"], hits["up"]["session_id"], hits["up"]["source_class"]) == (
        "-",
        "chat-1",
        "asserted",
    )
    assert (
        hits["summary"]["agent"],
        hits["summary"]["session_id"],
        hits["summary"]["source_class"],
    ) == ("helper", "run-9", "derived")
    own = gw.ok(
        "alice",
        "knowledge.search",
        {
            "namespace": col,
            "query": nonce,
            "namespace_scope": "this",
            "source_classes": ["asserted"],
        },
    )["results"]
    assert {h["source_uri"] for h in own} == {"up"}
    chat = gw.ok(
        "alice",
        "knowledge.search",
        {"namespace": col, "query": nonce, "namespace_scope": "this", "session_ids": ["run-9"]},
    )["results"]
    assert {h["source_uri"] for h in chat} == {"summary"}
    refused = gw.call(
        "alice",
        "knowledge.add",
        {"namespace": col, "source_uri": "x", "content": "y", "source_class": "observed"},
    )
    assert refused.status_code == 422, "a body only declares a downgrade"
