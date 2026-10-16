"""The identity of an embedding space: what makes two embedders' vectors comparable.

Two services can serve the same model label at the same width and still produce
different vectors, so the space is the provider, the model, the width, the
endpoint and whether the width is sent with each request. Rebuild recipes, the
deployment's MUST_MATCH check and the shared similarity calibration all key on it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

Identity = tuple[str, str, int, str, bool]

# The settings that make up an identity, in its order.
FIELDS = (
    "embedding_provider",
    "embedding_model",
    "embedding_dim",
    "embedding_base_url",
    "embedding_send_dimensions",
)


def inferred_provider(model: str) -> str:
    """The provider a served model name implies: fake, local, openai or gemini."""
    if model.startswith("fake-ngram"):
        return "fake"
    if model == "local" or model.startswith("local:"):
        return "local"
    if model.startswith("text-embedding-"):
        return "openai"
    if model.startswith("gemini"):
        return "gemini"
    raise ValueError(f"unknown embedding model: {model!r}")


def identity(values: Mapping[str, Any]) -> Identity:
    """The identity `values` (settings, or a dump of them) describe. An empty
    provider is the one the model implies ('' for a name none does); the
    endpoint's trailing slash is not part of it. Rebuild recipes store its
    fingerprint: it must not change for a given configuration."""
    model = str(values["embedding_model"])
    provider = str(values["embedding_provider"])
    if not provider:
        try:
            provider = inferred_provider(model)
        except ValueError:
            provider = ""
    return (
        provider,
        model,
        int(values["embedding_dim"]),
        str(values["embedding_base_url"]).rstrip("/"),
        bool(values["embedding_send_dimensions"]),
    )


# The query parameters that select a space; every other one (an `apikey`, a
# `sig`, an `auth`) is dropped: an unknown one at worst merges two spaces.
_SPACE_PARAMS = frozenset({"api-version", "api_version", "deployment", "model", "dimensions"})


def public_identity(values: Mapping[str, Any]) -> Identity:
    """`identity`, its endpoint without credentials (userinfo, any query parameter
    but `_SPACE_PARAMS`) or fragment, scheme and host lowercased: what may be
    published and compared across replicas, and what a key rotated inside the
    URL does not change. Rebuild recipes keep the exact `identity`."""
    provider, model, dim, url, send = identity(values)
    if url:
        parts = urlsplit(url)
        netloc = parts.netloc.rsplit("@", 1)[-1].lower()  # host and port as written
        query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k.lower() in _SPACE_PARAMS])
        url = urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), query, ""))
    return provider, model, dim, url, send


def fingerprint(
    provider: str, model: str, dimensions: int, base_url: str, send_dimensions: bool
) -> str:
    """sha256 of an identity."""
    return hashlib.sha256(
        json.dumps(
            [provider, model, dimensions, base_url.rstrip("/"), send_dimensions],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
