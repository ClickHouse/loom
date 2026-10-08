"""A coding session's extraction keeps only durable facts.

The fixture holds synthetic facts shaped like a coding session's SessionEnd
extraction, labelled real / junk-by-class: every REAL fact must survive the
rules, and each junk sample must be caught by its class's rule."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.memory.app.pipeline import _step_drop_non_durable
from server.memory.app.pipeline_state import WorkflowState
from server.memory.domain import activity_facts as af
from server.memory.domain import durable_facts as df
from server.memory.domain import session_echoes as se

_FIX = json.loads((Path(__file__).parent / "fixtures" / "durable_facts.json").read_text())


@pytest.fixture(autouse=True)
def _peer_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixture's peer hosts are named `build-<2 chars>`."""
    monkeypatch.setattr(df.settings, "memory_quality_peer_host_pattern", r"build-[0-9a-z]{2}")


def _f(text: str, key: str = "k") -> dict:
    return {"key": key, "value": text}


@pytest.mark.parametrize("text", _FIX["real"])
def test_every_real_fact_of_the_eval_survives(text: str) -> None:
    assert df.non_durable_class(_f(text)) is None, text


@pytest.mark.parametrize(
    ("cls", "text"),
    [(c, t) for c, ts in _FIX["junk_by_class"].items() for t in ts],
)
def test_each_junk_class_of_the_eval_is_caught(cls: str, text: str) -> None:
    assert df.non_durable_class(_f(text)) == cls, text


@pytest.mark.parametrize(
    ("junk", "real"),
    [
        (
            "Cut 51 is all green.",
            "Cut 69 added inp_ms on every ui.vitals row and an INP column in deploy-delta.",
        ),
        (
            "Round 10 is live on c0cf54838.",
            "User's settings page is now live with three tabs instead of one long scroll.",
        ),
        (
            "User freed 27 GB by pruning Docker's build cache.",
            "The disk was growing by 2.1 GB every 20 seconds due to a runaway writer.",
        ),
        (
            "build-25 claimed the lease regarding a GitHub issue search.",
            "Linking a database with any schema, without writing views, is deliberately not supported.",
        ),
        (
            "User found the handler at server/tracker/app/dupcheck.py:75-112.",
            "Loom decides the fixed set of tables and columns its queries read.",
        ),
        (
            "User is waiting for the final grading of Django to open the PR.",
            "User is working on building the best code index for Python for any repo.",
        ),
    ],
)
def test_the_shape_decides_not_the_topic(junk: str, real: str) -> None:
    assert df.non_durable_class(_f(junk)) is not None
    assert df.non_durable_class(_f(real)) is None


def test_a_disposable_code_comment_is_not_a_durable_session_fact() -> None:
    fact = "User appended the comment # loomcheck codex gate test to the end of server/core/naming.py as a disposable plugin test."
    assert df.non_durable_class(_f(fact)) == "housekeeping"
    assert df.non_durable_class(_f("The namespace validator now rejects uppercase names.")) is None
    codex = asyncio.run(_step_drop_non_durable(_state([_f(fact)], ["source:codex"]), {}))
    assert codex.extracted_facts == [] and codex.skipped_non_durable == 1


def test_duplicates_keep_the_more_specific_fact() -> None:
    a = _f(
        "User claims jdoe + viewer on code-example-private, code-loom, qa-example-private.",
        "a",
    )
    b = _f(
        "User's token claims jdoe + viewer on code-example-private, code-loom, qa-example-private.",
        "b",
    )
    c = _f("User decided to move the Flow page into the Admin drawer.", "c")
    kept, dropped = df.collapse_duplicates([a, b, c])
    assert [f["key"] for f in kept] == ["b", "c"]
    assert dropped[0]["key"] == "a" and dropped[0]["_duplicate_of"] == "b"


def test_a_bare_identifier_is_not_a_quotable_session_value() -> None:
    """A fact that names the namespace a tool call took is not dropped for that alone."""
    facts = [
        _f(
            "User's team-alerts namespace was reshaped to store claims as plain-text rows under readable keys."
        )
    ]
    kept, dropped = se.partition(facts, ["team-alerts", "jdoe", "memory_key"])
    assert kept == facts and not dropped
    kept, dropped = se.partition(
        [_f("User prefers rebase merges for small PRs.")], ["I prefer rebase merges for small PRs"]
    )
    assert dropped, "a restated value still is an echo"


def test_a_short_fact_does_not_restate_a_huge_result_blob() -> None:
    blob = " ".join(
        f'{{"memory_key": "k{i}", "namespace": "jdoe", "value": "loom row {i}"}}'
        for i in range(200)
    )
    kept, _ = se.partition([_f("User's Loom namespace is jdoe.")], [blob])
    assert kept


def test_a_passive_design_statement_is_not_narration() -> None:
    assert not af.is_activity_narration(
        "Access is checked per caller, and it's limited to 1.5 s so it can't slow the search down."
    )
    assert af.is_activity_narration("Avery checked the file server/main.py.")


def _state(facts: list[dict], tags: list[str]) -> WorkflowState:
    return WorkflowState(
        org="o",
        namespace="n",
        agent="a",
        session_id="s",
        messages=[],
        extra_tags=tags,
        extracted_facts=facts,
    )


def test_the_step_runs_only_on_coding_captures() -> None:
    facts = [
        _f("User left 98 GB of Docker images on the host."),
        _f("User decided retries back off exponentially."),
    ]
    personal = asyncio.run(_step_drop_non_durable(_state(list(facts), ["topic:life"]), {}))
    assert personal.extracted_facts == facts and personal.skipped_non_durable == 0
    coding = asyncio.run(_step_drop_non_durable(_state(list(facts), ["source:claude-code"]), {}))
    assert [f["value"] for f in coding.extracted_facts] == [
        "User decided retries back off exponentially."
    ]
    assert coding.skipped_non_durable == 1
    assert all(not k.startswith("_") for f in coding.extracted_facts for k in f)


def test_peer_host_rule_follows_the_configured_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    fact = _f("build-7 claimed the release lease.")
    monkeypatch.setattr(df.settings, "memory_quality_peer_host_pattern", "")
    assert df.non_durable_class(fact) is None
    monkeypatch.setattr(df.settings, "memory_quality_peer_host_pattern", r"build-[0-9]+")
    assert df.non_durable_class(fact) == "peer"
