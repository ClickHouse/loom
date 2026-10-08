"""`prefix:<dim>:<served model>` — the served embedder's vector cut to its
first `dim` coordinates and renormalised.

A Matryoshka-trained model (the OpenAI text-embedding-3 family, Gemini
embedding) is trained so that a prefix of its vector is itself an embedding
of the text in a smaller space; the shortened vector ranks nearly as the
full one does and costs a fraction of the bytes. This embedder wraps the
served one: a query is embedded once by the served provider and projected
locally, so a search that uses both spaces makes one provider call. The
label is the spec itself; it must name the served model, because the twin
rows it reads were projected from that model's vectors.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.core.vectors.prefix import parse_prefix_label, project

if TYPE_CHECKING:
    from server.embedding.embedder import Embedder


class PrefixEmbedder:
    def __init__(self, *, base: Embedder, spec: str) -> None:
        parsed = parse_prefix_label(spec)
        if parsed is None:
            raise ValueError(f"not a prefix spec: {spec!r} (expected prefix:<dim>:<model>)")
        dim, base_label = parsed
        served = base.model_name
        if served != base_label:
            raise ValueError(
                f"{spec!r} names {base_label!r} but the served embedder is {served!r}; "
                "a prefix twin is only valid for the model its rows were cut from"
            )
        if base.dim and dim > base.dim:
            raise ValueError(f"{spec!r}: {dim} exceeds the served model's {base.dim} dims")
        self.base = base
        self.model_name = spec
        self.dim = dim

    def project(self, vec: list[float]) -> list[float]:
        return project(vec, self.dim)

    async def embed(self, text: str) -> list[float]:
        return self.project(await self.base.embed(text))

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.project(v) for v in await self.base.embed_many(texts)]
