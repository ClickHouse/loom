"""A coding capture gets a last look before it is stored — a
batched verifier drops a fact only when every model says drop, folds
same-claim paraphrases into the most complete kept member, and keeps every
fact on any failure. Coding sessions skip speaker attribution, and a few
content-free shapes are rules."""

from __future__ import annotations

import json

import pytest

from server.llm.client import LLMResult
from server.memory.app import durability_verifier as dv
from server.memory.app import pipeline as mp
from server.memory.app import pipeline_state as pipeline_state
from server.memory.domain import durable_facts as df
from server.settings import settings


class _Scripted:
    provider = "scripted"
    model = "scripted-model"

    def __init__(self, answer: object) -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def complete(self, system, user, max_tokens=1024, response_format=None):
        self.calls.append(user)
        if isinstance(self.answer, Exception):
            raise self.answer
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return LLMResult(text=text, tokens_in=100, tokens_out=20, provider="p", model="m")


def _facts(*values: str) -> list[dict]:
    return [{"key": f"k{i}", "value": v} for i, v in enumerate(values)]


def _answer(
    drop: list[int] = (),
    groups: list[tuple[list[int], int]] = (),
    superseded: list[tuple[int, int]] = (),
    n: int = 4,
) -> dict:
    return {
        "verdicts": [{"i": i, "keep": i not in drop, "why": "w"} for i in range(n)],
        "same_claim": [{"members": m, "best": b} for m, b in groups],
        "superseded": [{"old": o, "new": w} for o, w in superseded],
    }


# --- parsing ----------------------------------------------------------------


def test_an_unreadable_answer_is_not_ok_and_drops_nothing() -> None:
    for text in ("", "no json here", '{"verdicts": "x"}', "{broken"):
        v = dv.parse_verdicts(text, 3)
        assert not v.ok and not v.drop and not v.folds
    assert dv.decide(3, [dv.parse_verdicts("nope", 3)]) == {}


def test_parsing_ignores_out_of_range_and_self_folds() -> None:
    text = json.dumps(
        {
            "verdicts": [
                {"i": 9, "keep": False},
                {"i": "x", "keep": False},
                {"i": 1, "keep": False},
            ],
            "same_claim": [{"members": [0, 7], "best": 0}, {"members": [2, 3], "best": 5}],
            "superseded": [{"old": 2, "new": 2}, {"old": 0, "new": 3}],
        }
    )
    v = dv.parse_verdicts("prefix " + text + " suffix", 4)
    assert v.ok and set(v.drop) == {1}
    assert v.folds == {0: 3}  # [0, 7] has one member in range; best 5 is not a member


# --- the decision rule ------------------------------------------------------


def test_a_fact_is_dropped_only_when_every_model_says_drop() -> None:
    a = dv.parse_verdicts(json.dumps(_answer(drop=[0, 1])), 4)
    b = dv.parse_verdicts(json.dumps(_answer(drop=[1, 2])), 4)
    assert set(dv.decide(4, [a, b])) == {1}


def test_a_model_that_failed_vetoes_every_non_durable_drop() -> None:
    a = dv.parse_verdicts(json.dumps(_answer(drop=[0, 1])), 4)
    assert dv.decide(4, [a, dv.Verdicts()]) == {}
    assert dv.decide(4, [dv.Verdicts(), a]) == {}  # a failed primary: nothing at all


def test_folds_come_from_the_primary_and_only_into_a_kept_rep() -> None:
    primary = dv.parse_verdicts(json.dumps(_answer(drop=[3], groups=[([0, 1], 1), ([2, 3], 3)])), 4)
    other = dv.parse_verdicts(json.dumps(_answer(drop=[3])), 4)
    out = dv.decide(4, [primary, other])
    assert out[0] == ("same_claim", 1)
    assert 2 not in out  # its rep (3) is itself dropped, so 2 carries the claim
    assert out[3][0].startswith("non_durable")


def test_a_fold_cycle_drops_nothing() -> None:
    v = dv.Verdicts(ok=True, folds={0: 1, 1: 0})
    assert dv.decide(2, [v]) == {}


# --- verify() ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_drops_folds_and_counts_tokens() -> None:
    facts = _facts("A long, complete claim.", "A claim.", "User ran ls.", "Pets: a dog.")
    ans = _answer(drop=[2], groups=[([0, 1], 0)])
    a, b = _Scripted(ans), _Scripted(ans)
    res = await dv.verify(facts, [a, b])
    assert [f["key"] for f in res.kept] == ["k0", "k3"]
    by = {f["key"]: f for f in res.dropped}
    assert by["k1"]["_duplicate_of"] == "k0" and by["k1"]["_verify"] == "same_claim"
    assert by["k2"]["_verify"].startswith("non_durable")
    assert res.tokens_in == 200 and res.tokens_out == 40 and res.answered


@pytest.mark.asyncio
async def test_any_failure_keeps_every_fact() -> None:
    facts = _facts("a", "b", "c", "d")
    res = await dv.verify(facts, [_Scripted(RuntimeError("boom")), _Scripted(_answer(drop=[0]))])
    assert res.kept == facts and not res.dropped
    res = await dv.verify(facts, [_Scripted("not json")])
    assert res.kept == facts and not res.answered


@pytest.mark.asyncio
async def test_a_long_capture_is_judged_in_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "memory_durability_verifier_max_facts_per_call", 3)
    client = _Scripted({"verdicts": []})
    await dv.verify(_facts(*"abcdefg"), [client])
    assert len(client.calls) == 3


def test_the_prompt_names_the_fail_safe_and_both_tasks() -> None:
    p = dv.VERIFIER_SYSTEM_PROMPT
    assert (
        "useful to recall in a week" in p
        and "test chatter" in p
        and '"same_claim"' in p
        and '"superseded"' in p
    )


# --- the pipeline step ---------------------------------------------------------


def _state(tags: list[str], values: list[str]) -> pipeline_state.WorkflowState:
    st = pipeline_state.WorkflowState(
        org="o", namespace="n", agent="a", session_id="s", messages=[], extra_tags=tags
    )
    st.extracted_facts = _facts(*values)
    return st


def test_the_step_runs_after_the_rules_and_before_the_write() -> None:
    ids = [s.step_id for s in mp.DEFAULT_PIPELINE]
    assert (
        ids.index("loom.drop_non_durable")
        < ids.index("loom.verify_durable")
        < ids.index("loom.bulk_write")
    )
    assert "loom.verify_durable" in mp.registered_step_ids()


@pytest.mark.asyncio
async def test_the_step_judges_coding_captures_only(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Scripted(_answer(drop=[0], n=2))
    monkeypatch.setattr(mp, "_verify_clients", lambda state: [client])
    st = await mp._REGISTRY["loom.verify_durable"](_state(["source:claude-code"], ["x", "y"]), {})
    assert [f["key"] for f in st.extracted_facts] == ["k1"] and st.skipped_unverified == 1
    assert not any(k.startswith("_") for f in st.extracted_facts for k in f)
    st = await mp._REGISTRY["loom.verify_durable"](_state([], ["x", "y"]), {})
    assert len(st.extracted_facts) == 2 and len(client.calls) == 1  # a personal chat: no call


def test_no_verifier_on_a_fake_stack_or_for_a_keyless_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.settings import settings

    st = _state(["source:codex"], [])
    monkeypatch.setattr(settings, "llm_provider", "fake")
    assert mp._verify_clients(st) == []
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "memory_extract_verify_models", "claude-haiku-4-5")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert mp._verify_clients(st) == []
    monkeypatch.setattr(settings, "memory_extract_verify_models", "")
    assert mp._verify_clients(st) == []


# --- speaker attribution ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_coding_capture_skips_speaker_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    async def _f(messages, **kw):
        from server.memory.app.extract import ExtractionResult

        seen.append(kw["speaker_attribution"])
        return ExtractionResult(
            facts=[],
            llm_result=LLMResult(text="", tokens_in=0, tokens_out=0, provider="p", model="m"),
            prompt_version="v",
        )

    monkeypatch.setattr("server.memory.app.extract.extract_from_messages", _f)
    for tags in (["source:claude-code"], []):
        st = pipeline_state.WorkflowState(
            org="o",
            namespace="n",
            agent="a",
            session_id="s",
            messages=[{"role": "user", "content": "hi"}],
            extra_tags=tags,
        )
        await mp._REGISTRY["loom.llm_extract"](st, {})
    assert seen == [False, True]


# --- content-free shapes ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The incident regarding the disk growth and docker logs was closed.",
        "The hypothesis regarding the occasional slow first call after idle was closed.",
        "Two incidents were reported, both from round 15's field lists.",
        "User provided an update for ClickGap regarding the state of play.",
        "The agent relied on finding_phase_d_pr114089.",
        "User's loom_context_pack call returned mentor, memories, and procedures.",
        "User's three Loom calls succeeded in the requested order.",
        "codex prefers squash merges for small PRs'}\"\n…[truncated]\npaths: /dev/shm/cc/s.",
    ],
)
def test_content_free_shapes_are_not_durable(text: str) -> None:
    assert df.non_durable_class({"value": text}) in ("vague", "session_action")


@pytest.mark.parametrize(
    "text",
    [
        "The issue with docker logs was identified as a runaway writer due to excessive logging.",
        "The disk was growing by 2.1 GB every 20 seconds due to a runaway writer.",
        "User noted two incidents in round 16, both related to field lists causing errors in the Runs page.",
        "Task inbox trust policy is owner-set mode only.",
    ],
)
def test_an_event_with_content_is_kept(text: str) -> None:
    assert df.non_durable_class({"value": text}) is None


# --- capture shapes from Claude Code and Codex sessions -----------------------


@pytest.mark.parametrize(
    "text",
    [
        "User set the preference 'test/loomcheck-cx/pref' to 'I prefer squash merges for small PRs'.",
        "User locked the preference 'test/loomcheck-cx/pref' as read-only.",
        "User deferred one of the two additional tasks.",
        "User marked the other additional task as wont_do.",
        "User called tracker test_signal for test 02346_text_index; it returned no_signal.",
        "User appended 'step 5b done' to loom working memory.",
        "User replaced loom working memory with 'loomcheck-cx complete'.",
        "User made a trivial edit to README.md, then called code impact on the diff.",
        "User created two more tasks after the initial task.",
        "User found five hits in memories related to the tracker rename.",
        "User noted that the context pack has almost nothing about the tracker rename itself.",
        "User noted that the Loom test gate indicates no test reaches server.core.naming::validate_namespace_name.",
        "User's loom_namespace_summary call for the default namespace reported 4,782 memories.",
        "User's loom_code_search results included: Duplicate checking is registered as tracker.duplicate_check.",
        "User noted that all five procedures returned by the context pack were marked 'unproven'.",
        "User noted that the key test/loomcheck/pref is locked and cannot be deleted without unlocking it.",
        "The value for test/loomcheck/pref is locked in read-only mode and cannot be updated.",
        "User's preference is still 'I prefer rebase merges for small PRs' (locked).",
        "User's tracker record states that 'Bad cast ColumnNullable' has no likely duplicate (verdict none, calibrated).",
        "The final states of the tasks are: 'loomcheck-cx: say hi' is in triage.",
        "The hook message for the GitHub issue search was: 'loom-code-shunt: …'",
        "Assistant deleted the 'loomcheck runbook' doc and noted a second doc was left untouched.",
        "User outlined server/clients/qa_intel.py with 29 entries.",
        "The scoring code without I/O is in server/tracker/domain/dupcheck.py.",
        "User reverted the edit to server/core/naming.py.",
        "The update on test/loomcheck/pref was successful, with the new version being 1790324377424849699.",
    ],
)
def test_live_capture_shapes_are_not_durable(text: str) -> None:
    assert df.non_durable_class({"value": text}) is not None


@pytest.mark.parametrize(
    "text",
    [
        "User prefers rebase merges for small PRs.",
        "User decided retries back off exponentially, capped at 30 seconds.",
        "User created an optimize skill for the plugin and MCP that consumes the audit.",
        "User merged PR #1805, which includes the specificity-ranked trimming feature.",
        "User identified that 22,730 rows in support-bot hold raw unquoted text due to a builder bug.",
        "Task inbox trust policy is owner-set mode only.",
        "Internal docs are stored as regular Loom documents with the same access as the code.",
    ],
)
def test_real_facts_keep_passing(text: str) -> None:
    assert df.non_durable_class({"value": text}) is None


# --- the coverage sweep in a one-shot coding session ------------------------------


def _extract_spy(seen: list[dict]):
    async def _f(messages, **kw):
        from server.memory.app.extract import ExtractionResult

        seen.append({"sweep": kw.get("coverage_sweep"), "source": kw.get("coverage_sweep_source")})
        return ExtractionResult(
            facts=[],
            llm_result=LLMResult(text="", tokens_in=0, tokens_out=0, provider="p", model="m"),
            prompt_version="v",
        )

    return _f


def _turns(n_user: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n_user):
        out += [
            {"role": "user", "content": f"ask {i}"},
            {"role": "assistant", "content": f"answer {i}"},
        ]
    return out


@pytest.mark.asyncio
async def test_a_one_shot_coding_capture_skips_the_auto_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict] = []
    monkeypatch.setattr("server.memory.app.extract.extract_from_messages", _extract_spy(seen))

    async def _none(org, ns):
        return None

    monkeypatch.setattr("server.memory.app.coverage_sweep_gate.resolve", _none)
    for tags, n in (
        (["source:claude-code"], 1),
        (["source:codex"], 3),
        (["source:claude-code"], 4),
        ([], 1),
    ):
        st = pipeline_state.WorkflowState(
            org="o", namespace="n", agent="a", session_id="s", messages=_turns(n), extra_tags=tags
        )
        await mp._REGISTRY["loom.llm_extract"](st, {})
    assert seen == [
        {"sweep": False, "source": "coding"},
        {"sweep": False, "source": "coding"},
        {"sweep": None, "source": ""},  # a working session keeps the auto gates
        {"sweep": None, "source": ""},  # a personal chat is untouched
    ]


@pytest.mark.asyncio
async def test_an_explicit_sweep_switch_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr("server.memory.app.extract.extract_from_messages", _extract_spy(seen))

    async def _on(org, ns):
        return True

    monkeypatch.setattr("server.memory.app.coverage_sweep_gate.resolve", _on)
    st = pipeline_state.WorkflowState(
        org="o",
        namespace="n",
        agent="a",
        session_id="s",
        messages=_turns(1),
        extra_tags=["source:claude-code"],
    )
    await mp._REGISTRY["loom.llm_extract"](st, {})
    st = pipeline_state.WorkflowState(
        org="o",
        namespace="n",
        agent="a",
        session_id="s",
        messages=_turns(1),
        extra_tags=["source:claude-code"],
        coverage_sweep=True,
    )
    await mp._REGISTRY["loom.llm_extract"](st, {})
    assert seen == [{"sweep": True, "source": "namespace"}, {"sweep": True, "source": "per_call"}]


# --- mined procedures from a trivial session -------------------------------------


@pytest.mark.asyncio
async def test_no_procedure_is_mined_from_a_short_session() -> None:
    from server.memory.app import trajectory_notes as tn

    class _NoCall:
        async def complete(self, *a, **k):
            raise AssertionError("the notes model must not be called")

    obs = [
        tn.Observation(
            memory_key=f"obs:s:{i}",
            kind="command_run",
            tool_name="Bash",
            narrative=f"ran step {i} of the test",
            error="",
            file_paths=(),
            created_at=None,
            valid_at=None,
        )
        for i in range(6)
    ]
    res = await tn.distill_notes(
        {"org": "o", "namespace": "n", "session_id": "s"}, obs, llm=_NoCall()
    )
    assert not res.procedure_written and res.llm_calls == 0
