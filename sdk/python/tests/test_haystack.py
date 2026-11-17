"""Haystack native writers/retrievers and pipeline serialization use a Loom store."""

import base64
import json
import os

os.environ["HAYSTACK_TELEMETRY_ENABLED"] = "false"

import pytest
from haystack import Document, Pipeline
from haystack.components.retrievers import FilterRetriever
from haystack.components.writers import DocumentWriter
from haystack.dataclasses import ByteStream
from haystack.document_stores.errors import DuplicateDocumentError, MissingDocumentError
from haystack.document_stores.types import DuplicatePolicy
from haystack.utils import Secret
from loom.integrations.haystack import LoomDocumentStore, LoomRetriever


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
