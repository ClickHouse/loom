"""Request fields code.search no longer takes are dropped, not refused: the model
ignores them and the transport reports them as unknown arguments."""

from __future__ import annotations

from server.code.api.models_search import CodeSearchInput
from server.transport.arg_check import unknown_args

_RETIRED = {
    "fusion": "comparable",
    "fusion_lexical_weight": 0.5,
    "lexical_scoring": "bm25",
    "lexical_bm25_k1": 1.2,
    "lexical_bm25_b": 0.75,
    "lexical_coverage_weight": 1.0,
    "file_saturation": 0.5,
    "pr_leg": "always",
    "pr_leg_cap": 0.5,
    "history_route": "on",
    "history_anchor": True,
    "query_max_tokens": 32,
    "fan_in_prior_cap": 0.25,
}


def test_retired_fields_are_ignored_and_reported() -> None:
    args = {"org": "o", "namespace": "n", "query": "q", **_RETIRED}
    inp = CodeSearchInput.model_validate(args)
    assert not set(_RETIRED) & set(inp.model_dump())
    assert unknown_args(CodeSearchInput, args) == sorted(_RETIRED)
