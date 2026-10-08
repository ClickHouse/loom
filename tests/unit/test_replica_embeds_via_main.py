"""A read-replica with no provider key embeds through the main Loom's
`embedding.embed`, called with a service token it mints itself; the op serves
only such a token."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.auth.jwt import mint_token, verify_token
from server.clients import main_loom
from server.embedding import embedder as emb
from server.embedding._provider import MalformedEmbedding
from server.embedding.main_loom import MainLoomEmbedder
from server.settings import settings

MAIN = "https://main.loom.test"
MODEL = "text-embedding-3-large"


@pytest.fixture
def keyless_replica(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "read_replica", True)
    monkeypatch.setattr(settings, "read_replica_main_url", MAIN)
    monkeypatch.setattr(settings, "embedding_model", MODEL)
    monkeypatch.setattr(emb, "provider_key", lambda provider: "")
    emb.reset()
    yield
    emb.reset()


@pytest.mark.usefixtures("keyless_replica")
def test_a_keyless_replica_that_knows_the_main_url_embeds_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert emb.embeds_via_main()
    got = emb.get_embedder()
    assert isinstance(got, MainLoomEmbedder)
    assert (got.model_name, got.dim) == (MODEL, settings.embedding_dim)
    # Its own key wins, and so does a missing URL or the main Loom itself.
    monkeypatch.setattr(emb, "provider_key", lambda provider: "sk-test")
    assert not emb.embeds_via_main()
    monkeypatch.setattr(emb, "provider_key", lambda provider: "")
    monkeypatch.setattr(settings, "read_replica_main_url", "")
    assert not emb.embeds_via_main()
    monkeypatch.setattr(settings, "read_replica_main_url", MAIN)
    monkeypatch.setattr(settings, "read_replica", False)
    assert not emb.embeds_via_main()


def _answers(monkeypatch: pytest.MonkeyPatch, reply: Any) -> list[tuple[str, dict[str, Any]]]:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def fake_call(op: str, body: dict[str, Any]) -> dict[str, Any]:
        sent.append((op, body))
        return reply(body)

    monkeypatch.setattr(main_loom, "call", fake_call)
    return sent


async def test_the_replica_embedder_sends_query_and_batch_texts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _answers(
        monkeypatch,
        lambda b: {"model": MODEL, "dim": 4, "vectors": [[0.5, 0.5, 0.5, 0.5]] * len(b["texts"])},
    )
    e = MainLoomEmbedder(model=MODEL, dim=4)
    assert await e.embed("a fresh query text for the replica embedder") == [0.5] * 4
    assert len(await e.embed_many([f"t{i}" for i in range(70)])) == 70
    assert [(op, b["mode"], len(b["texts"])) for op, b in sent] == [
        ("embedding.embed", "query", 1),
        ("embedding.embed", "batch", 64),
        ("embedding.embed", "batch", 6),
    ]


@pytest.mark.parametrize(
    "reply",
    [
        lambda b: {"model": "text-embedding-3-small", "dim": 4, "vectors": [[1.0] * 4]},
        lambda b: {"model": MODEL, "dim": 4, "vectors": []},
        lambda b: {"model": MODEL, "dim": 4, "vectors": [[1.0] * 3]},
    ],
    ids=["other-model", "short-count", "other-width"],
)
async def test_a_vector_the_replica_cannot_use_is_refused(
    monkeypatch: pytest.MonkeyPatch, reply: Any
) -> None:
    _answers(monkeypatch, reply)
    with pytest.raises(MalformedEmbedding):
        await MainLoomEmbedder(model=MODEL, dim=4).embed_many(["x"])


def test_the_service_token_names_a_replica_and_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_loom, "_token", None)
    token = main_loom.service_token()
    auth = verify_token(token)
    assert auth.subject.startswith("loom-replica:") and auth.is_service_account
    assert main_loom.service_token() == token
    # Close to expiry it is minted again.
    monkeypatch.setattr(main_loom, "_token", (token, 0.0))
    assert main_loom.service_token() != token


@pytest.fixture
def main_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "read_replica", False)
    monkeypatch.setattr(settings, "embedding_model", "fake-ngram-64")
    monkeypatch.setattr(settings, "embedding_dim", 64)
    emb.reset()
    from server.transport.http import build_router

    app = FastAPI()
    app.include_router(build_router())
    return TestClient(app)


def _bearer(subject: str, *, service: bool = True) -> dict[str, str]:
    token = mint_token(
        subject=subject,
        org="dev",
        namespaces=[],
        roles=["viewer"],
        ttl_seconds=300,
        is_service_account=service,
    )
    return {"Authorization": f"Bearer {token}"}


def test_the_main_loom_embeds_for_a_replica_token(main_client: TestClient) -> None:
    r = main_client.post(
        "/v1/embedding.embed",
        json={"texts": ["minmax index", "skip index"], "mode": "batch"},
        headers=_bearer("loom-replica:1234"),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "fake-ngram-64" and body["dim"] == 64
    assert [len(v) for v in body["vectors"]] == [64, 64]
    emb.reset()


@pytest.mark.parametrize(
    ("subject", "service"),
    [("agent-x", True), ("loom-replica:1234", False)],
    ids=["other-service-account", "user-token-with-replica-name"],
)
def test_the_main_loom_refuses_every_other_caller(
    main_client: TestClient, subject: str, service: bool
) -> None:
    r = main_client.post(
        "/v1/embedding.embed",
        json={"texts": ["spend someone else's budget"]},
        headers=_bearer(subject, service=service),
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "AUTH_INSUFFICIENT"
    emb.reset()


def test_embedding_embed_is_not_an_mcp_tool() -> None:
    from server.operations.reach import mcp_reach

    assert mcp_reach("embedding.embed") == set()
