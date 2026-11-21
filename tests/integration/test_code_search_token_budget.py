"""Search real indexed bodies with unlimited, deployment and request budgets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def code_repo(ch_client: Any) -> AsyncIterator[Any]:
    from server.code.store.index import CodeRepo
    from server.core.ch import get_client

    client = get_client()
    await client.connect()
    try:
        yield CodeRepo()
    finally:
        await client.disconnect()


@pytest.mark.parametrize("annotate", [False, True])
async def test_search_keeps_full_bodies_unless_a_budget_is_supplied(
    code_repo: Any, monkeypatch: pytest.MonkeyPatch, annotate: bool
) -> None:
    from server.code.api.models_search import CodeSearchInput
    from server.code.app.index.ingest import ingest_file
    from server.code.app.search.handler import handle_code_search
    from server.embedding.fake import FakeNGramEmbedder
    from server.settings import settings

    org, ns = unique_org("code-budget"), "code-budget"
    embedder = FakeNGramEmbedder(dim=settings.embedding_dim)
    for i in range(5):
        source = (
            f"void retry_transport_{i}() {{\n"
            + "// retry operation after transient transport failure in connection pool\n" * 70
            + "}\n"
        ).encode()
        await ingest_file(
            code_repo,
            org,
            ns,
            path=f"src/retry{i}.cpp",
            source=source,
            commit_sha="a" * 40,
            embedding_model=embedder.model_name,
            embedder=embedder,
        )

    monkeypatch.setattr(settings, "code_search_token_budget", 0)
    request = CodeSearchInput(
        org=org,
        namespace=ns,
        query="retry transient transport connection",
        top_k=5,
        bodies="auto",
        legs="lexical",
        fresh=True,
        annotate=annotate,
        include_lessons=False,
        deadline_ms=0,
        rerank="none",
        embedding_model=embedder.model_name,
    )
    unlimited = await handle_code_search(request)
    assert not unlimited.abstain and len(unlimited.results) == 5
    assert unlimited.tokens_returned > 3000
    assert all(u.body and not u.elided for u in unlimited.results)

    for budget in (300, 1000):
        monkeypatch.setattr(settings, "code_search_token_budget", budget)
        bounded = await handle_code_search(request)
        assert bounded.results and bounded.tokens_returned <= budget
        assert any(u.elided for u in bounded.results)
    for budget in (0, 20000):
        overridden = await handle_code_search(request.model_copy(update={"token_budget": budget}))
        assert [u.body for u in overridden.results] == [u.body for u in unlimited.results]
        assert overridden.tokens_returned == unlimited.tokens_returned
