"""Check public endpoint identities without a provider, server or database."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from server.core import embedding_space


def configuration(url: str) -> dict[str, object]:
    return {
        "embedding_provider": "custom",
        "embedding_model": "synthetic-model",
        "embedding_dim": 16,
        "embedding_base_url": url,
        "embedding_send_dimensions": True,
    }


def check_input(data: bytes) -> None:
    """Malformed URLs may refuse; accepted identities must retain no URL credentials."""
    if len(data) > 4096:
        raise ValueError("The replay input exceeds the fuzzing input budget")
    try:
        result = embedding_space.public_identity(configuration(data.decode("utf-8", "replace")))
    except ValueError:
        # The URL parser explicitly rejects malformed brackets and netloc normalization.
        result = None
    if result is not None:
        assert result[:3] == ("custom", "synthetic-model", 16) and result[4] is True
        parts = urlsplit(result[3])
        assert parts.username is None and parts.password is None and not parts.fragment
        allowed = {"api-version", "api_version", "deployment", "model", "dimensions"}
        assert all(key.lower() in allowed for key, _ in parse_qsl(parts.query))
        assert embedding_space.public_identity(configuration(result[3])) == result

    # A structured branch guarantees useful accepted cases even with malformed raw bytes.
    credential = data.hex()
    url = (
        f"HTTPS://user{credential}:pass{credential}@EXAMPLE.INVALID:443/v1///"
        f"?api-version=v1&credential={credential}&MODEL=m#fragment{credential}"
    )
    public = embedding_space.public_identity(configuration(url))
    expected = (
        "custom",
        "synthetic-model",
        16,
        "https://example.invalid:443/v1?api-version=v1&MODEL=m",
        True,
    )
    assert public == expected
    rotated = url.replace(f"user{credential}:pass{credential}@", "rotated:replacement@")
    assert embedding_space.public_identity(configuration(rotated)) == public
    changed_space = url.replace("MODEL=m#", "MODEL=n#")
    assert embedding_space.public_identity(configuration(changed_space)) != public
