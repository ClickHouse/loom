"""`embedder_is_fake` says what `get_embedder` serves: the fake n-gram embedder for a fake
model or provider and for a keyed model without its key, a real one otherwise."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.embedding import embedder
from server.settings import settings


@pytest.mark.parametrize(
    ("model", "provider", "key", "fake"),
    [
        ("fake-ngram-1024", "", "k", True),
        ("text-embedding-3-small", "fake", "k", True),
        ("text-embedding-3-small", "", "", True),
        ("text-embedding-3-small", "", "k", False),
    ],
)
def test_the_answer_matches_the_embedder_served(
    monkeypatch: pytest.MonkeyPatch, model: str, provider: str, key: str, fake: bool
) -> None:
    monkeypatch.setattr(settings, "embedding_model", model)
    monkeypatch.setattr(settings, "embedding_provider", provider)
    monkeypatch.setattr(embedder, "provider_key", lambda p: key)
    if key:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setattr(embedder.gateway, "keyless", lambda: False)
    assert embedder.embedder_is_fake() is fake
    served = embedder._served(model)
    assert (type(served).__name__ == "FakeNGramEmbedder") is fake
    if key or provider == "fake" or model.startswith("fake-ngram"):
        assert served.model_name == embedder.served_model_label(model)


def test_a_model_no_provider_serves_is_not_reported_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "embedding_model", "bge-small")
    monkeypatch.setattr(settings, "embedding_provider", "")
    with pytest.raises(ValueError):
        embedder._served("bge-small")
    assert embedder.embedder_is_fake() is False


def test_local_override_shares_the_source_label_without_loading_a_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = []

    def build(name: str, provider: str):
        built.append((name, provider))
        return SimpleNamespace(model_name=name)

    monkeypatch.setattr(settings, "embedding_provider", "local")
    monkeypatch.setattr(embedder, "_build", build)
    served = embedder._served("text-embedding-3-small")
    assert built == [("local", "local")]
    assert served.model_name == embedder.served_model_label("text-embedding-3-small") == "local"
