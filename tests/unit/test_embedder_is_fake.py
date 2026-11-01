"""`embedder_is_fake` says what `get_embedder` serves: the fake n-gram embedder for a fake
model or provider and for a keyed model without its key, a real one otherwise."""

from __future__ import annotations

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
