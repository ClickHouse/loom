"""What the session's own Loom tools carried is not a new memory.

Cases are facts the plugin's SessionEnd extraction wrote from headless Loom
feature-test sessions, and the decisions a user
stated next to tool output that shares their words (which must survive).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.memory.app import pipeline as mp
from server.memory.app import pipeline_state as pipeline_state
from server.memory.domain import activity_facts as af
from server.memory.domain import session_echoes as se


def _f(value: str, key: str = "k") -> dict[str, Any]:
    return {"key": key, "value": value}


def test_a_value_the_session_wrote_is_an_echo_even_after_it_moved_on() -> None:
    """The stale copy: the session set "squash" then updated the key to
    "rebase"; both values rode the session, both re-extractions go."""
    values = ["I prefer squash merges for small PRs", "I prefer rebase merges for small PRs"]
    kept, dropped = se.partition(
        [
            _f("I prefer squash merges for small PRs."),
            _f("User prefers rebase merges for small PRs."),
        ],
        values,
    )
    assert kept == [] and len(dropped) == 2


def test_a_quoted_argument_and_a_written_note_are_echoes() -> None:
    values = [
        "loomcheck",
        "step 5b done",
        "Step 1: drain. Step 2: swap colours. Step 3: verify health.",
    ]
    facts = [
        _f("User created a conversation titled 'loomcheck'."),
        _f("append added step 5b done"),
        _f(
            "knowledge_get returned the full text: Step 1: drain. Step 2: swap colours. Step 3: verify health."
        ),
    ]
    kept, dropped = se.partition(facts, values)
    assert kept == [] and len(dropped) == 3


def test_an_unquoted_short_word_is_not_an_echo() -> None:
    kept, _ = se.partition([_f("User likes loomcheck-free tea on Fridays.")], ["loomcheck"])
    assert len(kept) == 1


def test_a_restated_tool_result_matches_through_identifier_parts() -> None:
    """The JSON said `total_memories`; the fact says "memories"."""
    result = '{"org":"clickhouse","namespace":"alice","total_memories":894,"recent_writes":9}'
    kept, dropped = se.partition([_f("The alice namespace has 894 total memories.")], [result])
    assert kept == [] and len(dropped) == 1


def test_a_new_decision_next_to_tool_output_that_shares_its_words_is_kept() -> None:
    """The adversarial controls of the measurement: a new deploy day beside a
    search result saying the old one, a decision that extends a doc."""
    old_day = '{"memory":{"results":[{"memory_key":"user_deploy_day","content_excerpt":"User deploys on Tuesdays."}]}}'
    doc = '{"results":[{"path":"docs/decisions/13090.md","body":"qa.* names answer as aliases for one release; tracker.* is the name."}]}'
    facts = [
        _f("User changed the deployment day to Thursdays after the QA sign-off."),
        _f(
            "User decided to keep the qa aliases for two more releases, not one, because Codex users still call qa.duplicate_check."
        ),
    ]
    kept, dropped = se.partition(facts, [old_day, doc])
    assert dropped == [] and len(kept) == 2


def test_no_session_values_keeps_everything() -> None:
    facts = [_f("I prefer squash merges.")]
    assert se.partition(facts, []) == (facts, [])


@pytest.mark.parametrize(
    "text",
    [
        "User used loom_code_grep for 'retry_budget'.",
        "User called code.pr_check for 'add retries'; refused due to access issues.",
        "User attempted to create a task 'loomcheck: say hi' in their inbox.",
        "User's attempt to register an inbox was refused due to insufficient authorization.",
        "Assistant reported that the plugin server defaulted to the clickhouse index.",
        "Assistant stated that neither principal can read 'code-loom'.",
        "append added step 5b done",
        "the hello loom search found no conversation results",
        "the server rated this search high-risk",
        "The raw response for loom_memory_set includes version '1790'.",
        "The result of loom_memory_update is 'updated: true'.",
        "The loom_memory_history has 25 versions across 5 memory ids.",
        "The loom_memory_related found no related memories due to lack of entities.",
        "The memories returned by loom about a tracker rename are about renameTempPart methods.",
        "User stored a document titled 'loomcheck runbook' with body 'Step 1'.",
    ],
)
def test_the_sessions_report_of_its_own_tool_calls_is_narration(text: str) -> None:
    assert af.is_activity_narration(text)


@pytest.mark.parametrize(
    "text",
    [
        "User decided retries back off exponentially, capped at 30 seconds.",
        "User reported that parts pile up to 3000 on memory_embeddings.",
        "Assistant explained that a run is Loom's unit for one agent execution.",
        "Assistant confirmed the root cause is the text index.",
        "The tracker search returned the wrong issue because the title was stripped.",
        "The deploy call failed because the token expired.",
        "The server has 64 cores.",
        "The results are stored in S3.",
        "The server is deployed on Tuesdays.",
        "The search index has 3 replicas.",
        "The history of the repo shows a rename in 2021.",
        "User stored the deploy keys in Vault.",
        "User prefers rebase merges for small PRs.",
    ],
)
def test_facts_about_the_users_world_are_not_narration(text: str) -> None:
    assert not af.is_activity_narration(text)


def test_the_step_runs_after_recall_echoes_and_before_narration() -> None:
    ids = [s.step_id for s in mp.DEFAULT_PIPELINE]
    assert (
        ids.index("loom.drop_recall_echoes")
        < ids.index("loom.drop_session_echoes")
        < ids.index("loom.drop_activity_narration")
    )
    assert "loom.drop_session_echoes" in mp.registered_step_ids()


def test_the_step_drops_echoes_and_counts_them() -> None:
    state = pipeline_state.WorkflowState(
        org="o",
        namespace="n",
        agent="a",
        session_values=["I prefer squash merges for small PRs"],
    )
    state.extracted_facts = [
        {"key": "test_pref", "value": "I prefer squash merges for small PRs."},
        {"key": "user_deploy_day", "value": "User deploys on Thursdays now."},
    ]
    out = asyncio.run(mp._step_drop_session_echoes(state, {}))
    assert [f["key"] for f in out.extracted_facts] == ["user_deploy_day"]
    assert out.skipped_session_echo == 1


def test_pasted_loomcheck_values_are_test_data_not_user_preferences() -> None:
    messages = [
        {
            "role": "user",
            "content": (
                'Use loom_memory_set to save key test/loomcheck/pref with value "I prefer squash merges for small PRs".\n'
                'Use loom_memory_update on test/loomcheck/pref: change it to "I prefer rebase merges for small PRs".\n'
                "The real deployment day is Thursdays.\n"
            ),
        }
    ]
    assert se.synthetic_test_values(messages) == [
        "I prefer squash merges for small PRs",
        "I prefer rebase merges for small PRs",
    ]
    state = pipeline_state.WorkflowState(
        org="o",
        namespace="n",
        agent="codex",
        messages=messages,
        extra_tags=["source:codex"],
    )
    state.extracted_facts = [
        _f('User prefers rebase merges for small PRs".', "user_preference_rebase_merges_small"),
        _f("The real deployment day is Thursdays.", "user_deployment_day"),
    ]
    out = asyncio.run(mp._step_drop_session_echoes(state, {}))
    assert [f["key"] for f in out.extracted_facts] == ["user_deployment_day"]
    assert out.skipped_session_echo == 1


def test_synthetic_values_need_a_disposable_key_and_user_role() -> None:
    assert (
        se.synthetic_test_values(
            [
                {"role": "user", "content": 'I prefer "rebase merges for small PRs".'},
                {
                    "role": "user",
                    "content": 'Use memory.set key team/merge with value "squash merges for small PRs".',
                },
                {
                    "role": "assistant",
                    "content": 'test/loomcheck/pref = "rebase merges for small PRs"',
                },
            ]
        )
        == []
    )
    assert se.synthetic_test_values(
        [
            {
                "role": "user",
                "content": (
                    'Set test/loomcheck/pref to "sample value for testing". '
                    'My real preference is "rebase merges for small PRs".'
                ),
            }
        ]
    ) == ["sample value for testing"]


def test_the_request_carries_session_values_clipped() -> None:
    from types import SimpleNamespace

    from server.memory.app.extract.handler import _extract_request_pins

    pins = _extract_request_pins(SimpleNamespace(session_values=["x" * 9000, "", "short value"]))
    assert [len(v) for v in pins["session_values"]] == [8000, 11]
