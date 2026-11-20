"""Native Haystack documents and retrieval against an explicit private Loom."""

import pytest

pytest.importorskip("haystack")
from haystack import Document, Pipeline
from haystack.components.writers import DocumentWriter
from haystack.document_stores.types import DuplicatePolicy
from haystack.utils import Secret
from loom.integrations.haystack import LoomDocumentStore, LoomRetriever

from tests.integration._resources import unique_org


def test_haystack_writes_filters_retrieves_and_deletes(control_plane_url, mint_test_token):
    org = unique_org("haystack")
    token = mint_test_token(org=org, namespaces=["documents"], roles=["writer"])
    store = LoomDocumentStore(
        org,
        "documents",
        base_url=control_plane_url,
        api_key=Secret.from_token(token),
    )
    docs = [
        Document(
            id="travel",
            content="Alice prefers aisle seats on flights.",
            meta={"category": "travel"},
        ),
        Document(id="food", content="Tea is served with meals.", meta={"category": "food"}),
    ]
    assert DocumentWriter(store).run(documents=docs)["documents_written"] == 2
    assert store.count_documents() == 2
    [travel] = store.filter_documents(
        {"field": "meta.category", "operator": "==", "value": "travel"}
    )
    assert travel.to_dict(flatten=False) == docs[0].to_dict(flatten=False)
    pipeline = Pipeline()
    pipeline.add_component("recall", LoomRetriever(store, search_mode="lexical"))
    found = pipeline.run({"recall": {"query": "aisle seats"}})["recall"]["documents"]
    assert any(doc.id == "travel" and "aisle" in doc.content for doc in found)
    assert all(doc.id in {"travel", "food"} for doc in found)
    updated = Document(
        id="travel", content="Alice prefers window seats.", meta={"category": "travel"}
    )
    assert store.write_documents([updated], DuplicatePolicy.OVERWRITE) == 1
    assert (
        store.filter_documents({"field": "id", "operator": "==", "value": "travel"})[0].content
        == updated.content
    )
    other = LoomDocumentStore(
        org,
        "documents",
        store="other",
        base_url=control_plane_url,
        api_key=Secret.from_token(token),
    )
    other.write_documents([Document(id="travel", content="Other application document.")])
    store.delete_documents(["travel", "food"])
    assert store.count_documents() == 0 and other.count_documents() == 1
    other.delete_documents(["travel"])
