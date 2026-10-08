"""Coverage is judged against this call's heads, not the cache's."""

from __future__ import annotations

from server.code.api.models_status import CodeCompilerTier, CodeIndexStatusOutput
from server.code.app.index_status import _fresh_heads


def _cached() -> CodeIndexStatusOutput:
    st = CodeIndexStatusOutput.model_construct(
        git_head="5d00ecff",
        clone_head="5d00ecff",
        compiler_tiers=[
            CodeCompilerTier(
                producer="scip-clang",
                tool="scip-clang",
                label="C++",
                watermark_key="scip_commit",
                commit="old0",
                lag=3,
            )
        ],
    )
    return st


def test_a_stale_cached_status_takes_the_fresh_state_rows() -> None:
    cached = _cached()
    st = cached.model_copy()
    state = {
        "git_head": {"value": "585f97e8", "updated_at": "2026-09-25 17:15:27"},
        "clone_head": {"value": b"5e65b017\x00"},
        "scip_commit": {"value": "5e65b017", "updated_at": "2026-09-25 17:32:00"},
    }
    _fresh_heads(st, state)
    assert (st.git_head, st.clone_head) == ("585f97e8", "5e65b017")
    assert st.compiler_tiers[0].commit == "5e65b017"
    assert st.compiler_tiers[0].updated_at == "2026-09-25 17:32:00"
    # the cached row itself is untouched (model_copy is shallow)
    assert cached.git_head == "5d00ecff" and cached.compiler_tiers[0].commit == "old0"


def test_an_empty_state_read_keeps_the_cached_heads() -> None:
    st = _cached().model_copy()
    _fresh_heads(st, {})
    assert st.git_head == "5d00ecff" and st.compiler_tiers[0].commit == "old0"
