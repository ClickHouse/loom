"""Capture junk a coding session stores about itself.

Every text below is the shape of a row a coding session (Claude Code or Codex)
stored about the session itself. Each must be dropped on the write side and
classed on the read side, and the facts that look alike but are durable must
survive both."""

from __future__ import annotations

import asyncio
import json

import pytest

from server.memory.app.pipeline import _step_drop_non_durable
from server.memory.app.pipeline_state import WorkflowState
from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS
from server.memory.domain import durable_facts as df
from server.memory.domain import fact_quality as fq
from server.memory.domain import session_echoes as se


def _f(text: str, key: str = "k") -> dict:
    return {"key": key, "value": text}


# SessionEnd captures (tag source:claude-code): what a tool call returned.
_TOOL_OUTCOMES = [
    "The pinned value won.",
    "The update was refused due to the lock being read_only.",
    "The extraction ran on openai / gpt-4o-mini.",
    "It returned three sections and was cut off at the default 2,000-token limit.",
    "What came back is mostly records of earlier sessions that ran this same prompt.",
    "Loom has nothing about the tracker rename itself.",
    "This tool takes no task argument.",
    "It accepts only a `pr_number`, and without one it returns the general repo map.",
    "Neither `loom_namespace_list_mine` nor `loom_namespace_summary` is a tool in the plugin.",
    "The index is at commit `fcf7bb837`, 3 commits behind main.",
    "The first hit for 'decision 42' was `docs/decisions/0043-x.md:11`, a note.",
    "The last two PRs to touch it were #98 (decision 44) and #85.",
    "The index flagged retrieval_risk: high with confidence 0.49 for the tracker duplicate check.",
    "The latest related commit in the repo is `c15ee82f1` (decision 45, tracker scoping).",
    "The other 37 are unit tests in `test_tracker_duplicate_check.py`.",
    "User found no report of the 'IllegalTypeOfArgument in toStartOfInterval' error.",
    "User noted that 'a', 'b', and 'pinned' were deleted, while 'pref' was refused with 423 LOCKED.",
    "User received feedback from the Stop hook stating no test reaches server.core.naming::validate.",
    "User added a comment '# loomcheck' at the end of server/core/naming.py.",
    "User's conversation turns were not saved because conversation logging is turned off.",
    "The command 'python' was not found when running a Bash command.",
    "Codex tests passed with 121 tests passing in 0.55 seconds.",
    "The hook message was: 'loom-code-shunt: server/main.py is 1007 lines'.",
    "Loom blocked the single `cat server/main.py` attempt.",
    "The first issue found is 'Logical error: Bad cast from type A to B'.",
    "User completed the task 'loomcheck: say hi' with outcome {'text': 'hi'}.",
]
_CODE_SPANS = [
    "`QaIntelUnavailable` (36-37) is the error class in `server/clients/qa_intel.py`.",
    "`_Breaker` (41-75) is a circuit breaker with `allow`, `record` and `status`.",
    "`Agent.qa` is a deprecated alias in `sdk/python/loom/agent.py:214-221`.",
]


@pytest.mark.parametrize("text", _TOOL_OUTCOMES + _CODE_SPANS)
def test_live_capture_junk_is_not_durable(text: str) -> None:
    # an earlier rule may claim it first (the housekeeping rule); caught is what counts
    assert df.non_durable_class(_f(text)) is not None, text
    assert df.capture_outcome_class(_f(text)) in ("tool_outcome", "code_lookup"), text


@pytest.mark.parametrize("text", _TOOL_OUTCOMES + _CODE_SPANS)
def test_read_side_classes_stored_capture_junk(text: str) -> None:
    assert fq.junk_class("k", text, tags=["source:claude-code"]) is not None, text


@pytest.mark.parametrize(
    "text",
    [
        "Retries back off exponentially, capped at 30 seconds.",
        "Unknown check names return HTTP 404.",
        "Every call is `POST /v1/<op>` on the loom they already use.",
        "`loom_search`, run from that repository's checkout, now also searches its docs.",
        "The disk was growing by 2.1 GB every 20 seconds due to a runaway writer.",
    ],
)
def test_lookalike_durable_facts_survive(text: str) -> None:
    assert df.non_durable_class(_f(text)) is None
    assert fq.junk_class("k", text, tags=["source:claude-code"]) is None


def test_read_side_rules_apply_only_to_capture_rows() -> None:
    # An asserted row with the same words is the namespace owner's own claim.
    assert fq.junk_class("k", "The pinned value won.", tags=[]) is None


# --- echoes of a code-tool result and of a row inside a search result -------
_CODE_RESULT = json.dumps(
    {
        "matches": [
            {
                "path": "server/transport/mcp_config.py",
                "line": 40,
                "text": "TOOLSET_ALIASES = {'qa': 'tracker'}  # ?toolset=qa maps to tracker",
            },
            {
                "path": "server/transport/http.py",
                "line": 12,
                "text": "serve every alias at /v1/<alias> with a deprecation notice",
            },
        ]
    }
)
_SEARCH_RESULT = json.dumps(
    {
        "memory": {
            "results": [
                {
                    "memory_key": "test/loomcheck/pref",
                    "content_excerpt": '"I prefer rebase merges for small PRs" (#test/loomcheck/pref)',
                    "rank_score": 0.70,
                },
                {"memory_key": "other", "content_excerpt": "x" * 3000},
            ]
        }
    }
)


def test_a_fact_restating_a_code_tool_result_is_an_echo() -> None:
    facts = [
        _f("`?toolset=qa` maps to `tracker` through `TOOLSET_ALIASES`."),
        _f("The HTTP router serves every alias at `/v1/<alias>` with a deprecation notice."),
    ]
    kept, dropped = se.partition(facts, [_CODE_RESULT])
    assert kept == [] and len(dropped) == 2


def test_a_fact_restating_a_row_inside_a_search_result_is_an_echo() -> None:
    kept, dropped = se.partition(
        [_f("User prefers rebase merges for small PRs.")], [_SEARCH_RESULT]
    )
    assert kept == [] and len(dropped) == 1


def test_one_dotted_token_is_not_a_code_echo() -> None:
    # "User's email is …" is a real fact even when a search result carried the
    # address.
    blob = json.dumps(
        {
            "results": [
                {"memory_key": "e", "content_excerpt": "avery.quinn@example.com " + "y " * 900}
            ]
        }
    )
    kept, _ = se.partition([_f("User's email is avery.quinn@example.com.")], [blob])
    assert len(kept) == 1


# --- the observe replay: an actor-led fact is an action ----------------------
@pytest.mark.parametrize(
    "text",
    [
        "Claude-Code ran a Bash command in the paths /opt/loom, server/tasks/api/task.py.",
        "Codex created a pull request for 'codex-loom-audit' on GitHub.",
        "Codex synced 1 file(s) into integrations/codex-plugin.",
        "Codex applied a patch successfully, updating the file server/transport/defect_answer.py.",
        "Codex plugin status changed to 'enabled' for loom.",
        "Claude-code created the script /dev/shm/vs/pp2.py using a Bash command.",
        "Sam wrote the alertheatmap.js service file.",
        "Sam is working in the directory /tmp/acme-24.",
        "Claude verified that PR 2 diff contains only intended files.",
        "Nik will verify everything on the local Tilt stack first before pushing.",
    ],
)
def test_replay_actions_are_dropped(text: str) -> None:
    assert df.replay_action_class(_f(text), "codex") == "replay_action", text


@pytest.mark.parametrize(
    "text",
    [
        "Avery noted that a trustworthy performance bisect ends bimodal.",
        "Sam decided to adopt the canonical availability data from #42089.",
        "Unknown check names return HTTP 404.",
        "User's favorite editor is Neovim.",
        "Loom is a self-hosted data plane for AI agents, built on ClickHouse.",
    ],
)
def test_replay_knowledge_and_world_facts_survive(text: str) -> None:
    assert df.replay_action_class(_f(text), "codex") is None, text


def _state(facts: list[dict], instructions: str, tags: list[str]) -> WorkflowState:
    return WorkflowState(
        org="o",
        namespace="n",
        agent="codex",
        session_id="s",
        messages=[],
        extra_tags=tags,
        extracted_facts=facts,
        extraction_instructions=instructions,
    )


def test_the_step_drops_replay_actions_only_in_a_replay() -> None:
    facts = [
        _f("Codex synced 1 file(s) into integrations/codex-plugin."),
        _f("Unknown check names return HTTP 404."),
    ]
    replay = asyncio.run(
        _step_drop_non_durable(
            _state(list(facts), TRAJECTORY_EXTRACTION_INSTRUCTIONS, ["from_observation"]), {}
        )
    )
    assert [f["value"] for f in replay.extracted_facts] == ["Unknown check names return HTTP 404."]
    session_end = asyncio.run(_step_drop_non_durable(_state(list(facts), "", ["source:codex"]), {}))
    assert len(session_end.extracted_facts) == 2


def test_read_side_classes_stored_replay_actions() -> None:
    tags = ["crystallized", "from_observation", "speaker:codex"]
    assert (
        fq.junk_class("k", "Codex synced 1 file(s) into integrations/codex-plugin.", tags=tags)
        == "narration"
    )
    assert fq.junk_class("k", "Unknown check names return HTTP 404.", tags=tags) is None
    # a person named at the start is left to the activity-narration rule
    assert (
        fq.junk_class("k", "Avery used the script bisect.sh for the bisect process.", tags=tags)
        is None
    )
