"""The profile fold reads dates and keeps the newest.

A fold with no dates and no supersession can carry a preference the user has
already replaced (on PersonaMem-v2, changed preferences scored 35.5 with memories alone and 32.3 with such a fold). The fold sees each fact's
date, oldest first, reads only current rows, and is told to keep the most
recent statement when two disagree.
"""

from __future__ import annotations

from server.memory.workers import profile_consolidation as pc


def test_render_fold_input_is_dated_oldest_first_and_skips_blanks() -> None:
    rows = [
        {"value": "Prefers dark mode.", "since": "2026-03-01 10:00:00"},
        {"value": "  ", "since": "2026-02-01"},
        {"value": "Prefers light mode now.", "since": "2026-05-09 08:00:00"},
        {"value": "Uses uv.", "since": ""},
    ]
    text = pc.render_fold_input(rows)
    assert text.splitlines() == [
        "- [2026-03-01] Prefers dark mode.",
        "- [2026-05-09] Prefers light mode now.",
        "- [undated] Uses uv.",
    ]
    assert pc.render_fold_input([]) == "" and pc.render_fold_input([{"value": ""}]) == ""


def test_fold_prompt_carries_the_recency_and_forget_rules() -> None:
    assert "MOST RECENT" in pc._FOLD_SYSTEM and "forget" in pc._FOLD_SYSTEM


def test_fold_reads_only_current_rows() -> None:
    import inspect

    from server.memory.store import profile_sources

    assert "user_keys_only=True" in inspect.getsource(pc._consolidate_identity)
    src = inspect.getsource(profile_sources.user_facts) + profile_sources.CURRENT_FACTS
    assert "status NOT IN ('superseded', 'rejected', 'proposed')" in src
    assert "(valid_to IS NULL OR valid_to > now())" in src
    assert "(expires_at IS NULL OR expires_at > now())" in src
    assert "coalesce(valid_at, created_at)" in src
    # The user's own facts only, never a stored request to forget
    assert "startsWith(memory_key, 'user_')" in src and "NOT has(tags, 'forget')" in src


def test_fold_config_reads_the_model_setting() -> None:
    from server.memory.workers.profile_consolidation import ProfileConsolidationConfig
    from server.settings import settings

    assert ProfileConsolidationConfig().model == settings.profile_consolidation_model
