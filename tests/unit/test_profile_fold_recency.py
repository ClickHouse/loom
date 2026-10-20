"""The profile fold reads dates and keeps the newest.

A fold with no dates and no supersession can carry a preference the user has
already replaced (on PersonaMem-v2, changed preferences scored 35.5 with memories alone and 32.3 with such a fold). The fold sees each fact's
date, oldest first, reads only current rows, and is told to keep the most
recent statement when two disagree.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.workers import profile_consolidation as pc
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


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
    assert "status NOT IN ('superseded', 'rejected', 'proposed', 'duplicate')" in src
    assert "(valid_to IS NULL OR valid_to > now())" in src
    assert "(expires_at IS NULL OR expires_at > now())" in src
    assert "coalesce(valid_at, updated_at)" in src
    # The user's own facts only, never a stored request to forget
    assert "startsWith(memory_key, 'user_')" in src and "NOT has(tags, 'forget')" in src


class _LocalCH:
    def __init__(self, *setup: str) -> None:
        self.setup = list(setup)

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return run(self.setup, sql, params)


def _preference(mid: str, value: str, status: str) -> str:
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_type, value, memory_id, content_sha256, version, status) VALUES ('o', 'n', "
        f"'a', 'u1', '-', 'user_{mid[-1]}', 'preference', '{value}', '{mid}', "
        f"'{value[0] * 64}', 1, '{status}')"
    )


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_fold_leaves_out_a_duplicate_row() -> None:
    """A duplicate's canonical row is the fact: the fold reads that one only."""
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store.profile_sources import user_facts

    ch = _LocalCH(
        ddl("memory"),
        _preference("00000000-0000-0000-0000-00000000000a", "Uses uv.", "active"),
        _preference("00000000-0000-0000-0000-00000000000b", "Likes uv.", "duplicate"),
    )
    rows = await user_facts(
        org="o",
        namespace="n",
        user_id="u1",
        types=["preference", "decision"],
        limit=10,
        user_keys_only=True,
        scope=WHOLE_NAMESPACE,
        ch=ch,
    )
    assert [r["value"] for r in rows] == ["Uses uv."]


def test_fold_config_reads_the_model_setting() -> None:
    from server.memory.workers.profile_consolidation import ProfileConsolidationConfig
    from server.settings import settings

    assert ProfileConsolidationConfig().model == settings.profile_consolidation_model
