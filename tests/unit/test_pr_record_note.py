"""The code plane's PR record is the current state."""

from __future__ import annotations

from types import SimpleNamespace as N

from server.memory.app.ask.prompts import _PR_RECORD_NOTE, _has_pr_record


def test_pool_with_a_code_pr_or_gh_item_row_gets_the_note() -> None:
    assert _has_pr_record(
        [N(memory_key="user_pr_state"), N(memory_key="code_pr:code-clickhouse:116990")]
    )
    assert _has_pr_record([N(memory_key="gh_item:code-clickhouse:107091")])
    assert not _has_pr_record([N(memory_key="user_pr_state"), N(memory_key="chunk:s:1")])
    assert "authoritative" in _PR_RECORD_NOTE and "merged or closed" in _PR_RECORD_NOTE
