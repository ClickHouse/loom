"""Knowledge sharing end to end (docs/architecture/sharing.md), against a live
server: a gateway's end users, collections and their inheritance, SCIM groups,
saved items in `@me`, revocation, and a user's erasure.

Skips without LOOM_BASE_URL."""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.integration._eventually import ACL_OPS, acl_change_adopted, wait_until
from tests.integration._resources import unique_org

pytestmark = pytest.mark.slow


def _gateway_token(org: str) -> str:
    from server.auth.jwt import mint_token

    return mint_token("sa:chat", org, [], [], gateway=True, ttl_seconds=900)


class Gateway:
    """The service in front of Loom: one token for its org, a user per call."""

    def __init__(self, base: str, org: str) -> None:
        self.base, self.org, self.token = base, org, _gateway_token(org)

    def call(
        self, user: str, op: str, payload: dict[str, Any], *, agent: str = ""
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.token}", "X-Loom-On-Behalf-Of": f"user:{user}"}
        if agent:
            headers["X-Loom-Agent"] = f"agent:{agent}"
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
            headers={"Authorization": f"Bearer {self.token}"},
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
def gw(control_plane_url: str) -> Gateway:
    return Gateway(control_plane_url, unique_org("share"))


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


def test_no_request_field_makes_a_saved_item_or_widens_a_grant(
    gw: Gateway, control_plane_url: str, mint_test_token: Any
) -> None:
    from server.core.principals import personal_name as name_for

    nonce = _nonce()
    gw.ok(
        "alice",
        "knowledge.upsert",
        {"namespace": "@me", "source_uri": "chat/c1/m1", "content": nonce},
    )
    alices = name_for(gw.org, "user:alice")
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
    # A gateway asserts users and agents only.
    for header in ("group:finance", "alice@example.com", "sa:chat", ""):
        r = httpx.post(
            f"{control_plane_url}/v1/knowledge.search",
            json={"org": gw.org, "namespace": "@me", "query": nonce},
            headers={"Authorization": f"Bearer {gw.token}", "X-Loom-On-Behalf-Of": header},
        )
        assert r.status_code == 403, header
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


def test_a_long_saved_message_is_chunked_whole_and_its_tail_is_found(gw: Gateway) -> None:
    tail = _nonce()
    body = ("The renewal clause covers notice periods and pricing. " * 380) + f"Final note {tail}."
    assert len(body) > 20_000
    gw.ok(
        "alice",
        "knowledge.upsert",
        {"namespace": "@me", "source_uri": "chat/c9/m9", "content": body},
    )
    rows = gw.ok("alice", "knowledge.list", {"namespace": "@me", "uri_prefix": ["chat/c9/"]})[
        "rows"
    ]
    assert rows and rows[0]["chunk_count"] > 10
    for mode in ("lexical", "hybrid"):
        hits = gw.ok(
            "alice",
            "knowledge.search",
            {"namespace": "@me", "query": tail, "search_mode": mode, "namespace_scope": "this"},
        )["results"]
        assert hits and hits[0]["source_uri"] == "chat/c9/m9" and tail in hits[0]["content"], mode


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
        "bob", "knowledge.upsert", {"namespace": "@me", "source_uri": "chat/c1/m1", "content": mine}
    )
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

    # The org admin may erase bob's rows in collections it cannot read, not read them.
    assert _exported(admin) == set()
    everything = _exported(root)
    assert (col, bobs) in everything and {u for _, u in everything} == {bobs, "chat/c1/m1"}
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
        {"namespace": "@me", "source_uri": "chat/c2/m1", "content": "new"},
    )
    left = gw.ok(
        "bob", "knowledge.search", {"namespace": "@me", "query": mine, "namespace_scope": "this"}
    )
    assert not any(mine in h["content"] for h in left["results"])


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
        {"namespace": "@me", "source_uri": "chat/c/m", "content": nonce},
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
    assert (
        gw.call("Alice", "knowledge.search", {"namespace": "@me", "query": "q"}).status_code == 403
    )


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
    admin = mint_test_token(org=gw.org, namespaces=["*"], roles=["org_admin", "admin"], subject="o")
    _erase_user(control_plane_url, admin, gw.org, "bob")

    def _stored(data: bytes) -> int:
        return ch_client.query(
            "SELECT count() FROM loom.document_blobs FINAL WHERE org = {o:String}"
            " AND namespace = {n:String} AND sha256 = {s:String}",
            parameters={"o": gw.org, "n": col, "s": hashlib.sha256(data).hexdigest()},
        ).result_rows[0][0]

    assert (_stored(own), _stored(shared)) == (0, 1), "bob's own file goes; the shared one stays"


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
