"""`ctx:<served model>` embeds with the served embedder and reports the alias label."""

import asyncio

import pytest

from server.embedding.alias import AliasEmbedder, parse_alias_label
from server.embedding.embedder import provider_for


class _Base:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text):
        return [1.0, 0.0, 0.0, 0.0]

    async def embed_many(self, texts):
        return [await self.embed(t) for t in texts]


def test_alias_label_parses_and_routes() -> None:
    assert parse_alias_label("ctx:text-embedding-3-large") == "text-embedding-3-large"
    assert parse_alias_label("ctxhead:text-embedding-3-large") == "text-embedding-3-large"
    assert parse_alias_label("prefix:512:x") is None and parse_alias_label("ctx:") is None
    assert provider_for("ctxhead:text-embedding-3-large") == "alias"
    assert provider_for("ctx:text-embedding-3-large") == "alias"


def test_alias_embeds_with_the_base_under_its_own_name() -> None:
    a = AliasEmbedder(base=_Base(), spec="ctx:text-embedding-3-large")
    assert a.model_name == "ctx:text-embedding-3-large" and a.dim == 4
    assert asyncio.run(a.embed("x")) == [1.0, 0.0, 0.0, 0.0]
    assert asyncio.run(a.embed_many(["x", "y"])) == [[1.0, 0.0, 0.0, 0.0]] * 2


def test_alias_over_another_model_is_refused() -> None:
    with pytest.raises(ValueError):
        AliasEmbedder(base=_Base(), spec="ctx:text-embedding-3-small")
