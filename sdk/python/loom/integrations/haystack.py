"""Haystack's DocumentStore protocol and retrieval component over Loom knowledge."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Any, TypeVar, cast
from urllib.parse import urlsplit

try:
    from haystack import Document, component, default_from_dict, default_to_dict
    from haystack.document_stores.errors import DuplicateDocumentError, MissingDocumentError
    from haystack.document_stores.types import DuplicatePolicy
    from haystack.utils import Secret
    from haystack.utils.filters import document_matches_filter
except ImportError:
    raise ImportError(
        "loom.integrations.haystack needs haystack-ai: pip install 'clickhouse-loom[haystack]'"
    ) from None

from loom import SyncAgent
from loom.errors import LoomError
from loom.integrations._session_protection import _CallbackProtection
from loom.session_protection import automatic_roots


def _callback_scope(protection: _CallbackProtection) -> AbstractContextManager[None]:
    if automatic_roots():
        return protection.automatic(None)
    return protection.observing(None)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


_F = TypeVar("_F", bound=Callable[..., Any])


def _document_output(method: _F) -> _F:
    # Keep the signature when type-checking without the optional framework installed.
    return cast(_F, component.output_types(documents=list[Document])(method))


class LoomDocumentStore:
    """One store of native Haystack documents in a Loom namespace.

    Connection serialization keeps an environment-variable Secret, never a token.
    The caller's Loom deployment holds all document content and native metadata.
    Application-owned protection contributors survive fresh factory clients and
    later callbacks. An existing automatic scope guards each request; explicit
    store calls retain response observation without becoming automatic. Protection
    state stays with the application and is not serialized with the connection.
    """

    def __init__(
        self,
        org: str,
        namespace: str,
        *,
        agent: str = "haystack",
        base_url: str | None = None,
        api_key: Secret | None = None,
        store: str = "default",
    ) -> None:
        if not org or not namespace or not agent or not store:
            raise ValueError("org, namespace, agent and store must not be empty")
        if base_url is not None:
            url = urlsplit(base_url)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError(
                    "base_url must be an HTTP(S) endpoint without embedded credentials or query"
                )
        self.org = org
        self.namespace = namespace
        self.agent = agent
        self.base_url = base_url
        self.api_key = api_key or Secret.from_env_var("LOOM_API_KEY")
        self.store = store
        self._prefix = "haystack://" + _digest(store) + "/"
        self._tag = "haystack:" + _digest(store)
        self._protection = _CallbackProtection(None)

    def _client(self) -> SyncAgent:
        token = self.api_key.resolve_value()
        if not token:
            raise ValueError("The configured Loom token Secret has no value")
        return SyncAgent(
            org=self.org,
            namespace=self.namespace,
            agent=self.agent,
            base_url=self.base_url,
            api_key=token,
            consumer="haystack",
        )

    def _call(self, op: str, **fields: Any) -> dict[str, Any]:
        with _callback_scope(self._protection), self._client() as client:
            return client.call(op, fields)

    def _download(self, source_uri: str) -> bytes:
        with _callback_scope(self._protection), self._client() as client:
            data: bytes = client.knowledge.download(source_uri)
            return data

    def _uri(self, document_id: str) -> str:
        return self._prefix + _digest(document_id)

    def _get(self, source_uri: str) -> Document | None:
        result = self._call("knowledge.get", source_uri=source_uri)
        if not result.get("found"):
            return None
        if (result.get("metadata") or {}).get("haystack_document") != 1:
            raise LoomError("Stored document has no Haystack payload marker")
        return Document.from_dict(json.loads(self._download(source_uri)))

    def _documents(self) -> list[Document]:
        documents: list[Document] = []
        cursor = None
        while True:
            result = self._call("knowledge.list", tags=[self._tag], limit=500, cursor=cursor)
            for row in result.get("rows", []):
                # The URI boundary keeps a manually reused tag from crossing stores.
                if row["source_uri"].startswith(self._prefix):
                    document = self._get(row["source_uri"])
                    if document is not None:
                        documents.append(document)
            cursor = result.get("next_cursor")
            if not cursor:
                return documents

    def count_documents(self) -> int:
        return len(self._documents())

    def filter_documents(self, filters: dict[str, Any] | None = None) -> list[Document]:
        return [d for d in self._documents() if not filters or document_matches_filter(filters, d)]

    def write_documents(
        self, documents: list[Document], policy: DuplicatePolicy = DuplicatePolicy.NONE
    ) -> int:
        if policy not in (
            DuplicatePolicy.NONE,
            DuplicatePolicy.FAIL,
            DuplicatePolicy.SKIP,
            DuplicatePolicy.OVERWRITE,
        ):
            raise ValueError("Unsupported duplicate policy")
        count = 0
        for document in documents:
            if not isinstance(document, Document):
                raise TypeError("documents must contain Haystack Documents")
            uri = self._uri(document.id)
            if policy in (DuplicatePolicy.FAIL, DuplicatePolicy.NONE, DuplicatePolicy.SKIP):
                existing = self._get(uri)
                if existing is not None:
                    if policy is DuplicatePolicy.SKIP:
                        continue
                    raise DuplicateDocumentError(f"Document already exists: {document.id}")
            # Native serialization retains metadata, embeddings and binary modalities.
            self._call(
                "knowledge.upsert",
                source_uri=uri,
                content=document.content or document.id,
                tags=[self._tag],
                metadata={"haystack_document": 1},
                original_bytes_b64=base64.b64encode(
                    json.dumps(document.to_dict(flatten=False)).encode()
                ).decode(),
                original_filename="document.json",
                blob_content_type="application/json",
            )
            count += 1
        return count

    def delete_documents(self, document_ids: list[str]) -> None:
        uris = [self._uri(document_id) for document_id in dict.fromkeys(document_ids)]
        for uri in uris:
            if self._get(uri) is None:
                raise MissingDocumentError(f"Document does not exist: {uri}")
        for start in range(0, len(uris), 500):
            self._call("knowledge.bulk_delete", source_uris=uris[start : start + 500])

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = default_to_dict(
            self,
            org=self.org,
            namespace=self.namespace,
            agent=self.agent,
            base_url=self.base_url,
            api_key=self.api_key.to_dict(),
            store=self.store,
        )
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoomDocumentStore:
        data = {**data, "init_parameters": dict(data.get("init_parameters", {}))}
        if data["init_parameters"].get("api_key") is not None:
            data["init_parameters"]["api_key"] = Secret.from_dict(
                data["init_parameters"]["api_key"]
            )
        result: LoomDocumentStore = default_from_dict(cls, data)
        return result


@component
class LoomRetriever:
    """Full stored documents ranked by the best hit among up to 100 Loom chunks.

    Retain constructor and earlier callback contributors across search, get and
    download. In the application's automatic scope, protection stops later
    requests and withholds an incomplete result. Explicit retrieval keeps its
    response observers without changing ownership.
    """

    def __init__(
        self, document_store: LoomDocumentStore, top_k: int = 10, search_mode: str = "hybrid"
    ) -> None:
        if type(top_k) is not int or not 1 <= top_k <= 100:
            raise ValueError("top_k must be from 1 to 100")
        if search_mode not in ("hybrid", "semantic", "lexical"):
            raise ValueError("search_mode must be hybrid, semantic or lexical")
        self.document_store = document_store
        self.top_k = top_k
        self.search_mode = search_mode
        self._protection = _CallbackProtection(None)

    @_document_output
    def run(self, query: str, top_k: int | None = None) -> dict[str, list[Document]]:
        limit = self.top_k if top_k is None else top_k
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("top_k must be from 1 to 100")
        store = self.document_store
        with _callback_scope(store._protection), _callback_scope(self._protection):
            hits = store._call(
                "knowledge.search",
                query=query,
                tags=[store._tag],
                uri_prefix=[store._prefix],
                namespace_scope="this",
                search_mode=self.search_mode,
                top_k=100,
            )
            found: dict[str, Document] = {}
            for hit in hits.get("results", []):
                uri = hit["source_uri"]
                if uri in found or not uri.startswith(store._prefix):
                    continue
                document = store._get(uri)
                if document is not None:
                    found[uri] = replace(document, score=hit.get("score", hit.get("similarity")))
                    if len(found) == limit:
                        break
            return {"documents": list(found.values())}

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = default_to_dict(
            self,
            document_store=self.document_store.to_dict(),
            top_k=self.top_k,
            search_mode=self.search_mode,
        )
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LoomRetriever:
        data = {**data, "init_parameters": dict(data.get("init_parameters", {}))}
        data["init_parameters"]["document_store"] = LoomDocumentStore.from_dict(
            data["init_parameters"]["document_store"]
        )
        result: LoomRetriever = default_from_dict(cls, data)
        return result
