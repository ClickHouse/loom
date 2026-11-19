"""Live-search configuration may only reduce the original work ceilings."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.settings.catalog import KnowledgeSettings


@pytest.mark.parametrize(
    ("name", "default", "reduced", "above", "below"),
    [
        ("knowledge_live_search_timeout_s", 5.0, 0.5, 5.1, 0),
        ("knowledge_live_search_max_sources", 3, 1, 4, 0),
        ("knowledge_live_search_max_hits", 10, 2, 11, 0),
        ("knowledge_live_search_response_max_bytes", 262144, 1024, 262145, 0),
        ("knowledge_live_search_max_items", 4, 1, 5, 0),
        ("knowledge_live_search_max_ancestors", 3, 0, 4, -1),
    ],
)
def test_live_search_limits_only_reduce_original_ceilings(
    name: str, default: int | float, reduced: int | float, above: int | float, below: int | float
) -> None:
    assert getattr(KnowledgeSettings(), name) == default
    assert getattr(KnowledgeSettings.model_validate({name: reduced}), name) == reduced
    for refused in (above, below):
        with pytest.raises(ValidationError) as caught:
            KnowledgeSettings.model_validate({name: refused})
        assert caught.value.errors()[0]["loc"] == (name,)
