"""The vector leg's arm: with no request-named model and no deployment twin
default, the served embedder reads its own rows with no shortlist."""

from __future__ import annotations

from unittest.mock import patch

from server.code.app.search.vector import _resolve_vector_arm
from server.settings import settings


class _Base:
    model_name, dim = "text-embedding-3-large", 4

    async def embed(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def test_no_model_and_no_twin_default_is_the_served_embedder() -> None:
    base = _Base()
    with patch.multiple(
        settings,
        code_search_vector_model_default="",
        code_search_vector_shortlist=500,
    ):
        vec, n, label = _resolve_vector_arm("", None, base, None)
    assert vec is base and n == 0 and label == ""
