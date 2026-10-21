"""The model client (server/clients/_http.py), its hooks in place, under a test.

    client = through_the_model_client(monkeypatch)
    monkeypatch.setattr(client, "_transport", httpx.MockTransport(answer))

Every model call then goes through `_on_request` and `_on_response` to `answer`,
as in production: the gateway check, the identity headers kept to the gateway's
hosts, the gateway's bearer and its identity holds.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from server.clients import _http


def through_the_model_client(monkeypatch: pytest.MonkeyPatch) -> AsyncClient:
    """A fresh model client, its hooks in place, that every sender posts through: a
    sender first imported while a test patched `get_model_client` keeps that patch
    under its own name, so each is pointed back here. The reranker and classifier
    read `_http.get_model_client()` at each call."""
    from server.embedding import compatible, gemini
    from server.embedding import openai as embed_openai
    from server.llm import anthropic
    from server.llm import openai as chat_openai

    monkeypatch.setattr(_http, "_model_client", None)  # restored after the test
    for mod in (chat_openai, anthropic, embed_openai, gemini):
        monkeypatch.setattr(mod, "_http_client", _http.get_model_client)
    monkeypatch.setattr(compatible, "get_model_client", _http.get_model_client)
    return _http.get_model_client()
