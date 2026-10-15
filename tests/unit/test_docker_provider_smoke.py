"""The smoke fixture is deterministic and isolates recovery from paid providers."""

import json
import threading
import urllib.error
import urllib.request

import pytest

import scripts.gates.docker_provider_data as data
import scripts.gates.docker_provider_server as fixture
import scripts.gates.docker_provider_smoke as smoke
from scripts.gates.docker_quickstart_smoke import provider_fixture


@pytest.fixture
def provider():
    server = fixture.ProviderServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def post(base, path, payload, key="docker-smoke-embedding-key"):
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def test_provider_returns_exact_repeatable_model_dimensions(provider):
    for model, dimensions in fixture.DIMENSIONS.items():
        payload = {"model": model, "input": ["same text", "other text"]}
        result = post(provider, "/v1/embeddings", payload)
        assert result == post(provider, "/v1/embeddings", payload)
        vectors = [entry["embedding"] for entry in result["data"]]
        assert [entry["index"] for entry in result["data"]] == [0, 1]
        assert all(len(vector) == dimensions for vector in vectors)
        assert vectors[0] != vectors[1]


def test_injected_failure_recovers_without_changing_model_or_data(provider):
    payload = {"model": "docker-smoke-16", "input": ["context"]}
    expected = post(provider, "/v1/embeddings", payload)
    post(provider, "/control", {"failed_model": "docker-smoke-16"})
    assert post(
        provider,
        "/v1/embeddings",
        {"model": "docker-smoke-16", "input": ["Loom provider connection check."]},
    )["data"]
    with pytest.raises(urllib.error.HTTPError) as error:
        post(provider, "/v1/embeddings", payload)
    assert error.value.code == 400
    post(provider, "/control", {"failed_model": ""})
    assert post(provider, "/v1/embeddings", payload, key="docker-smoke-rotated-key") == expected


def test_llm_transport_fixture_still_checks_credentials(provider):
    result = post(provider, "/v1/chat/completions", {}, key="docker-smoke-llm-key")
    assert result["choices"][0]["message"]["content"] == "OK"
    with pytest.raises(urllib.error.HTTPError) as error:
        post(provider, "/v1/chat/completions", {}, key="wrong")
    assert error.value.code == 401


def test_fixture_rejects_unknown_credentials(provider):
    with pytest.raises(urllib.error.HTTPError) as error:
        post(provider, "/v1/embeddings", {"model": "docker-smoke-8", "input": ["text"]}, "wrong")
    assert error.value.code == 401


def test_fixture_is_not_exposed_outside_the_generated_network(tmp_path):
    configuration = json.loads(provider_fixture("loom:unit-smoke", tmp_path).read_text())
    services = configuration["services"]
    assert services["provider-smoke"]["image"] == "loom:unit-smoke"
    assert "ports" not in services["provider-smoke"]
    assert "environment" not in services["provider-smoke"]
    assert services["loom"]["depends_on"]["provider-smoke"]["condition"] == "service_healthy"


def test_key_rotation_omission_preserves_saved_credential_and_current_version(monkeypatch):
    calls = []

    def request(base, path, payload=None):
        calls.append((path, payload))
        return {"version": "18446744073709551615"} if payload is None else {"state": "active"}

    monkeypatch.setattr(smoke, "request", request)
    smoke.configure("http://localhost", "docker-smoke-8", 8)
    payload = calls[-1][1]
    assert payload["expected_version"] == "18446744073709551615"
    assert payload["keys"] == {}


def test_failed_status_is_an_error_unless_explicitly_waiting_for_failure(monkeypatch):
    failed = {"pending": {"state": "failed", "error_code": "rebuild_rebuild_failed"}}
    monkeypatch.setattr(smoke, "request", lambda *_: failed)
    with pytest.raises(AssertionError, match="Provider rebuild failed"):
        smoke.wait_status("http://localhost", lambda _: False)
    assert (
        smoke.wait_status("http://localhost", lambda row: row["pending"]["state"] == "failed")
        == failed
    )


@pytest.mark.asyncio
async def test_data_check_requires_no_retained_rebuild_copies(monkeypatch):
    from unittest.mock import AsyncMock

    client = AsyncMock()
    client.query_one.side_effect = [
        {
            "embedding_model": "model",
            "embedding_dim": 8,
            "memory_key_embedding_dim": 8,
            "width": 8,
            "verified": 1,
            "tags": ["provider-smoke"],
            "content_excerpt": "Provider rebuild preserves metadata",
        },
        {
            "embedding_model": "model",
            "embedding_dim": 8,
            "width": 8,
            "content": "def greet(): return 42",
            "start_line": 1,
        },
        {"n": 1},
    ]
    monkeypatch.setattr(data, "get_client", lambda: client)
    with pytest.raises(AssertionError):
        await data.verify("model", 8)
