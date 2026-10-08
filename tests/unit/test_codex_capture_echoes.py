"""A file dump is not crystallized, and a Loom tool's answer
restated with a reporting verb is not a fact."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.app import observe_replay as replay
from server.memory.domain import durable_facts as df

CAT_CLAUDE_MD = (
    "[command_run] tool=Bash\n"
    'input: {"command":"cat CLAUDE.md"}\n'
    "output: # Loom — project memory for AI coding agents\n"
    "\n"
    "Loom is a self-hosted memory, knowledge, and observability data plane for AI agents.\n"
    "`make ingest FILE=CLAUDE.md TAGS=loom,project-memory` loads it into the knowledge plane.\n"
    "- `server/` — FastAPI control plane (289 ops across 32 groups as of 2026-09-20).\n"
)


def _obs(narrative: str, **extra: object) -> SimpleNamespace:
    return SimpleNamespace(value={"kind": "command_run", "narrative": narrative, **extra})


def test_a_file_dump_whose_first_line_is_short_is_not_replayed() -> None:
    compact = replay.compact_narrative(CAT_CLAUDE_MD)
    assert "Loom is a self-hosted" not in compact and "289 ops" not in compact
    assert not replay.replayable(_obs(CAT_CLAUDE_MD))


def test_a_short_multi_line_result_and_an_error_still_replay() -> None:
    short = "[command_run] tool=Bash\ninput: pytest -q\noutput: 1 failed\n2 passed"
    assert replay.compact_narrative(short).endswith("output: 1 failed\n2 passed")
    assert replay.replayable(_obs(short))
    assert replay.replayable(_obs(CAT_CLAUDE_MD, error="exit 1"))


def test_a_payload_block_ends_at_the_next_payload_or_kept_line() -> None:
    text = "[command_run] tool=Bash\ninput: ls\noutput: a.py\nb.py\nerror: exit 2: missing"
    assert replay.compact_narrative(text) == (
        "[command_run] tool=Bash\ninput: ls\noutput: a.py\nb.py\nerror: exit 2: missing"
    )


def test_a_loom_tool_answer_with_a_reporting_verb_is_a_session_action() -> None:
    for text in (
        "User's loom_code_search results indicate that tracker.duplicate_check is registered in "
        "server/tracker/api/tracker.py",
        "User's loom_code_context_pack indicates a new tracker op starts in server/tracker/api/tracker.py",
        "The loom_code_outline results show five classes in qa_intel.py",
        "User's memory history for key test/loomcheck-cx/pref includes multiple versions with timestamps.",
        "User's preference test/loomcheck-cx/pref is locked as 'read_only'.",
        "User noted that Loom found one production caller, `tracker_leg`, plus test calls.",
        "User identified that `handle_tracker_duplicate_check` validates the draft and runs the check once.",
        "User noted that the first match falls in the design record's Context section, lines 5–37.",
        "User received a hook message stating that the GitHub search is answered by Loom's tracker record.",
        "User has one remaining key under test/loomcheck-cx/: test/loomcheck-cx/pref.",
        "User's loom token is absent at /home/u/.loom/token, but present at /home/u/.codex/plugins",
    ):
        assert df.session_action_class({"value": text}) == "session_action", text


def test_a_real_fact_that_names_a_loom_tool_is_kept() -> None:
    for text in (
        "User prefers loom_search over grep for recall questions.",
        "The team decided loom_op stays the only door to unlisted ops.",
        "User identified that the flaky test was caused by a timezone bug in toStartOfInterval.",
        "User's profile section for work lists ClickHouse as the employer.",
        "User has two children under ten.",
    ):
        assert df.session_action_class({"value": text}) is None, text
