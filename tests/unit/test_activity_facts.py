"""Activity narration is not a memory.

The labelled set is shaped like extraction output (synthetic names, ids and home
paths changed): True = narration a later session gains
nothing from, False = a fact worth keeping. The filter must never drop a
keeper (precision 1.0 on drops) and catch most narration; a few vague rows
("conducted an analysis of …") are labelled narration and allowed through —
erring toward keeping."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.memory.domain import activity_facts as af

NARRATION: list[str] = [
    "User read lines 1001-2000 from MergeTreeData.cpp.",
    "User read lines 1-1000 from MergeTreeData.cpp.",
    "User has now read all 14,758 lines.",
    "User read src/Storages/MergeTree/MergeTreeData.cpp in full.",
    "User read the file /home/dev/worktrees/hooktest/server/core/naming.py.",
    "User read the implementation of MergeTreeData, the abstract base for every MergeTree-family engine.",
    "User read and summarized the file src/Storages/MergeTree/MergeTreeData.cpp.",
    "User requested a summary of src/Storages/MergeTree/MergeTreeData.cpp.",
    "User searched for occurrences of StorageSharedMergeTree and found matches in 7 files.",
    "User searched for occurrences of StorageSharedMergeTree and found no matches.",
    "User searched for the symbol DB::StorageSharedMergeTree and found no definitions or candidates.",
    "User ran a grep command to find occurrences of X::mergeSelectingTask in src/Storages/X.cpp.",
    "User ran multiple Bash commands on tests/unit/test_default_namespace.py and other files.",
    "User ran tests after the Stop hook's request, resulting in 37 passed, 1 skipped, and 6 warnings.",
    "User ran the Loom client doctor and found every client line passes.",
    "User ran a command to read RawBLOB docs and get data at users.",
    "User is checking if the issue 'LOGICAL_ERROR in X' is already reported.",
    "User is testing 'Random stuff'.",
    "User edited the file /home/dev/worktrees/hooktest/server/core/naming.py.",
    "User has fetched GitHub data for her weekly update across all relevant repos.",
    "User reported the results of three tool calls: 1) Read server/main.py was blocked by a hook.",
    "Claude ran a command on the file tests/integration/test_storage_delta/test_writes.py.",
    "Claude ran the command using the path .claude/tools/fetch_ci_report.js.",
    "Claude ran commands using paths: tests/integration/test_storage_delta/test_writes_interop.py",
    "Claude ran a command to list files in tmp/chsrv/users.xml and access/need_rebuild_lists.mark.",
    "Claude searched for RawBLOB references in the documentation and found relevant files.",
    "Claude edited the file tests/queries/0_stateless/05232_deltalake_writes_matrix.sh.",
    "Claude read the file /private/tmp/claude-501/tasks/bsu1pnmz3.output.",
    "Claude used the TaskStop tool on the paths /private/tmp/claude-501/scratch.",
    "Claude used the Monitor tool on the paths ci/tmp/pytest_parallel-gw0.log.",
    "Claude-Code ran a Bash command in the paths /home/dev/worktrees/feature-a, server/settings.py.",
    "Claude-Code ran a Bash command in the path server/transport/mcp.py.",
    "Claude-Code ran a Bash command in the paths /dev/shm/pin_unit.txt, docs/decisions/0042-a-pinned-row-is-never-evicted.md.",
    "Claude-Code edited the file /home/dev/worktrees/feature-b/server/memory/app/handlers.py.",
    "Claude-Code edited a file in the path docs/decisions/0042-a-pinned-row-is-never-evicted-by-a-write.md.",
    "Assistant quoted a Loom working-memory note: 'I switched to black coffee'.",
    "Dana searched the codebase in src/Storages/MergeTree/MergeTreeData.cpp.",
    "Dana ran a grep command to find occurrences of X::mergeSelectingTask in src/Storages/X.cpp.",
    "Dana retrieved a code snippet from src/Storages/MergeTree/MergeTreeData.cpp.",
    "Dana retrieved results for tests, detailing their status and number of failures.",
    "Dana executed a query to fetch test failure data, focusing on tests that failed since September 21.",
    "Dana accessed the full reference for the loom-memory plugin.",
    "Dana ran a Python script to check for duplicates in ClickHouse.",
    "Dana summarized test failures, noting various tests with failures and non-failures.",
    "Dana encountered an error: SKILL.md file not found at plugins/cache/loom/0.9.18/SKILL.md.",
    # Vague narration the filter lets through (erring toward keeping).
    "Dana conducted an analysis of flaky tests, identifying tests with failures and their timestamps.",
    "User confirmed the file src/Storages/MergeTree/MergeTreeData.cpp has 14,758 lines.",
    "Dana found the SKILL.md file at plugins/cache/loom/0.9.19/skills/loom-code/SKILL.md.",
]
KEEP: list[str] = [
    "User posts her weekly QA team update on Mondays in the team Slack channel.",
    "Dana Reyes's GitHub username is dreyes.",
    "The weekly update thread is posted every Thursday in the team channel.",
    "The reporting window for this run is from 2026-09-14T07:47Z to 2026-09-21T16:50Z (UTC).",
    "The method MergeTreeData::renameTempPartAndAdd is defined at src/Storages/MergeTree/MergeTreeData.cpp:7137.",
    "The declaration of MergeTreeData::renameTempPartAndAdd is in MergeTreeData.h at line 951.",
    "There are roughly 290 methods in about 14.8k lines.",
    "The issue is fixed by PR #113385 and backported in versions 26.8.9.11 and 26.9.1.1616.",
    "User's issue is the same defect as issue #113223, with 99.57% duplicate confidence.",
    "User's case uses 'renameTempPartAndReplace', occurs after a replica restart, and affects 26.8.1.",
    "User's laptop token has the viewer grant on qa-example-private.",
    "User's one failed memory.set this morning was due to the namespace bug.",
    "User reported an incident that started at 03:40Z, with inserts failing due to 'too many parts'.",
    "User confirmed that bisect answers 'unknown' when the token has no QA grant, because it needs the QA record.",
    "User should file this issue as a new report, including the complete stack trace.",
    "User found no matching report for the issue in the QA record and should file it as a new issue.",
    "Dana ran a bisect to find the commit that introduced a bug using pre-built master binaries.",
    "Dana noted that a trustworthy performance bisect ends bimodal.",
    "Dana analyzed failures in test_storage_nats/test_nats_jet_stream.py, finding 3 failures and 7548 non-failures.",
    "Dana identified that the user's issue is the same defect as issue #113223.",
    "Claude ran the tests after fixing the TTL merge bug; 37 passed.",
    "Claude accessed the upstream Arrow fix PR 115250 for details.",
    "The assistant checked the config and the root cause is a stale token.",
    "User read the docs: the setting must be enabled before 26.8.",
    "Claude ran the benchmark and decided to keep the compact prompt tier.",
    "Tests ran green on the fix branch.",
    "The file is the implementation of MergeTreeData, the abstract base for every MergeTree-family engine.",
    "Loom holds a compiler-grade index of the codebase.",
    "User prefers squash merges for small PRs.",
    "User switched to rebase merges because squash lost co-author trailers.",
]


def test_the_labelled_set_is_large_enough() -> None:
    assert len(NARRATION) + len(KEEP) >= 60


def test_no_keeper_is_dropped_and_most_narration_is() -> None:
    dropped_keepers = [t for t in KEEP if af.is_activity_narration(t)]
    assert dropped_keepers == [], dropped_keepers
    caught = sum(af.is_activity_narration(t) for t in NARRATION)
    assert caught / len(NARRATION) >= 0.85, f"recall {caught}/{len(NARRATION)}"


@pytest.mark.parametrize(
    "value",
    [
        "User read lines 1-1000 from MergeTreeData.cpp.",
        {"text": "Claude ran a command on the file a.py"},
        '"Claude-Code ran a Bash command in the path server/x.py."',
    ],
)
def test_value_shapes(value: object) -> None:
    assert af.is_activity_narration(value)


def test_non_text_values_are_kept() -> None:
    assert not af.is_activity_narration({"n": 3})
    assert not af.is_activity_narration(42)
    assert not af.is_activity_narration("")


def test_subject_to_agent_names_the_agent_not_the_person() -> None:
    assert af.subject_to_agent("User ran pytest; 2 failed because of X.", "claude-code") == (
        "claude-code ran pytest; 2 failed because of X."
    )
    assert (
        af.subject_to_agent("The user fixed the flaky test.", "codex")
        == "codex fixed the flaky test."
    )
    # A possessive names the person: their laptop, their preference.
    assert af.subject_to_agent("User's laptop has the viewer grant.", "claude-code") == (
        "User's laptop has the viewer grant."
    )
    assert af.subject_to_agent({"text": "User ran X."}, "cc") == {"text": "cc ran X."}
    assert af.subject_to_agent("User ran X.", "*") == "User ran X."


def _state(facts: list[dict], instructions: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        extracted_facts=facts,
        extraction_instructions=instructions,
        agent="claude-code",
        skipped_activity=0,
    )


def test_the_pipeline_step_drops_narration_and_counts_it() -> None:
    from server.memory.app import pipeline as mp

    facts = [
        {"memory_key": "user_read_lines", "value": "User read lines 1-1000 from X.cpp."},
        {"memory_key": "merge_style", "value": "User prefers squash merges for small PRs."},
    ]
    out = asyncio.run(mp._step_drop_activity_narration(_state(facts), {}))
    assert [f["memory_key"] for f in out.extracted_facts] == ["merge_style"]
    assert out.skipped_activity == 1
    ids = [s.step_id for s in mp.DEFAULT_PIPELINE]
    assert (
        ids.index("loom.drop_recall_echoes")
        < ids.index("loom.drop_activity_narration")
        < ids.index("loom.hash_dedup")
    )
    assert "loom.drop_activity_narration" in mp.registered_step_ids()


def test_a_trajectory_replay_names_the_agent_where_the_model_wrote_user() -> None:
    from server.memory.app import pipeline as mp
    from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS

    facts = [{"memory_key": "k", "value": "User fixed the retry loop because it spun on 429."}]
    out = asyncio.run(
        mp._step_drop_activity_narration(_state(facts, TRAJECTORY_EXTRACTION_INSTRUCTIONS), {})
    )
    assert out.extracted_facts[0]["value"].startswith("claude-code fixed")
    # A transcript extraction (no trajectory instructions) keeps the wording.
    out = asyncio.run(mp._step_drop_activity_narration(_state([dict(facts[0])]), {}))
    assert out.extracted_facts[0]["value"].startswith("User fixed")


def test_the_replay_keeps_only_observations_that_carry_a_result() -> None:
    """A replay line without an output or an error can only be narrated
    ("Claude-Code ran a Bash command in the paths a.py"), and bare
    `[command_run] tool=Bash / paths: …` observations are the common case."""
    from server.memory.app import observe_replay as replay

    def obs(narrative: str, **extra: object) -> SimpleNamespace:
        return SimpleNamespace(
            value={"kind": "command_run", "narrative": narrative, **extra}, created_at=None
        )

    rows = [
        obs("[command_run] tool=Bash\npaths: server/a.py"),  # bare: skipped
        obs("[file_read] tool=Read\npaths: server/a.py"),  # a read: skipped
        obs("[command_run] tool=Bash\ninput: pytest -q\noutput: 2 failed"),
        obs("[command_run] tool=Bash\nerror: exit 1"),
        obs("[file_read] tool=Read", error="No such file or directory"),
    ]
    assert [replay.replayable(o) for o in rows] == [False, False, True, True, True]
    assert len(replay.replay_messages(rows, agent="claude-code")) == 3


def test_a_session_with_nothing_to_replay_runs_no_extraction() -> None:
    from server.memory.app import observe_replay as replay

    assert asyncio.run(replay._extract_replay({"org": "o"}, [], "")) == 0
