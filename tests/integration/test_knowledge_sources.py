"""knowledge.source.* against a running server and its ClickHouse. A registry row
is seeded directly (an add needs a reachable source to probe), then read, asked
to sync and retired through the ops; the state and registry rows are read back.
Six of the eight cases need the deployment's code namespace (LOOM_CODE_ORG /
LOOM_CODE_NAMESPACE) and skip without it: the registry reads and writes and the
name rule, with documents written as the sync worker and as a caller."""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

NAME = "design-decks"
CONFIG = {"folder_ids": ["1AbCdEfGhIjKlMnOp"], "include": ["slides"]}


def _post(url: str, op: str, body: dict[str, Any]) -> httpx.Response:
    # The writes share the namespace's per-op rate limit with the other tests of
    # the shared code namespace: wait out a 429 as it asks.
    for _ in range(10):
        resp = httpx.post(f"{url}/v1/{op}", json=body, timeout=30.0)
        if resp.status_code != 429:
            return resp
        time.sleep(float(resp.json()["detail"].get("retry_after_s") or 1))
    return resp


def _seed(ch: Any, org: str, ns: str, name: str = NAME) -> None:
    ch.insert(
        "loom.knowledge_sources",
        [
            [
                org,
                ns,
                name,
                "gdrive",
                json.dumps(CONFIG),
                "not-a-sealed-value",
                "alice",
                True,
            ]
        ],
        column_names=[
            "org",
            "namespace",
            "name",
            "kind",
            "config_json",
            "credential_enc",
            "created_by",
            "active",
        ],
    )


def _query(ch: Any, sql: str, **params: Any) -> list[dict[str, Any]]:
    result = ch.query(sql, parameters=params)
    return [dict(zip(result.column_names, r, strict=True)) for r in result.result_rows]


@pytest.fixture
def tenant(ch_client: Any, repository: Any) -> tuple[str, str, str]:
    """A source seeded on the deployment's code namespace: (org, namespace, name)."""
    name = repository.name()
    _seed(ch_client, repository.org, repository.ns, name)
    return repository.org, repository.ns, name


@pytest.fixture
def orphan(ch_client: Any) -> tuple[str, str]:
    """A source whose namespace is no repository's trunk (its repository is gone)."""
    org, ns = unique_org("ksrc"), "code-ksrc-gone"
    _seed(ch_client, org, ns)
    return org, ns


def test_list_and_inspect_read_the_registry(control_plane_url: str, tenant: Any) -> None:
    org, ns, name = tenant
    listed = _post(control_plane_url, "knowledge.source.list", {"org": org, "namespace": ns})
    assert listed.status_code == 200, listed.text
    body = listed.json()
    [entry] = [e for e in body["sources"] if e["name"] == name]
    assert (entry["name"], entry["kind"], entry["config"], entry["created_by"]) == (
        name,
        "gdrive",
        CONFIG,
        "alice",
    )
    assert "credential" not in listed.text
    seen = _post(
        control_plane_url, "knowledge.source.inspect", {"org": org, "namespace": ns, "name": name}
    )
    assert seen.status_code == 200, seen.text
    assert seen.json()["documents"] == [] and seen.json()["config"] == CONFIG
    missing = _post(
        control_plane_url, "knowledge.source.inspect", {"org": org, "namespace": ns, "name": "nope"}
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "SOURCE_NOT_FOUND"


def test_sync_now_and_remove_write_state_and_registry_rows(
    control_plane_url: str, tenant: Any, ch_client: Any
) -> None:
    org, ns, name = tenant
    asked = _post(
        control_plane_url,
        "knowledge.source.sync_now",
        {"org": org, "namespace": ns, "name": name, "full": True},
    )
    assert asked.status_code == 200, asked.text
    assert asked.json() == {"name": name}
    [state] = _query(
        ch_client,
        "SELECT state_value, detail FROM loom.code_index_state FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND state_key = {key:String}",
        org=org,
        ns=ns,
        key=f"knowledge_source:{name}",
    )
    detail = json.loads(state["detail"])
    assert state["state_value"] == "" and detail["full"] is True and detail["requested_at"]
    listed = _post(control_plane_url, "knowledge.source.list", {"org": org, "namespace": ns})
    [entry] = [e for e in listed.json()["sources"] if e["name"] == name]
    assert entry["sync"]["requested_at"] == detail["requested_at"]

    wrong = {"org": org, "namespace": ns, "name": name, "confirm": "x"}
    refused = _post(control_plane_url, "knowledge.source.remove", wrong)
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "CONFIRM_REQUIRED"
    body = {"org": org, "namespace": ns, "name": name, "confirm": name, "delete_documents": True}
    removed = _post(control_plane_url, "knowledge.source.remove", body)
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"name": name, "documents_pending": 0}
    rows = _query(
        ch_client,
        "SELECT kind, config_json, credential_enc, created_by, active FROM loom.knowledge_sources FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND name = {name:String}",
        org=org,
        ns=ns,
        name=name,
    )
    assert rows == [
        {
            "kind": "gdrive",
            "config_json": json.dumps(CONFIG, sort_keys=True),
            "credential_enc": "",
            "created_by": "alice",
            "active": False,
        }
    ]
    after = _post(control_plane_url, "knowledge.source.list", {"org": org, "namespace": ns})
    assert name not in [e["name"] for e in after.json()["sources"]]


def test_check_with_no_usable_credential_fails_closed(control_plane_url: str, tenant: Any) -> None:
    org, ns, name = tenant
    out = _post(
        control_plane_url, "knowledge.source.check", {"org": org, "namespace": ns, "name": name}
    )
    assert out.status_code == 200, out.text
    assert out.json()["reachable"] is False
    assert out.json()["summary"].startswith("credential_invalid")


def test_a_source_whose_repository_is_gone_is_only_read_and_removed(
    control_plane_url: str, orphan: Any
) -> None:
    org, ns = orphan
    named = {"org": org, "namespace": ns, "name": NAME}
    for op, extra in (
        ("knowledge.source.check", {}),
        ("knowledge.source.rotate", {"credential": "{}"}),
        ("knowledge.source.sync_now", {}),
    ):
        out = _post(control_plane_url, op, {**named, **extra})
        assert out.status_code == 422, (op, out.text)
        assert out.json()["detail"]["code"] == "NOT_A_CODE_NAMESPACE", op
    assert _post(control_plane_url, "knowledge.source.inspect", named).status_code == 200
    removed = _post(control_plane_url, "knowledge.source.remove", {**named, "confirm": NAME})
    assert removed.status_code == 200, removed.text


def test_add_refuses_a_namespace_that_is_not_a_repository(control_plane_url: str) -> None:
    body = {
        "org": unique_org("ksrc"),
        "namespace": "notes",
        "name": NAME,
        "kind": "gdrive",
        "config": CONFIG,
        "credential": "{}",
    }
    out = _post(control_plane_url, "knowledge.source.add", body)
    assert out.status_code == 422, out.text
    assert out.json()["detail"]["code"] == "NOT_A_CODE_NAMESPACE"
    out = _post(control_plane_url, "knowledge.source.add", {**body, "kind": "other"})
    assert out.json()["detail"]["code"] == "BAD_KIND"


def _document(ch: Any, repo: Any, name: str, actor: str) -> str:
    """One live document tagged `source:<name>`, written as `actor`."""
    org, ns = repo.org, repo.ns
    uri = f"https://docs.example.com/{uuid.uuid4().hex}"
    repo.uris.append(uri)
    ch.insert(
        "loom.documents",
        [[org, ns, uri, "Deck", "text", [f"source:{name}"], time.time_ns(), actor]],
        column_names=[
            "org",
            "namespace",
            "source_uri",
            "title",
            "content",
            "tags",
            "version",
            "actor",
        ],
    )
    return uri


class _Repository:
    """The deployment's code namespace, and what a test wrote there: the finalizer
    deletes the documents and the registry rows, so no source row (a pending
    delete above all) outlives the test in a real repository namespace."""

    def __init__(self, org: str, ns: str) -> None:
        self.org, self.ns = org, ns
        self.names: list[str] = []
        self.uris: list[str] = []

    def name(self) -> str:
        self.names.append(f"ksrc-{uuid.uuid4().hex[:8]}")
        return self.names[-1]


@pytest.fixture
def repository(control_plane_url: str, ch_client: Any) -> Any:
    org = os.environ.get("LOOM_CODE_ORG") or pytest.skip("LOOM_CODE_ORG unset: no code repo")
    ns = os.environ.get("LOOM_CODE_NAMESPACE") or pytest.skip("LOOM_CODE_NAMESPACE unset")
    repo = _Repository(org, ns)
    yield repo
    if repo.names:
        ch_client.command(
            "DELETE FROM loom.knowledge_sources WHERE org = {org:String} "
            "AND namespace = {ns:String} AND name IN {names:Array(String)}",
            parameters={"org": org, "ns": ns, "names": repo.names},
        )
        ch_client.command(
            "DELETE FROM loom.code_index_state WHERE org = {org:String} "
            "AND namespace = {ns:String} AND state_key IN {keys:Array(String)}",
            parameters={
                "org": org,
                "ns": ns,
                "keys": [f"knowledge_source:{n}" for n in repo.names],
            },
        )
    for uri in repo.uris:
        body = {"org": org, "namespace": ns, "source_uri": uri}
        assert _post(control_plane_url, "knowledge.delete", body).status_code == 200
    left = _query(
        ch_client,
        "SELECT count() AS n FROM loom.documents FINAL WHERE org = {org:String} "
        "AND namespace = {ns:String} AND source_uri IN {uris:Array(String)} "
        "AND superseded_at IS NULL",
        org=org,
        ns=ns,
        uris=repo.uris or [""],
    )
    assert left == [{"n": 0}]


def _add(url: str, org: str, ns: str, name: str) -> httpx.Response:
    body = {
        "org": org,
        "namespace": ns,
        "name": name,
        "kind": "gdrive",
        "config": CONFIG,
        "credential": "{}",
    }
    return _post(url, "knowledge.source.add", body)


def _remove(url: str, org: str, ns: str, name: str, delete: bool) -> httpx.Response:
    body = {"org": org, "namespace": ns, "name": name, "confirm": name, "delete_documents": delete}
    return _post(url, "knowledge.source.remove", body)


@pytest.mark.parametrize(
    ("delete", "code"), [(True, "SOURCE_DELETE_PENDING"), (False, "SOURCE_DOCUMENTS_KEPT")]
)
def test_a_name_whose_sync_documents_are_live_is_refused(
    control_plane_url: str, ch_client: Any, repository: Any, delete: bool, code: str
) -> None:
    org, ns = repository.org, repository.ns
    name = repository.name()
    _seed(ch_client, org, ns, name)
    synced = _document(ch_client, repository, name, "worker:knowledge-source-sync")
    _document(ch_client, repository, name, "human:someone")  # a caller's own tagged document
    seen = _post(
        control_plane_url, "knowledge.source.inspect", {"org": org, "namespace": ns, "name": name}
    )
    assert [d["source_uri"] for d in seen.json()["documents"]] == [synced]
    removed = _remove(control_plane_url, org, ns, name, delete)
    assert removed.status_code == 200, removed.text
    assert removed.json()["documents_pending"] == (1 if delete else 0)
    [row] = _query(
        ch_client,
        "SELECT active, pending_delete, credential_enc FROM loom.knowledge_sources FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} AND name = {name:String}",
        org=org,
        ns=ns,
        name=name,
    )
    assert row == {"active": False, "pending_delete": delete, "credential_enc": ""}
    refused = _add(control_plane_url, org, ns, name)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == code


def test_a_callers_tagged_document_never_holds_a_name(
    control_plane_url: str, ch_client: Any, repository: Any
) -> None:
    org, ns = repository.org, repository.ns
    name = repository.name()
    _seed(ch_client, org, ns, name)
    _document(ch_client, repository, name, "human:someone")
    removed = _remove(control_plane_url, org, ns, name, True)
    assert removed.json()["documents_pending"] == 0
    # The name is free: the add gets past the name rule to the probe, which the
    # placeholder credential fails.
    out = _add(control_plane_url, org, ns, name)
    assert out.json()["detail"]["code"] == "CREDENTIAL_REFUSED", out.text
