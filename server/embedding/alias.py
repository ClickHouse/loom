"""An alias label over the served embedder.

`ctx:<served model>` names rows whose *text* differs from the served
label's (a chunk's context text rewritten with a question-oriented
description) while the vectors come from the same model. The alias embeds
with the served embedder and reports the alias as its `model_name`, so the
vector leg reads the alias's rows (`embedding_model = 'ctx:…'`, same table,
same width) and a query vector from the same space.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from server.embedding.embedder import Embedder

PREFIX = "ctx"
_LABEL_RE = re.compile(r"^ctx[A-Za-z0-9_-]*:(.+)$")


def parse_alias_label(spec: str) -> str | None:
    """`ctx:<base>` or `ctx<variant>:<base>` -> `<base>`; None when `spec` is
    not an alias label. The variant tag names a text rewrite (`ctxhead:` for
    the head-weighted text) — the vectors are the base model's either way."""
    m = _LABEL_RE.match((spec or "").strip())
    return m.group(1) if m else None


class AliasEmbedder:
    def __init__(self, *, base: Embedder, spec: str) -> None:
        base_label = parse_alias_label(spec)
        if base_label is None:
            raise ValueError(f"not an alias spec: {spec!r} (expected ctx[<variant>]:<model>)")
        served = base.model_name
        if served != base_label:
            raise ValueError(
                f"{spec!r} names {base_label!r} but the served embedder is {served!r}; "
                "an alias label is only valid over the model its rows were embedded with"
            )
        self.base = base
        self.model_name = spec
        self.dim = base.dim

    async def embed(self, text: str) -> list[float]:
        return await self.base.embed(text)

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return await self.base.embed_many(texts)
