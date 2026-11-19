"""Haystack native writers/retrievers and pipeline serialization use a Loom store."""

import asyncio
import base64
import json
import os
from pathlib import Path

os.environ["HAYSTACK_TELEMETRY_ENABLED"] = "false"

import httpx
import pytest
from haystack import Document, Pipeline
from haystack.components.retrievers import FilterRetriever
from haystack.components.writers import DocumentWriter
from haystack.core.errors import PipelineRuntimeError
from haystack.dataclasses import ByteStream
from haystack.document_stores.errors import DuplicateDocumentError, MissingDocumentError
from haystack.document_stores.types import DuplicatePolicy
from haystack.utils import Secret
from loom.integrations.haystack import LoomDocumentStore, LoomRetriever
from loom.session_protection import (
    AutomaticCaptureSuppressed,
    SessionProtection,
    SessionProtectionUnavailable,
    observation_scope,
)


@pytest.fixture
def fake(monkeypatch):
    rows = {}
    calls = []

    def call(self, op, **fields):
        calls.append((op, fields, self.namespace))
        key = (self.namespace, fields.get("source_uri"))
        if op == "knowledge.upsert":
            rows[key] = json.loads(json.dumps(fields))
            return {}
        if op == "knowledge.get":
            return {"found": key in rows, **rows.get(key, {})}
        if op == "knowledge.list":
            selected = [
                r
                for (ns, _), r in rows.items()
                if ns == self.namespace and set(fields["tags"]) & set(r["tags"])
            ]
            start = int(fields.get("cursor") or 0)
            return {
                "rows": selected[start : start + 2],
                "next_cursor": str(start + 2) if start + 2 < len(selected) else None,
            }
        if op == "knowledge.bulk_delete":
            for uri in fields["source_uris"]:
                rows.pop((self.namespace, uri), None)
            return {}
        if op == "knowledge.search":
            selected = [
                r
                for (ns, _), r in rows.items()
                if ns == self.namespace
                and set(fields["tags"]) & set(r["tags"])
                and r["source_uri"].startswith(fields["uri_prefix"][0])
            ]
            return {
                "results": [
                    {"source_uri": r["source_uri"], "score": 1 - i / 100}
                    for i, r in enumerate(selected)
                    for _ in range(2)
                ]
            }
        raise AssertionError(op)

    monkeypatch.setattr(LoomDocumentStore, "_call", call)
    monkeypatch.setattr(
        LoomDocumentStore,
        "_download",
        lambda self, uri: base64.b64decode(rows[(self.namespace, uri)]["original_bytes_b64"]),
    )
    return rows, calls


def store(**kwargs):
    return LoomDocumentStore("o", "n", **kwargs)


def test_native_writer_filter_and_store_roundtrip(fake):
    db = store()
    docs = [
        Document(id=str(i), content="aisle " + str(i), meta={"category": "travel", "rank": i})
        for i in range(5)
    ]
    assert DocumentWriter(db).run(documents=docs)["documents_written"] == 5
    assert db.count_documents() == 5
    got = FilterRetriever(db).run(filters={"field": "meta.rank", "operator": ">=", "value": 3})[
        "documents"
    ]
    assert sorted(d.id for d in got) == ["3", "4"]
    assert db.filter_documents()[0].to_dict() == docs[0].to_dict()


def test_duplicates_and_delete_missing_use_native_errors(fake):
    db = store()
    first = Document(id="same", content="one")
    assert db.write_documents([first]) == 1
    with pytest.raises(DuplicateDocumentError):
        db.write_documents([first])
    assert db.write_documents([first], DuplicatePolicy.SKIP) == 0
    assert db.write_documents([Document(id="same", content="two")], DuplicatePolicy.OVERWRITE) == 1
    assert db.filter_documents()[0].content == "two"
    with pytest.raises(MissingDocumentError):
        db.delete_documents(["same", "missing"])
    assert db.count_documents() == 1
    db.delete_documents(["same", "same"])
    assert db.count_documents() == 0


def test_native_modalities_and_long_content_are_lossless(fake):
    doc = Document(
        id="binary",
        content="x" * 5000,
        blob=ByteStream(data=b"blob", mime_type="application/octet-stream"),
        embedding=[0.1, 0.2],
        meta={"nested": {"x": [1, 2]}},
    )
    db = store()
    db.write_documents([doc])
    assert db.filter_documents()[0].to_dict(flatten=False) == doc.to_dict(flatten=False)
    assert all(len(json.dumps(row["metadata"])) < 100 for row in fake[0].values())


def test_stores_and_namespaces_do_not_read_or_delete_each_other(fake):
    db = store()
    second = store(store="other")
    third = LoomDocumentStore("o", "other")
    for instance in (db, second, third):
        instance.write_documents([Document(id="same", content="same")])
    db.delete_documents(["same"])
    assert db.count_documents() == 0
    assert second.count_documents() == third.count_documents() == 1


def test_retriever_runs_in_pipeline_and_deduplicates_full_documents(fake):
    db = store()
    db.write_documents(
        [
            Document(id="one", content="aisle", meta={"type": "fact"}),
            Document(id="two", content="window"),
        ]
    )
    pipeline = Pipeline()
    pipeline.add_component("recall", LoomRetriever(db, top_k=1, search_mode="lexical"))
    result = pipeline.run({"recall": {"query": "seat"}})["recall"]["documents"]
    assert len(result) == 1 and result[0].id == "one" and result[0].score == 1
    _, calls = fake
    search = next(body for op, body, _ in calls if op == "knowledge.search")
    assert search["namespace_scope"] == "this" and search["search_mode"] == "lexical"
    assert search["top_k"] == 100 and search["uri_prefix"] == [db._prefix]


def test_pipeline_serialization_keeps_environment_secret_not_token(fake, monkeypatch):
    monkeypatch.setenv("LOOM_API_KEY", "test-token")
    pipeline = Pipeline()
    pipeline.add_component("recall", LoomRetriever(store()))
    serialized = pipeline.dumps()
    assert "LOOM_API_KEY" in serialized and "test-token" not in serialized
    from haystack.core.errors import DeserializationError

    # Third-party modules retain the framework's explicit trust boundary.
    with pytest.raises(DeserializationError, match="trusted-module allowlist"):
        Pipeline.loads(serialized)
    restored = LoomDocumentStore.from_dict(store().to_dict())
    assert restored.api_key.to_dict() == store().api_key.to_dict()
    token_store = store(api_key=Secret.from_token("test-token"))
    with pytest.raises(ValueError):
        token_store.to_dict()


@pytest.mark.parametrize("top_k", [0, 101, True, 1.5])
def test_limits_are_refused_before_request(fake, top_k):
    with pytest.raises(ValueError):
        LoomRetriever(store(), top_k=top_k)
    retriever = LoomRetriever(store())
    with pytest.raises(ValueError):
        retriever.run("q", top_k=top_k)


@pytest.mark.parametrize(
    ("component_type", "options", "inputs"),
    [
        (LoomRetriever, {"top_k": 1, "search_mode": "lexical"}, {"query": "seat"}),
        (
            FilterRetriever,
            {},
            {"filters": {"field": "meta.category", "operator": "==", "value": "travel"}},
        ),
        (
            DocumentWriter,
            {"policy": DuplicatePolicy.OVERWRITE},
            {"documents": [Document(id="written", content="Tea")]},
        ),
    ],
)
def test_explicitly_trusted_pipeline_restores_connection_and_runs(
    fake, monkeypatch, component_type, options, inputs
):
    from haystack.core.errors import DeserializationError

    monkeypatch.setenv("HAYSTACK_TEST_TOKEN", "offline-test-token")
    db = LoomDocumentStore(
        "saved-org",
        "saved-namespace",
        agent="saved-agent",
        base_url="https://loom.example",
        api_key=Secret.from_env_var("HAYSTACK_TEST_TOKEN"),
        store="travel",
    )
    db.write_documents([Document(id="one", content="Aisle", meta={"category": "travel"})])
    component = component_type(db, **options)
    pipeline = Pipeline()
    pipeline.add_component("node", component)
    serialized = pipeline.dumps()
    assert "HAYSTACK_TEST_TOKEN" in serialized and "offline-test-token" not in serialized
    restored = Pipeline.loads(serialized, allowed_modules=["loom.integrations.haystack"])
    assert restored.get_component("node").to_dict() == component.to_dict()
    result = restored.run({"node": inputs})["node"]
    if component_type is DocumentWriter:
        assert result == {"documents_written": 1}
        assert {doc.id for doc in db.filter_documents()} == {"one", "written"}
    else:
        assert [doc.id for doc in result["documents"]] == ["one"]
    with pytest.raises(DeserializationError, match="trusted-module allowlist"):
        Pipeline.loads(serialized)


class _SDKWire:
    """The real factory's HTTP boundary; native documents remain serialized losslessly."""

    def __init__(self, monkeypatch):
        self.documents = {}
        self.requests = []
        self.clients = []
        self.mirrored = False
        self.auth = None
        self.on_response = None
        self.page_size = 1
        real_client = httpx.AsyncClient
        mock = httpx.MockTransport(self.respond)

        def client(*args, **kwargs):
            kwargs["transport"] = mock
            kwargs["trust_env"] = False
            if self.auth is not None:
                kwargs["auth"] = self.auth
            result = real_client(*args, **kwargs)
            assert (
                result._transport_for_url(httpx.URL("http://inert.invalid/v1/knowledge.search"))
                is mock
            )
            self.clients.append(result)
            return result

        monkeypatch.setenv("NO_PROXY", "*")
        monkeypatch.setenv("no_proxy", "*")
        monkeypatch.setenv("LOOM_MENTOR_BRIEF", "off")
        monkeypatch.setenv("LOOM_AUTO_RUN", "off")
        monkeypatch.setenv("HAYSTACK_TEST_TOKEN", "offline-test-token")
        monkeypatch.setattr(httpx, "AsyncClient", client)

    def seed(self, db, document):
        self.documents[db._uri(document.id)] = document.to_dict(flatten=False)

    def respond(self, request):
        path = request.url.path
        fields = (
            dict(request.url.params) if request.method == "GET" else json.loads(request.content)
        )
        self.requests.append((request.method, path, fields))
        if self.on_response is not None:
            self.on_response(path)
        if path == "/v1/knowledge.search":
            results = [
                {"source_uri": uri, "score": 1 - index / 10}
                for index, uri in enumerate(self.documents)
                if uri.startswith(fields["uri_prefix"][0])
                for _ in range(2)
            ]
            return httpx.Response(200, json={"mirrored": self.mirrored, "results": results})
        if path == "/v1/knowledge.list":
            start = int(fields.get("cursor") or 0)
            uris = list(self.documents)
            end = start + self.page_size
            return httpx.Response(
                200,
                json={
                    "mirrored": self.mirrored,
                    "rows": [{"source_uri": uri} for uri in uris[start:end]],
                    "next_cursor": str(end) if end < len(uris) else None,
                },
            )
        if path == "/v1/knowledge.get":
            return httpx.Response(
                200,
                json={
                    "mirrored": self.mirrored,
                    "found": fields["source_uri"] in self.documents,
                    "metadata": {"haystack_document": 1},
                },
            )
        if path == "/v1/knowledge.download":
            return httpx.Response(200, json=self.documents[fields["source_uri"]])
        if path == "/v1/knowledge.upsert":
            self.documents[fields["source_uri"]] = json.loads(
                base64.b64decode(fields["original_bytes_b64"])
            )
            return httpx.Response(
                200,
                json={
                    "doc_id": "11111111-1111-4111-8111-111111111111",
                    "chunks_created": 1,
                    "chunks_superseded": 0,
                    "embedding_model": "test-model",
                    "embedding_dim": 2,
                },
            )
        assert path == "/v1/knowledge.bulk_delete"
        for uri in fields["source_uris"]:
            self.documents.pop(uri, None)
        return httpx.Response(
            200,
            json={
                "deleted": [
                    {"source_uri": uri, "chunks_tombstoned": 1} for uri in fields["source_uris"]
                ],
                "total_chunks_tombstoned": len(fields["source_uris"]),
            },
        )

    def assert_closed(self):
        assert self.clients
        assert all(client.is_closed for client in self.clients)
        assert len({id(client) for client in self.clients}) == len(self.clients)


@pytest.fixture
def wire(monkeypatch):
    boundary = _SDKWire(monkeypatch)
    yield boundary
    assert all(client.is_closed for client in boundary.clients)


def _wire_store():
    return LoomDocumentStore(
        "acme",
        "support",
        base_url="http://inert.invalid",
        api_key=Secret.from_env_var("HAYSTACK_TEST_TOKEN"),
        store="travel",
    )


def _document():
    return Document(
        id="seat",
        content="Alice prefers aisle seats",
        meta={"category": "travel", "nested": {"rank": [1, 2]}},
        embedding=[0.1, 0.2],
        blob=ByteStream(data=b"native-bytes", mime_type="application/octet-stream"),
    )


@pytest.mark.parametrize("owner", ["store", "retriever"])
def test_automatic_retrieval_retains_constructor_observers_across_fresh_clients(
    wire, tmp_path: Path, owner
):
    original = SessionProtection.create(base=tmp_path / "original")
    producer = SessionProtection.create(base=tmp_path / "producer")
    if owner == "store":
        with observation_scope((original,)):
            db = _wire_store()
        retriever = LoomRetriever(db)
    else:
        db = _wire_store()
        with observation_scope((original,)):
            retriever = LoomRetriever(db)
    wire.seed(db, _document())
    original.mark()
    with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
        retriever.run("seat")
    assert wire.requests == []
    assert not producer.protected


def test_native_pipeline_preserves_retained_suppression_as_its_primary_cause(wire, tmp_path: Path):
    original = SessionProtection.create(base=tmp_path / "original")
    producer = SessionProtection.create(base=tmp_path / "producer")
    with observation_scope((original,)):
        db = _wire_store()
    wire.seed(db, _document())
    pipeline = Pipeline()
    pipeline.add_component("recall", LoomRetriever(db))
    original.mark()
    with producer.automatic(), pytest.raises(PipelineRuntimeError) as caught:
        pipeline.run({"recall": {"query": "seat"}})
    assert isinstance(caught.value.__cause__, AutomaticCaptureSuppressed)
    assert wire.requests == []
    assert not producer.protected


@pytest.mark.parametrize("earlier_callback", ["filter", "retrieve"])
def test_later_automatic_retrieval_retains_an_earlier_native_contributor(
    wire, tmp_path: Path, earlier_callback
):
    earlier = SessionProtection.create(base=tmp_path / "earlier")
    producer = SessionProtection.create(base=tmp_path / "producer")
    db = _wire_store()
    retriever = LoomRetriever(db)
    document = _document()
    wire.seed(db, document)
    with observation_scope((earlier,)):
        if earlier_callback == "filter":
            found = FilterRetriever(db).run()["documents"]
        else:
            found = retriever.run("seat")["documents"]
    assert found[0].content == document.content
    before = list(wire.requests)
    earlier.mark()
    with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
        retriever.run("seat")
    assert wire.requests == before
    assert not producer.protected
    wire.assert_closed()


def test_retained_factory_control_survives_authentication_await(wire, tmp_path: Path):
    original = SessionProtection.create(base=tmp_path / "original")
    producer = SessionProtection.create(base=tmp_path / "producer")
    with observation_scope((original,)):
        db = _wire_store()
    wire.seed(db, _document())

    class MarkingAuth(httpx.Auth):
        async def async_auth_flow(self, request):
            await asyncio.sleep(0)
            original.mark()
            yield request

    wire.auth = MarkingAuth()
    with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
        LoomRetriever(db).run("seat")
    assert wire.requests == []
    assert not producer.protected
    wire.assert_closed()


@pytest.mark.parametrize("mark_after", ["knowledge.search", "knowledge.get"])
def test_mark_between_native_retrieval_steps_prevents_later_document_requests(
    wire, tmp_path: Path, mark_after
):
    original = SessionProtection.create(base=tmp_path / "original")
    producer = SessionProtection.create(base=tmp_path / "producer")
    with observation_scope((original,)):
        db = _wire_store()
    wire.seed(db, _document())

    def mark(path):
        if path == "/v1/" + mark_after:
            original.mark()

    wire.on_response = mark
    returned = []
    with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
        returned.extend(LoomRetriever(db).run("seat")["documents"])
    assert returned == []
    expected = ["/v1/knowledge.search"]
    if mark_after == "knowledge.get":
        expected.append("/v1/knowledge.get")
    assert [path for _, path, _ in wire.requests] == expected
    assert not producer.protected
    wire.assert_closed()


def test_paged_native_filter_observes_retained_families_before_returning_documents(
    wire, tmp_path: Path
):
    original = SessionProtection.create(base=tmp_path / "original")
    entry = SessionProtection.create(base=tmp_path / "entry")
    with observation_scope((original,)):
        db = _wire_store()
    first = _document()
    second = Document(id="other", content="Another document", meta={"category": "travel"})
    wire.seed(db, first)
    wire.seed(db, second)
    wire.mirrored = True
    with observation_scope((entry,)):
        found = FilterRetriever(db).run()["documents"]
    assert [d.to_dict(flatten=False) for d in found] == [
        first.to_dict(flatten=False),
        second.to_dict(flatten=False),
    ]
    assert original.protected and entry.protected
    assert [path for _, path, _ in wire.requests] == [
        "/v1/knowledge.list",
        "/v1/knowledge.get",
        "/v1/knowledge.download",
        "/v1/knowledge.list",
        "/v1/knowledge.get",
        "/v1/knowledge.download",
    ]
    assert len(wire.clients) == 6
    wire.assert_closed()


def test_failed_retained_mark_attempts_all_native_contributors_and_withholds_result(
    wire, tmp_path: Path, monkeypatch
):
    broken = SessionProtection.create(base=tmp_path / "broken")
    earlier = SessionProtection.create(base=tmp_path / "earlier")
    current = SessionProtection.create(base=tmp_path / "current")
    entry = SessionProtection.create(base=tmp_path / "entry")
    with observation_scope((broken,)):
        db = _wire_store()
    retriever = LoomRetriever(db)
    wire.seed(db, _document())
    with observation_scope((earlier,)):
        assert retriever.run("seat")["documents"][0].id == "seat"
    wire.requests.clear()
    mark = SessionProtection.mark
    attempted = []

    def fail_one(family):
        attempted.append(family.id)
        if family == broken:
            raise SessionProtectionUnavailable()
        mark(family)

    monkeypatch.setattr(SessionProtection, "mark", fail_one)
    wire.mirrored = True
    returned = []
    with observation_scope((current, entry)), pytest.raises(SessionProtectionUnavailable):
        returned.extend(retriever.run("seat")["documents"])
    assert returned == []
    assert set(attempted) == {broken.id, earlier.id, current.id, entry.id}
    assert earlier.protected and current.protected and entry.protected
    assert [path for _, path, _ in wire.requests] == ["/v1/knowledge.search"]
    wire.assert_closed()


def test_first_marked_native_query_withholds_followups_and_marks_retained_family(
    wire, tmp_path: Path
):
    original = SessionProtection.create(base=tmp_path / "original")
    producer = SessionProtection.create(base=tmp_path / "producer")
    with observation_scope((original,)):
        db = _wire_store()
    wire.seed(db, _document())
    wire.mirrored = True
    returned = []
    with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
        returned.extend(LoomRetriever(db).run("seat")["documents"])
    assert returned == []
    assert original.protected and producer.protected
    assert [path for _, path, _ in wire.requests] == ["/v1/knowledge.search"]
    wire.assert_closed()


@pytest.mark.parametrize("operation", ["write", "filter", "delete", "retrieve"])
def test_explicit_native_document_operations_keep_ownership_and_retained_observers(
    wire, tmp_path: Path, operation
):
    protected = SessionProtection.create(base=tmp_path / "protected")
    observer = SessionProtection.create(base=tmp_path / "observer")
    with observation_scope((protected, observer)):
        db = _wire_store()
    document = _document()
    wire.seed(db, document)
    protected.mark()
    wire.mirrored = operation in ("filter", "retrieve")
    if operation == "write":
        assert DocumentWriter(db, policy=DuplicatePolicy.OVERWRITE).run(documents=[document]) == {
            "documents_written": 1,
        }
        _, path, body = wire.requests[0]
        assert path == "/v1/knowledge.upsert"
        assert body["metadata"] == {"haystack_document": 1}
        assert json.loads(base64.b64decode(body["original_bytes_b64"])) == document.to_dict(
            flatten=False
        )
    elif operation == "delete":
        assert db.delete_documents([document.id]) is None
        assert wire.requests[-1][1] == "/v1/knowledge.bulk_delete"
        assert wire.requests[-1][2]["source_uris"] == [db._uri(document.id)]
        assert wire.documents == {}
    else:
        found = (
            FilterRetriever(db).run() if operation == "filter" else LoomRetriever(db).run("seat")
        )["documents"]
        assert found[0].content == document.content
        assert found[0].blob.data == b"native-bytes"
        assert found[0].meta == document.meta
    assert observer.protected is (operation in ("filter", "retrieve"))
    assert protected.protected
    wire.assert_closed()


def test_clean_native_pipeline_keeps_full_document_payload_and_closes_each_factory_client(wire):
    db = _wire_store()
    document = _document()
    writer = Pipeline()
    writer.add_component("write", DocumentWriter(db, policy=DuplicatePolicy.OVERWRITE))
    assert writer.run({"write": {"documents": [document]}})["write"] == {"documents_written": 1}
    query = Pipeline()
    query.add_component("recall", LoomRetriever(db, top_k=1, search_mode="lexical"))
    result = query.run({"recall": {"query": "seat"}})["recall"]["documents"]
    assert len(result) == 1
    restored = result[0].to_dict(flatten=False)
    restored["score"] = None
    assert restored == document.to_dict(flatten=False)
    assert result[0].score == 1
    search = next(body for _, path, body in wire.requests if path == "/v1/knowledge.search")
    assert {
        key: search[key]
        for key in ("query", "tags", "uri_prefix", "namespace_scope", "search_mode", "top_k")
    } == {
        "query": "seat",
        "tags": [db._tag],
        "uri_prefix": [db._prefix],
        "namespace_scope": "this",
        "search_mode": "lexical",
        "top_k": 100,
    }
    assert [path for _, path, _ in wire.requests] == [
        "/v1/knowledge.upsert",
        "/v1/knowledge.search",
        "/v1/knowledge.get",
        "/v1/knowledge.download",
    ]
    assert len(wire.clients) == 4
    wire.assert_closed()


def test_store_serialization_keeps_secret_contract_and_application_owned_restore(
    wire, tmp_path: Path
):
    family = SessionProtection.create(base=tmp_path / "family")
    with observation_scope((family,)):
        db = _wire_store()
    serialized = db.to_dict()
    assert set(serialized["init_parameters"]) == {
        "org",
        "namespace",
        "agent",
        "base_url",
        "api_key",
        "store",
    }
    assert "HAYSTACK_TEST_TOKEN" in json.dumps(serialized)
    assert "offline-test-token" not in json.dumps(serialized)
    assert family.id not in json.dumps(serialized) and str(family.base) not in json.dumps(
        serialized
    )
    with observation_scope((family,)):
        restored = LoomDocumentStore.from_dict(serialized)
    assert restored.to_dict() == serialized
    wire.seed(restored, _document())
    family.mark()
    producer = SessionProtection.create(base=tmp_path / "producer")
    with producer.automatic(), pytest.raises(AutomaticCaptureSuppressed):
        LoomRetriever(restored).run("seat")
    assert wire.requests == []
    assert not producer.protected
