"""A real document-head revocation interrupts only the fake provider leaf."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4


class _Scenario:
    def __init__(self, client, ch, org):
        from server.core.naming import new_mirror_name

        self.client, self.ch, self.org = client, ch, org
        self.namespace, self.mirror, self.source = "team", new_mirror_name(), str(uuid4())
        self.public_uri = "gdrive:checked-ask"
        self.uri = self.public_uri + "#" + self.source
        self.ordinary = "ordinaryask" + uuid4().hex
        self.private = "privateask" + uuid4().hex
        self.query = "What is the launch code?"
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.armed = self.fired = False
        self.calls = []
        self.row = {}
        self.as_of = ""

    def token(self, subject, *, service=False):
        from server.auth.jwt import mint_token

        return mint_token(
            subject,
            self.org,
            [self.namespace],
            ["viewer"],
            ttl_seconds=600,
            is_service_account=service,
            session_id=str(uuid4()),
        )

    async def rpc(self, op, token, body):
        response = await self.client.post(
            "/v1/" + op,
            json={"org": self.org, "namespace": self.namespace, **body},
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 200, response.text
        return response.json()

    async def ask(self, token):
        return await self.rpc(
            "memory.ask",
            token,
            {
                "query": self.query,
                "namespace_scope": "this",
                "search_mode": "hybrid",
                "reinforce": False,
                "depth": "excerpt",
                "precedents": False,
                "llm_cache": False,
                "decompose": "off",
                "personalization": "none",
                "personalize": False,
                "world_knowledge": "forbid",
                "as_of": self.as_of,
            },
        )

    async def document(self, namespace, uri, content, access="", tags=()):
        from server.core import pg
        from server.embedding import get_embedder
        from server.knowledge.store import pg_documents

        embedder = get_embedder()
        assert embedder.model_name.startswith("fake-")
        vector = await embedder.embed(self.query)
        row = {
            "org": self.org,
            "namespace": namespace,
            "source_uri": uri,
            "doc_id": str(uuid4()),
            "content": content,
            "title": "Checked ASK",
            "metadata": "{}",
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "tags": list(tags),
            "version": 10,
            "revision": 0,
            "superseded_at": None,
            "ingested_at": datetime.now(UTC).replace(microsecond=0),
            "access_json": access,
        }
        await self.ch.insert("loom.documents", [row])
        await self.ch.insert(
            "loom.chunks",
            [
                {
                    "org": self.org,
                    "namespace": namespace,
                    "source_uri": uri,
                    "chunk_idx": 0,
                    "content": content,
                    "doc_version": 10,
                    "chunk_metadata": json.dumps({"revision": 0}),
                    "embedding_model": embedder.model_name,
                    "embedding_dim": len(vector),
                    "embedding": vector,
                    "tags": list(tags),
                }
            ],
        )
        if pg.enabled():
            await pg_documents.commit([row], over={}, targets={uri: 1}, chunked={uri})
        return row

    async def seed(self, writer):
        from server.knowledge.domain.access_list import AccessList, to_json
        from server.knowledge.domain.sources import KnowledgeSource
        from server.knowledge.store import mirrors, sources

        await self.rpc(
            "memory.set",
            writer,
            {
                "memory_key": "launch-code",
                "value": "The launch code is " + self.ordinary,
                "agent": "checked-ask",
                "user_id": "source-owner",
                "session_id": "ordinary-session",
            },
        )
        await sources.insert_source(
            KnowledgeSource(
                org=self.org,
                namespace=self.namespace,
                name="checked",
                kind="gdrive",
                source_id=self.source,
                created_at=datetime.now(UTC),
                items=[{"type": "file", "id": "synthetic-file"}],
                access="mirrored",
                active=True,
            )
        )
        await mirrors.add_mirror(self.org, self.namespace, self.mirror)
        await mirrors.record_listing(
            self.org, self.mirror, self.source, "checked", datetime.now(UTC)
        )
        await self.document(
            self.namespace, "ordinary:visible", "The launch code is " + self.ordinary
        )
        self.row = await self.document(
            self.mirror,
            self.uri,
            "The launch code is " + self.private,
            to_json(AccessList()),
            ["source-id:" + self.source],
        )
        self.as_of = datetime.now(UTC).isoformat()

    async def permission(self, access):
        from server.core import pg
        from server.knowledge.domain.access_list import to_json
        from server.knowledge.store import pg_documents

        before = self.row
        after = {**before, "version": before["version"] + 1, "access_json": to_json(access)}
        if pg.enabled():
            await pg_documents.commit(
                [after], over={self.uri: before["version"]}, targets={self.uri: 1}
            )
            head = (await pg_documents.heads(self.org, self.mirror, [self.uri]))[self.uri].row
            assert (
                head["version"] == after["version"] and head["access_json"] == after["access_json"]
            )
        else:
            await self.ch.insert("loom.documents", [after])
            head = await self.ch.query_one(
                "SELECT version, access_json FROM loom.documents FINAL "
                "WHERE org={org:String} AND namespace={namespace:String} "
                "AND source_uri={uri:String}",
                {"org": self.org, "namespace": self.mirror, "uri": self.uri},
            )
            assert head == {"version": after["version"], "access_json": after["access_json"]}
        self.row = after

    def provider(self, original):
        async def complete(client, system, user, **kwargs):
            from server.observability.genai import current_stage

            result = await original(client, system, user, **kwargs)
            stage = str(current_stage())
            if not stage.startswith("ask_synthesize") or self.ordinary not in user:
                return result
            private = self.private in user
            self.calls.append((stage, private))
            if self.armed and private and not self.fired:
                self.fired = True
                self.entered.set()
                await self.release.wait()
            text = (self.private if private else self.ordinary) + " [m1]."
            return replace(result, text=text, tokens_out=len(text) // 4)

        return complete

    async def controls(self):
        from server.knowledge.domain.access_list import AccessList

        owner = self.token("source-owner")
        baseline = await self.ask(owner)
        assert baseline["source"] == "synthesized" and not baseline["abstain"]
        assert self.ordinary in baseline["answer"] and baseline["memory_keys"]
        assert baseline["knowledge_hits"] > 0 and self.calls and not any(p for _, p in self.calls)
        for token in (self.token("source-owner", service=True), self.token("outsider")):
            assert await self.ask(token) == baseline
        assert not any(private for _, private in self.calls)
        await self.permission(AccessList(owner="source-owner"))
        positive = await self.ask(owner)
        assert self.private in positive["answer"] and positive["knowledge_hits"] > 0
        assert self.calls[-1][1]
        return owner, baseline

    async def loss(self, owner, baseline):
        from server.knowledge.domain.access_list import AccessList

        self.armed = True
        pending = asyncio.create_task(self.ask(owner))
        try:
            await asyncio.wait_for(self.entered.wait(), timeout=45)
            assert self.fired and not pending.done()
            await self.permission(AccessList())
            ordinary_before = sum(not private for _, private in self.calls)
            self.release.set()
            result = await asyncio.wait_for(pending, timeout=45)
            assert result == baseline
            assert self.private not in json.dumps(result)
            assert sum(not private for _, private in self.calls) > ordinary_before
            assert await self.ask(owner) == baseline
        finally:
            self.release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def check_generation_loss(client, ch, org, writer):
    from unittest.mock import patch

    from server.llm.client import get_llm_client_for_model
    from server.llm.fake import FakeLLMClient
    from server.settings import settings

    assert get_llm_client_for_model(settings.memory_ask_synth_model.strip()).provider == "fake"
    scenario = _Scenario(client, ch, org)
    await scenario.seed(writer)
    with patch.object(FakeLLMClient, "complete", scenario.provider(FakeLLMClient.complete)):
        owner, baseline = await scenario.controls()
        await scenario.loss(owner, baseline)
