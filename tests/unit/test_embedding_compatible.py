"""Wire compatibility is verified independently of any provider account."""

import httpx
import pytest

from server.embedding import compatible
from server.embedding._provider import MalformedEmbedding


def embedder(**kwargs):
    return compatible.CompatibleEmbedder(
        model="vendor/custom-embedding",
        dim=2,
        base_url="https://embedding.example/v1",
        api_key="test-only-key",
        **kwargs,
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/model",
        "https://key@example.com",
        "https://example.com?key=secret",
        "https://example.com/#secret",
        "https://example.com:99999",
    ],
)
def test_rejects_invalid_or_secret_bearing_urls(url):
    with pytest.raises(ValueError):
        compatible.endpoint_url(url)


@pytest.mark.asyncio
async def test_batch_reorders_indices_and_omits_optional_dimensions(monkeypatch):
    async def handler(request):
        import json

        assert str(request.url) == "https://embedding.example/v1/embeddings"
        assert request.headers["authorization"] == "Bearer test-only-key"
        assert json.loads(request.content) == {
            "model": "vendor/custom-embedding",
            "input": ["first", "second"],
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(compatible, "get_model_client", lambda: client)
        assert await embedder().embed_many(["first", "second"]) == [[1, 0], [0, 1]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}],
        [{"index": True, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [1]}, {"index": 1, "embedding": [0, 1]}],
        [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [0, 1]}],
    ],
)
async def test_rejects_malformed_batches(monkeypatch, entries):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": entries}))
    ) as client:
        monkeypatch.setattr(compatible, "get_model_client", lambda: client)
        with pytest.raises(MalformedEmbedding):
            await embedder().embed_many(["first", "second"])


@pytest.mark.asyncio
async def test_does_not_follow_redirects_with_provider_credentials(monkeypatch):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://other.example/embeddings"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        monkeypatch.setattr(compatible, "get_model_client", lambda: client)
        with pytest.raises(httpx.HTTPStatusError):
            await embedder()._embed_one_raw("test")
    assert set(seen) == {"https://embedding.example/v1/embeddings"}


def test_cache_identity_separates_endpoints_but_not_key_rotations():
    first = embedder()
    second = compatible.CompatibleEmbedder(
        model=first.model_name, dim=2, base_url="https://other.example/v1", api_key="test-only-key"
    )
    rotated = compatible.CompatibleEmbedder(
        model=first.model_name,
        dim=2,
        base_url="https://embedding.example/v1/",
        api_key="new-test-key",
    )
    assert first._cache_model != second._cache_model
    assert first._cache_model == rotated._cache_model


@pytest.mark.asyncio
async def test_explicit_dimensions_and_credential_probe_bypass_cache(monkeypatch):
    import json

    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(compatible, "get_model_client", lambda: client)
        provider = embedder(send_dimensions=True)
        await provider._embed_one_raw("probe")
        await provider._embed_one_raw("probe")
    assert len(calls) == 2
    assert all(body["dimensions"] == 2 for body in calls)


def test_environment_factory_selects_compatible_endpoint(monkeypatch):
    from server.embedding.embedder import _build, provider_for
    from server.settings import settings

    monkeypatch.setattr(settings, "embedding_provider", "compatible")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.example/v1")
    monkeypatch.setattr(settings, "embedding_dim", 2)
    monkeypatch.setattr(settings, "embedding_send_dimensions", True)
    monkeypatch.setenv("LOOM_EMBEDDING_API_KEY", "test-only-key")
    assert provider_for("vendor/custom-embedding") == "compatible"
    result = _build("vendor/custom-embedding", "compatible")
    assert isinstance(result, compatible.CompatibleEmbedder)
    assert result.dim == 2
    assert result._send_dimensions is True
