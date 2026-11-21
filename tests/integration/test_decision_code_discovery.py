"""Identifier-free prose retrieves live synthetic code, without a paid provider.

The code docstring deliberately shares the requirement vocabulary. This exercises
real hybrid retrieval and grounding, not semantic-quality measurement.
"""

from __future__ import annotations

import json

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def discovery_store(ch_client):
    from server.code.store.index import CodeRepo
    from server.core.background import drain_task_sets
    from server.core.ch import get_client

    client = get_client()
    await client.connect()
    try:
        yield CodeRepo()
    finally:
        assert await drain_task_sets(timeout_s=5)
        await client.disconnect()


async def test_prose_candidates_use_the_authorized_live_index(discovery_store, monkeypatch):
    from server.auth.context import current_auth_var
    from server.auth.jwt import AuthInfo
    from server.code.app import prose_candidates
    from server.code.app.index.ingest import ingest_file
    from server.code.app.index_status import STATUS_SNAPSHOT_KEY
    from server.embedding import embedder
    from server.embedding.fake import FakeNGramEmbedder
    from server.operations.base import Role
    from server.settings import settings

    org = unique_org("prosecandidates")
    ns, foreign = "deliveries", "other-deliveries"
    head = "a" * 40
    quote = "When a delivery fails, wait longer before each retry and stop after three attempts."
    source = f'def retry_delay(attempt):\n    """{quote}"""\n    return 2 ** attempt if attempt < 3 else None\n'
    fake = FakeNGramEmbedder(dim=settings.embedding_dim)
    monkeypatch.setattr(embedder, "get_embedder", lambda: fake)
    monkeypatch.setattr(prose_candidates, "get_embedder", lambda: fake)
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_self_service_enabled", False)
    monkeypatch.setattr(
        settings,
        "code_repos_json",
        json.dumps(
            [
                {
                    "org": org,
                    "namespace": ns,
                    "repo": "example/deliveries",
                    "git_dir": "",
                    "git_branch": "main",
                },
                {
                    "org": org,
                    "namespace": foreign,
                    "repo": "example/other-deliveries",
                    "git_dir": "",
                    "git_branch": "main",
                },
            ]
        ),
    )
    repo = discovery_store
    for namespace, path in [(ns, "src/delivery.py"), (foreign, "src/foreign.py")]:
        await ingest_file(
            repo,
            org,
            namespace,
            path=path,
            source=source.encode(),
            commit_sha=head,
            embedding_model=fake.model_name,
            embedder=fake,
        )
        await repo.set_state(org, namespace, "git_head", head)
        await repo.set_state(org, namespace, "clone_head", head, "main")
        await repo.set_state(
            org,
            namespace,
            STATUS_SNAPSHOT_KEY,
            json.dumps(
                {
                    "clone_head": head,
                    "git_head": head,
                    "fast_tier_lag": 0,
                }
            ),
        )
    auth = AuthInfo(
        subject="reader", org=org, namespaces=(ns,), roles=(Role.VIEWER,), is_service_account=False
    )
    token = current_auth_var.set(auth)
    try:
        out = await prose_candidates.discover_candidates(org, ns, quote)
        assert out.status == "candidates", (out.status, out.note, out.as_of)
        assert out.candidates and {c.path for c in out.candidates} == {"src/delivery.py"}
        assert all(quote in c.excerpt for c in out.candidates)
        assert out.as_of["commit"] == head
        denied = await prose_candidates.discover_candidates(org, foreign, quote)
        assert denied.status == "unavailable" and not denied.candidates
    finally:
        current_auth_var.reset(token)

    # The cached search envelope still says A while fetched B is partly visible.
    await repo.set_state(org, ns, "clone_head", "b" * 40, "main")
    await ingest_file(
        repo,
        org,
        ns,
        path="src/delivery.py",
        source=source.replace("2 ** attempt", "3 ** attempt").encode(),
        commit_sha="b" * 40,
        embedding_model=fake.model_name,
        embedder=fake,
    )
    partial = await prose_candidates.discover_candidates(org, ns, quote)
    assert partial.status == "deferred" and not partial.candidates
