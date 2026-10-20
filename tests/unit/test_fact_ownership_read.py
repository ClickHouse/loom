"""A fact says whose it is on the read side too."""

from __future__ import annotations

from server.memory.workers import profile_consolidation as pc


def test_profile_fold_leaves_out_facts_about_others() -> None:
    from server.memory.store import profile_sources

    assert pc.user_facts is profile_sources.user_facts  # the fold reads the shared predicate
    assert "NOT has(tags, 'about:other')" in profile_sources.CURRENT_FACTS


def test_wire_hit_and_service_hit_carry_about_other() -> None:
    from server.memory.api.models_search import _MemorySearchHit
    from server.memory.domain.search_types import SearchHit

    h = _MemorySearchHit(
        memory_key="k",
        memory_type="semantic",
        content_excerpt="x",
        distance=0.1,
        similarity=0.9,
        rank_score=0.9,
        about_other=True,
    )
    assert h.about_other is True
    assert (
        SearchHit(
            memory_key="k", memory_type="semantic", content_excerpt="x", distance=0.1
        ).about_other
        is False
    )


def test_reader_line_labels_another_persons_fact() -> None:
    """The synthesis line the reader sees marks a row about someone else."""
    import inspect

    from server.memory.app.ask import rendering

    # The line renderer lives outside _synthesize so the question-first and
    # evidence-first prompts share it.
    src = inspect.getsource(rendering._memory_line)
    assert "about someone other than the user" in src
