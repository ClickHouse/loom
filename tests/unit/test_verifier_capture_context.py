"""The durability verifier sees what the capture was for.

With values alone, a bisect/duplicate-check session's "The fix is #112573,
merged into 26.8.1.1240" reads as a durable fact to the verifier; the
session goal and what its lookups returned are what mark it a restatement.
"""

from __future__ import annotations

import asyncio
from typing import Any

import server.memory.app.durability_verifier as dv
import server.memory.app.pipeline as mp
from server.memory.app import pipeline_state as pipeline_state
from server.memory.domain.extraction_prompts import (
    DURABILITY_VERIFIER_PROMPT_VERSION,
    DURABILITY_VERIFIER_SYSTEM_PROMPT,
)


def test_context_is_the_first_user_turn_and_clipped_lookups() -> None:
    msgs = [
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "which PR caused   this?"},
    ]
    vals = ["x" * 500, "", "  PR #1 fixed it  "] + [f"v{i}" for i in range(10)]
    ctx = dv.capture_context(msgs, vals)
    assert ctx.goal == "which PR caused this?"
    assert len(ctx.lookups) == dv.CONTEXT_LOOKUPS_MAX
    assert ctx.lookups[0] == "x" * dv.CONTEXT_LOOKUP_CHARS and ctx.lookups[1] == "PR #1 fixed it"


def test_prompt_carries_goal_lookups_role_and_key() -> None:
    facts = [
        {"key": "pr112573_fix_merged", "value": "The fix is #112573.", "source_role": "assistant"},
        {"key": "k2", "value": "v", "tags": ["source_role:user"]},
    ]
    text = dv._user_prompt(
        facts, dv.CaptureContext(goal="is this reported?", lookups=["#112573 merged"])
    )
    assert "Session goal (the user's first message): is this reported?" in text
    assert "- #112573 merged" in text
    assert "0. [assistant] (pr112573_fix_merged) The fix is #112573." in text
    assert "1. [user] (k2) v" in text


def test_prompt_without_context_is_the_old_shape_plus_keys() -> None:
    text = dv._user_prompt([{"value": "v"}], None)
    assert text.startswith("Facts extracted from one coding session:\n0. v\n")


def test_rubric_names_lookup_restatements_and_pins_the_version() -> None:
    assert "the tracker and the code index already hold them" in DURABILITY_VERIFIER_SYSTEM_PROMPT
    assert "Assistant stated" in DURABILITY_VERIFIER_SYSTEM_PROMPT
    assert DURABILITY_VERIFIER_PROMPT_VERSION == "durability-verifier-v1"


class _Client:
    def __init__(self) -> None:
        self.users: list[str] = []

    async def complete(self, **kw: Any) -> Any:
        self.users.append(kw["user"])

        class R:
            text = '{"verdicts": [{"i": 0, "keep": false, "why": "restates lookup"}]}'
            tokens_in = 10
            tokens_out = 5

        return R()


def test_pipeline_step_passes_the_capture_context(monkeypatch) -> None:
    client = _Client()
    monkeypatch.setattr(mp, "_verify_clients", lambda state: [client])
    state = pipeline_state.WorkflowState(org="o", namespace="n", agent="a")
    state.messages = [{"role": "user", "content": "what introduced this regression?"}]
    state.session_values = ["#98779 Fix assertion failure in view dependencies"]
    state.extra_tags = ["source:claude-code"]
    state.extracted_facts = [
        {
            "key": "pr98779_regression_pr_identified",
            "value": "Assistant identified the regression was caused by #98779.",
        }
    ]
    out = asyncio.run(mp._step_verify_durable(state, {}))
    assert out.extracted_facts == []
    assert "what introduced this regression?" in client.users[0]
    assert "#98779 Fix assertion failure" in client.users[0]
