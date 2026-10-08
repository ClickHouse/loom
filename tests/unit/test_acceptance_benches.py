"""The acceptance benches' pure parts: the report's
precision-first verdict, the per-archetype rollups, the hook fixture scorer
and the replay of the hook fixture transcripts — no server, no LLM."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "sdk" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bench import code_intel_qa as cq  # noqa: E402
from bench.acceptance import code_agent, harness, memory_app, plugin  # noqa: E402
from bench.acceptance._common import Metric, Report  # noqa: E402


def _report(*metrics: Metric) -> Report:
    r = Report(bench="b", archetype="a", base_url="u", consumer="c")
    for m in metrics:
        r.add(m)
    return r


def test_metric_passes_against_its_threshold_in_either_direction() -> None:
    assert Metric("x", 0.8, 0.75).passed() is True
    assert Metric("x", 0.7, 0.75).passed() is False
    assert Metric("x", 0.3, 0.4, higher_is_better=False).passed() is True
    assert Metric("x", None, 0.4).passed() is None
    assert Metric("x", 0.5, None).passed() is None
    assert Metric("x", 0.5, 0.4, unit="%").fmt_value() == " 50.0%"
    assert (
        Metric("x", 0.0123, 0.05, unit="$", higher_is_better=False).fmt_threshold() == "<= $0.0500"
    )


def test_verdict_is_precision_first() -> None:
    ok = _report(
        Metric("p", 0.9, 0.8), Metric("e", 1.0, 1.2, higher_is_better=False, kind="economy")
    )
    assert ok.verdict() == ("PASS", 0)
    econ_fail = _report(
        Metric("p", 0.9, 0.8), Metric("e", 2.0, 1.2, higher_is_better=False, kind="economy")
    )
    assert econ_fail.verdict() == ("FAIL (economy)", 1)
    prec_fail = _report(
        Metric("p", 0.5, 0.8), Metric("e", 1.0, 1.2, higher_is_better=False, kind="economy")
    )
    assert prec_fail.verdict() == ("FAIL (precision)", 1)
    both = _report(
        Metric("p", 0.5, 0.8), Metric("e", 9.0, 1.2, higher_is_better=False, kind="economy")
    )
    assert (
        both.verdict()[0] == "FAIL (precision)"
    )  # precision names the failure, whatever economy says
    assert _report(Metric("i", 1.0, None, kind="info")).verdict() == ("NO GATES MEASURED", 1)
    # a gated precision metric with no value is not a pass, whatever the economy says
    unmeasured = _report(Metric("p", None, 0.5), Metric("e", 1.0, 2.0, kind="economy"))
    assert unmeasured.verdict() == ("FAIL (precision not measured)", 1)
    smoke = _report(Metric("p", 0.9, 0.8))
    smoke.smoke = True
    assert smoke.verdict() == ("PASS (smoke only)", 0)


def test_render_is_one_screen_and_names_the_verdict() -> None:
    r = _report(
        Metric("precision thing", 0.9, 0.8, unit="%", n=36),
        Metric("calls per answer", 1.0, 1.2, higher_is_better=False, kind="economy"),
        Metric("context", 3.0, None, kind="info"),
    )
    r.probe = "probe[qa_agent] via code.index_status · served 6/6"
    r.stats = {"calls": 40, "attempts": 41, "retries": 1, "failures_by_code": {}}
    r.note("hello")
    text = r.render()
    assert text.count("\n") < 40
    assert "VERDICT: PASS" in text and "precision of what is served (first)" in text
    assert "the archetype's economy (second)" in text and "note: hello" in text
    assert "41 attempts (1 retries)" in text
    js = r.to_json()
    assert js["exit_code"] == 0 and js["metrics"][0]["passed"] is True


# -- code agent -------------------------------------------------------------------------


def test_code_agent_outcome_and_economy() -> None:
    q = cq.Question(qid="1", qtype="conceptual", query="x", gold={"paths": ["src/A.cpp"]})
    hit = cq.Retrieval(paths=["src/B.cpp", "src/A.cpp"])
    miss = cq.Retrieval(paths=["src/B.cpp"])
    assert code_agent.outcome_for(q, hit) == ("used_in_answer", ["src/A.cpp"])
    assert code_agent.outcome_for(q, miss) == ("ignored", ["src/B.cpp"])
    assert code_agent.outcome_for(q, cq.Retrieval(abstained=True, paths=["x"])) is None
    assert (
        code_agent.outcome_for(cq.Question(qid="2", qtype="symbol_def", query="s", gold={}), hit)
        is None
    )
    s = cq.Summary(n=10, errors=2, mean_tokens=1200.0)
    econ = code_agent.economy(s, retrieval_attempts=9, latencies=[5.0, 1.0, 3.0])
    assert econ["calls_per_answer"] == pytest.approx(9 / 8) and econ["p50_latency_ms"] == 3.0


def test_code_agent_dry_run_passes_on_synthetic_retrievals(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = code_agent.main(["--dry", "--limit", "20"])
    out = capsys.readouterr().out
    assert rc == 0 and "VERDICT: PASS" in out and "localization recall@5" in out


def test_code_agent_abstain_precision_is_unmeasured_when_nothing_abstained() -> None:
    probe = cq.Question(qid="p", qtype="abstain_probe", query="q", gold={})
    s = cq.summarize([(probe, cq.Retrieval(abstained=False, paths=["src/x.cpp"]))])
    args = argparse.Namespace(
        base_url="u",
        consumer="c",
        min_recall5=None,
        min_symbol_exact=None,
        min_callers_f1=None,
        min_abstain_precision=0.8,
        max_calls_per_answer=None,
        max_mean_tokens=None,
    )
    econ = {"calls_per_answer": 1.0, "mean_tokens": 10.0, "p50_latency_ms": 5.0}
    rep = code_agent.build_report(args, s, econ, outcomes_posted=0, probe_line="p")
    m = [m for m in rep.metrics if m.name == "abstain precision (probes)"][0]
    assert m.value is None and rep.verdict() == ("FAIL (precision not measured)", 1)


# -- memory app ---------------------------------------------------------------------------


def test_memory_app_rollups() -> None:
    v = memory_app.pi_llm._val
    assert memory_app.score_pi_answer(f"it is {v(3, 4)} now", 3, 5) == {
        "has_latest": True,
        "has_stale": False,
        "clean_correct": True,
        "abstain": False,
    }
    assert memory_app.score_pi_answer(f"{v(3, 1)} then {v(3, 4)}", 3, 5)["has_stale"] is True
    assert memory_app.score_pi_answer("no idea", 3, 5)["abstain"] is True
    rows = [
        {
            "depth": 5,
            "has_latest": True,
            "has_stale": False,
            "clean_correct": True,
            "abstain": False,
        },
        {
            "depth": 10,
            "has_latest": True,
            "has_stale": True,
            "clean_correct": False,
            "abstain": False,
        },
        {
            "depth": 15,
            "has_latest": False,
            "has_stale": True,
            "clean_correct": False,
            "abstain": False,
        },
        {"depth": 15, "error": "boom"},
    ]
    pi = memory_app.pi_rollup(rows)
    assert pi["n"] == 3 and pi["errors"] == 1 and pi["accuracy"] == pytest.approx(2 / 3)
    assert pi["n_deep"] == 2 and pi["stale_rate"] == 1.0
    assert pi["by_depth"][15] == {
        "accuracy": 0.0,
        "stale_rate": 1.0,
        "clean_correct": 0.0,
        "n": 1.0,
    }
    assert sorted(pi["by_depth"]) == [5, 10, 15]
    lme = memory_app.lme_rollup(
        [
            {
                "question_type": "knowledge-update",
                "judge_label": "CORRECT",
                "stale_answer": "",
                "recalled": "True",
            },
            {
                "question_type": "knowledge-update",
                "judge_label": "WRONG",
                "stale_answer": "1",
                "recalled": "False",
            },
            {
                "question_type": "knowledge-update",
                "judge_label": "WRONG",
                "stale_answer": "0",
                "recalled": "True",
            },
            {"question_type": "multi-session", "judge_label": "CORRECT"},
        ]
    )
    assert (
        lme["n"] == 3
        and lme["accuracy"] == pytest.approx(1 / 3)
        and lme["stale_share_of_wrong"] == 0.5
    )
    args = argparse.Namespace(
        base_url="http://x",
        lme_dataset="d.json",
        lme_seed=42,
        lme_limit=30,
        lme_top_k=3,
        lme_concurrency=2,
    )
    cmd = memory_app.lme_command(args, Path("/tmp/o.csv"))
    assert "--token" not in cmd
    assert "--question-type" in cmd and "knowledge-update" in cmd and "--stale-check" in cmd


def test_memory_app_report_is_smoke_below_thirty_items() -> None:
    args = argparse.Namespace(
        base_url="u",
        consumer="c",
        min_latest_accuracy=0.50,
        max_stale_rate=0.7,
        pi_min_depth=10,
        min_knowledge_update=0.65,
        max_usd_per_ask=0.05,
    )
    pi = {
        "accuracy": 0.9,
        "stale_rate": 0.5,
        "clean_correct": 0.6,
        "clean_correct_deep": 0.6,
        "n": 24.0,
        "n_deep": 16.0,
        "errors": 0.0,
    }
    lme = {"accuracy": 0.7, "n": 10.0, "stale_share_of_wrong": 0.5, "wrong": 3.0, "recall": 0.8}
    cost = {"memory.ask": {"ops": 24.0, "cost_usd": 0.24, "latency_p50_ms": 3000.0}}
    rep = memory_app.build_report(args, pi, lme, cost, probe_line="p", lme_note="")
    assert rep.smoke and rep.verdict() == ("PASS (smoke only)", 0)
    ask = [m for m in rep.metrics if m.name.startswith("memory.ask $")][0]
    assert ask.value == pytest.approx(0.01) and ask.passed() is True


# -- plugin -------------------------------------------------------------------------------------


def test_plugin_hook_fixture_scorer_and_replay() -> None:
    fx = plugin.load_hook_fixture()
    perfect = [{"id": c["id"], "reason": c["expect"]} for c in fx["cases"]]
    s = plugin.score_hook_decisions(fx, perfect)
    assert s["fidelity"] == 1.0 and s["n"] == len(fx["cases"]) == 12
    broken = perfect[:-1] + [{"id": perfect[-1]["id"], "reason": "hit"}]
    s2 = plugin.score_hook_decisions(fx, broken)
    assert (
        s2["fidelity"] == pytest.approx(11 / 12)
        and s2["mismatches"][0]["expected"] == "not_indexed"
    )
    rp = plugin.replay_fixture()
    m = plugin.replay_metrics(rp["cells"])
    assert rp["tasks"] == 1 and "A" in rp["cells"] and "C" in rp["cells"]
    assert m["tool_call_reduction"] is not None and m["bypass_calls"] == 0.0
    assert rp["denied_paths"] == ["src/Compression/CompressionFactory.cpp"]


def test_plugin_hook_fixture_under_node_matches_the_plugin() -> None:
    decisions = plugin.run_hook_fixture()
    if decisions is None:
        pytest.skip("node or code-shunt.mjs not available")
    s = plugin.score_hook_decisions(plugin.load_hook_fixture(), decisions)
    assert s["fidelity"] == 1.0, s["mismatches"]


def test_plugin_redirect_precision_and_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    q1 = cq.Question(qid="1", qtype="conceptual", query="x", gold={"paths": ["src/A.cpp"]})
    q2 = cq.Question(qid="2", qtype="conceptual", query="y", gold={"paths": ["src/B.cpp"]})
    q3 = cq.Question(qid="3", qtype="conceptual", query="z", gold={"paths": ["src/C.cpp"]})
    pairs = [
        (q1, cq.Retrieval(paths=["src/A.cpp"])),
        (q2, cq.Retrieval(paths=["src/X.cpp", "src/B.cpp"])),
        (q3, cq.Retrieval(error="boom")),
    ]
    r = plugin.redirect_precision(pairs)
    assert (
        r["n"] == 2
        and r["wrong_file_rate_top1"] == 0.5
        and r["hit_at_5"] == 1.0
        and r["unserved"] == 1
    )
    rc = plugin.main(["--dry"])
    out = capsys.readouterr().out
    assert "tool-call reduction" in out and "VERDICT" in out
    assert rc in (0, 1)


# -- harness --------------------------------------------------------------------------------------


def test_harness_rollups_and_smoke_flag() -> None:
    rows = [
        {"label": "CORRECT", "abstained": "False", "category_name": "single-hop"},
        {"label": "WRONG", "abstained": "True", "category_name": "single-hop"},
        {"label": "CORRECT", "abstained": "False", "category_name": "temporal"},
        {"label": "ERROR", "abstained": "False", "category_name": "temporal"},
    ]
    j = harness.judged_rollup(rows)
    assert j["n"] == 3 and j["accuracy"] == pytest.approx(2 / 3) and j["errors"] == 1
    assert j["abstain_rate"] == pytest.approx(1 / 3) and j["by_category"] == {
        "single-hop": "1/2",
        "temporal": "1/1",
    }
    cost = harness.cost_rollup(
        {
            "memory.ask": {"ops": 3.0, "cost_usd": 0.06, "latency_p50_ms": 2500.0},
            "memory.set_from_messages": {"ops": 20.0, "cost_usd": 0.4, "latency_p50_ms": 9000.0},
        },
        correct=2.0,
        answered=3.0,
    )
    assert cost["usd_per_ask"] == pytest.approx(0.02) and cost[
        "usd_per_correct_answer"
    ] == pytest.approx(0.03)
    assert cost["usd_ingest_per_session"] == pytest.approx(0.02) and cost[
        "usd_total"
    ] == pytest.approx(0.46)
    args = argparse.Namespace(base_url="u", consumer="c", min_accuracy=0.55, max_usd_per_answer=0.1)
    rep = harness.build_report(args, j, cost, probe_line="p")
    assert rep.smoke and rep.verdict() == ("PASS (smoke only)", 0)
    cmd = harness.locomo_command(
        argparse.Namespace(
            dataset="d",
            base_url="u",
            org="o",
            conversations=1,
            questions=30,
            qa_concurrency=4,
            categories="",
        ),
        Path("/tmp/x.csv"),
        "pfx",
    )
    assert (
        "--reader" in cmd
        and "ask" in cmd
        and "--keep-namespaces" in cmd
        and "--limit-questions" in cmd
    )


# -- the follow-next arm, floors, replay and slices ------------------------------------------------


class _FakeClient:
    """Scripted ReferenceClient stand-in: `responses[op]` is a list consumed
    in order (a dict = ok result; an Exception = failed call)."""

    consumer = "test"

    def __init__(self, responses: dict[str, list[object]]):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, op: str, body: dict, **_kw: object) -> object:
        from loom.reference_client import CallResult

        self.calls.append((op, dict(body)))
        queue = self.responses.get(op) or []
        scripted = queue.pop(0) if queue else {"ok": True}
        if isinstance(scripted, Exception):
            return CallResult(op=op, ok=False, code="UPSTREAM", error=str(scripted))
        return CallResult(op=op, ok=True, data=dict(scripted), elapsed_ms=12.0)


def _conflict(peer: str, sim: float = 0.91) -> dict:
    return {
        "conflict_verdict": "conflict",
        "peers": [{"memory_key": peer, "similarity": sim}],
        "next": (
            f"verdict=conflict: this write disagrees with {peer!r} (similarity {sim:.2f}). "
            f"If the fact changed, revise that row: memory.update(memory_key={peer!r}, "
            "value=...). Writing a third copy will not settle it."
        ),
        "noop": True,
        "dry_run": True,
        "version": 0,
    }


def test_follow_next_target_reads_only_a_conflict_with_an_update_hint() -> None:
    assert memory_app.follow_next_target(_conflict("quorvex0-status-0")) == "quorvex0-status-0"
    assert memory_app.follow_next_target({"conflict_verdict": "clear", "next": ""}) is None
    merge = {"conflict_verdict": "merge", "next": "memory.pin(memory_key='k', value=...)"}
    assert memory_app.follow_next_target(merge) is None
    assert memory_app.follow_next_target({"conflict_verdict": "conflict", "next": ""}) is None
    odd = _conflict("it's")  # repr() renders this key with double quotes
    assert 'memory_key="it\'s"' in odd["next"]
    assert memory_app.follow_next_target(odd) == "it's"


@pytest.mark.asyncio
async def test_follow_next_arm_revises_the_peer_and_control_writes_the_copy() -> None:
    from collections import Counter

    # follow_next: k=0 dry-run is clear → real set; k=1 dry-run conflicts → update the peer
    fc = _FakeClient(
        {
            "memory.set": [
                {"conflict_verdict": "clear", "next": "", "dry_run": True, "version": 0},
                {"conflict_verdict": "clear", "version": 1, "memory_id": "m0"},
                _conflict("quorvex0-status-0"),
            ],
            "memory.update": [{"conflict_verdict": "clear", "version": 2}],
        }
    )
    v: Counter[str] = Counter()
    ok, err, followed = await memory_app.pi_write(
        fc, "ns", "Quorvex0", 0, 0, ingest="set", arm="follow_next", verdicts=v
    )
    assert ok and not followed
    ok, err, followed = await memory_app.pi_write(
        fc, "ns", "Quorvex0", 0, 1, ingest="set", arm="follow_next", verdicts=v
    )
    assert ok and followed and err == ""
    ops = [op for op, _ in fc.calls]
    assert ops == ["memory.set", "memory.set", "memory.set", "memory.update"]
    assert fc.calls[0][1]["dry_run"] is True and "dry_run" not in fc.calls[1][1]
    assert fc.calls[2][1]["dry_run"] is True
    upd = fc.calls[3][1]
    assert upd["memory_key"] == "quorvex0-status-0" and "follow-next" in upd["reason"]
    assert memory_app.pi_llm._val(0, 1) in upd["value"]
    assert v == Counter({"dry:clear": 1, "set:clear": 1, "dry:conflict": 1, "update:clear": 1})
    # control: one real set per update, the verdict recorded, `next` ignored
    cc = _FakeClient({"memory.set": [_conflict("quorvex0-status-0") | {"dry_run": False}]})
    vc: Counter[str] = Counter()
    ok, _err, followed = await memory_app.pi_write(
        cc, "ns", "Quorvex0", 0, 1, ingest="set", arm="control", verdicts=vc
    )
    assert ok and not followed and [op for op, _ in cc.calls] == ["memory.set"]
    assert "dry_run" not in cc.calls[0][1] and vc == Counter({"set:conflict": 1})
    # a failed update is the episode's error, not a silent fallback to a copy
    bad = _FakeClient({"memory.set": [_conflict("p")], "memory.update": [RuntimeError("boom")]})
    ok, err, followed = await memory_app.pi_write(
        bad, "ns", "E", 0, 1, ingest="set", arm="follow_next", verdicts=Counter()
    )
    assert not ok and err.startswith("update ") and not followed
    # the extract path is unchanged: one set_from_messages, no verdict
    ex = _FakeClient({"memory.set_from_messages": [{"written": 1}]})
    ok, _err, followed = await memory_app.pi_write(
        ex, "ns", "E", 0, 0, ingest="extract", arm="control", verdicts=Counter()
    )
    assert ok and not followed and ex.calls[0][0] == "memory.set_from_messages"


@pytest.mark.asyncio
async def test_pi_episode_asks_with_reinforce_off_and_carries_arm_and_verdicts() -> None:
    v = memory_app.pi_llm._val
    fc = _FakeClient(
        {
            "memory.set": [{"conflict_verdict": "clear", "version": 1}] * 2,
            "memory.ask": [{"answer": f"It is {v(0, 1)}.", "memory_keys": ["k"], "source": "s"}],
        }
    )
    row = await memory_app.pi_episode(fc, "ns", 0, 2, outcomes=True, ingest="set", arm="control")
    assert row["clean_correct"] and row["arm"] == "control" and row["followed"] == 0
    assert row["verdicts"] == {"set:clear": 2}
    ask = [b for op, b in fc.calls if op == "memory.ask"][0]
    assert ask["reinforce"] is False
    assert [op for op, _ in fc.calls].count("memory.outcome") == 1


def test_paired_delta_and_clean_correct_floor_and_verdict_notes() -> None:
    rows = []
    for ep in range(8):
        for d in (5, 10, 15):
            rows.append(
                {
                    "ep": ep,
                    "depth": d,
                    "arm": "control",
                    "has_latest": True,
                    "has_stale": ep % 2 == 0,
                    "clean_correct": ep % 2 == 1,
                    "abstain": False,
                    "verdicts": {"set:conflict": d},
                    "followed": 0,
                }
            )
            rows.append(
                {
                    "ep": ep,
                    "depth": d,
                    "arm": "follow_next",
                    "has_latest": True,
                    "has_stale": False,
                    "clean_correct": True,
                    "abstain": False,
                    "verdicts": {"dry:conflict": d - 1, "update:clear": d - 1},
                    "followed": d - 1,
                }
            )
    pd = memory_app.paired_delta(rows, min_depth=10)
    assert pd["n_pairs"] == 16 and pd["delta"] == pytest.approx(0.5)
    assert pd["lb"] is not None and pd["lb"] > 0 and pd["ub"] <= 1.0
    # pairs missing one arm are dropped; nothing at depth → None
    assert memory_app.paired_delta(rows[:1])["n_pairs"] == 0
    ctl = memory_app.pi_rollup([r for r in rows if r["arm"] == "control"], min_depth=10)
    assert ctl["clean_correct_deep"] == pytest.approx(0.5)
    assert ctl["verdicts"] == {"set:conflict": 8 * 30} and ctl["followed"] == 0
    fn = memory_app.pi_rollup([r for r in rows if r["arm"] == "follow_next"], min_depth=10)
    assert fn["clean_correct_deep"] == 1.0 and fn["followed"] == 8 * 27
    assert fn["by_depth"][10]["clean_correct"] == 1.0
    args = argparse.Namespace(
        base_url="u",
        consumer="c",
        min_latest_accuracy=0.5,
        max_stale_rate=0.7,
        pi_min_depth=10,
        min_clean_correct=0.6,
        min_knowledge_update=0.65,
        max_usd_per_ask=0.05,
    )
    cost = {"memory.ask": {"ops": 48.0, "cost_usd": 0.1, "latency_p50_ms": 3000.0}}
    rep = memory_app.build_report(
        args,
        {**ctl, "arm": "control"},
        {"accuracy": None, "n": 0.0},
        cost,
        probe_line="p",
        lme_note="",
        arm_pi={"control": ctl, "follow_next": fn},
        paired=pd,
        replay={
            "namespace": "t",
            "days": 7,
            "ledger": {"ops": 100.0, "p50_ms": 19800.0, "usd_per_ask": 0.07, "errors": 1.0},
            "replay": {"n": 30.0, "errors": 0.0, "p50_ms": 8000.0, "abstain_rate": 0.1},
        },
    )
    # the clean_correct floor is a precision gate: 50% < 60% fails the bench
    assert rep.verdict() == ("FAIL (precision)", 1)
    names = [m.name for m in rep.metrics]
    assert "stale retrieval: clean correct (depth>=10)" in names
    assert any(n.startswith("paired Δ clean correct") for n in names)
    assert any(n.startswith("tenant t: ledger ask p50") for n in names)
    text = rep.render()
    assert "excludes 0" in text and "followed next 216" in text and "19800 ms" in text
    # at the shipped floor the same rows pass
    args.min_clean_correct = memory_app.CLEAN_CORRECT_FLOOR
    rep2 = memory_app.build_report(
        args,
        {**ctl, "arm": "control"},
        {"accuracy": None, "n": 0.0},
        cost,
        probe_line="p",
        lme_note="",
    )
    assert rep2.verdict()[0] == "PASS"


def test_plain_queries_drops_ledger_digests() -> None:
    qs, hashed = memory_app.plain_queries(
        ["sha256:64465f8d3144e27b/27", "what did I decide?", "sha256:fccbcd02c0364fca/26"]
    )
    assert qs == ["what did I decide?"] and hashed == 2
    assert memory_app.plain_queries([]) == ([], 0)


def test_pick_evenly_is_deterministic_and_keeps_order() -> None:
    items = [str(i) for i in range(100)]
    picked = memory_app.pick_evenly(items, 30)
    assert len(picked) == 30 and picked == sorted(picked, key=int) and picked[0] == "0"
    assert memory_app.pick_evenly(items[:5], 30) == items[:5]
    assert memory_app.pick_evenly([], 30) == [] and memory_app.pick_evenly(items, 0) == []


def test_harness_slices_preset_floors_and_ask_json_rides_the_command() -> None:
    args = argparse.Namespace(
        slice="conv26-30", conversations=None, questions=None, min_accuracy=None
    )
    harness.apply_slice(args)
    assert (args.conversations, args.questions, args.min_accuracy) == (1, 30, 0.85)
    args = argparse.Namespace(
        slice="stratified-100", conversations=None, questions=None, min_accuracy=0.7
    )
    harness.apply_slice(args)
    assert (args.conversations, args.questions, args.min_accuracy) == (10, 10, 0.7)
    cmd_args = argparse.Namespace(
        dataset="d.json",
        base_url="u",
        org="o",
        conversations=10,
        questions=10,
        qa_concurrency=2,
        categories="",
        ask_json='{"reinforce": false}',
    )
    cmd = harness.locomo_command(cmd_args, Path("/tmp/o.csv"), "pfx")
    assert "--token" not in cmd, "the bearer rides the environment, not argv"
    assert cmd[cmd.index("--limit-conversations") + 1] == "10"
    assert cmd[cmd.index("--ask-json") + 1] == '{"reinforce": false}'


def test_plugin_economy_metric_is_smoke_below_ten_tasks() -> None:
    args = argparse.Namespace(
        base_url="u",
        consumer="c",
        min_hook_fidelity=1.0,
        max_wrong_file_rate=0.4,
        min_hit5=0.75,
        min_tool_call_reduction=0.0,
    )
    replay = {
        "wrong_file_rate_replay": 0.2,
        "tool_call_reduction": 0.0,
        "denied_calls": 1.0,
        "bypass_calls": 0.0,
        "acc5_view_b_plugin": 0.9,
        "tasks": 1.0,
    }
    hook = {"fidelity": 1.0, "n": 12, "mismatches": []}
    redirect = {"wrong_file_rate_top1": 0.3, "hit_at_5": 0.86, "n": 36.0}
    kw = dict(hook=hook, redirect=redirect, outlines=(1, 1), outcomes_posted=0, probe_line="p")
    rep = plugin.build_report(args, replay=replay, **kw)
    econ = [m for m in rep.metrics if m.name.startswith("tool-call reduction")][0]
    assert econ.kind == "info" and econ.threshold is None and "smoke" in econ.note
    assert rep.smoke and rep.verdict() == ("PASS (smoke only)", 0)
    rep10 = plugin.build_report(args, replay={**replay, "tasks": 10.0}, **kw)
    econ10 = [m for m in rep10.metrics if m.name.startswith("tool-call reduction")][0]
    assert econ10.kind == "economy" and econ10.threshold == 0.0 and not rep10.smoke


def test_pi_floors_follow_the_depth_slice_and_flags_override() -> None:
    a = argparse.Namespace(
        pi_depths="10,15,20", min_latest_accuracy=None, max_stale_rate=None, min_clean_correct=None
    )
    memory_app.resolve_pi_floors(a)
    assert (a.min_latest_accuracy, a.max_stale_rate, a.min_clean_correct) == (0.20, 0.80, 0.20)
    assert a.pi_floors_slice == "10,15,20"
    b = argparse.Namespace(
        pi_depths="5, 10,15", min_latest_accuracy=None, max_stale_rate=0.6, min_clean_correct=None
    )
    memory_app.resolve_pi_floors(b)
    assert (b.min_latest_accuracy, b.max_stale_rate, b.min_clean_correct) == (0.50, 0.6, 0.50)
    c = argparse.Namespace(
        pi_depths="7,9", min_latest_accuracy=None, max_stale_rate=None, min_clean_correct=None
    )
    memory_app.resolve_pi_floors(c)
    assert c.min_latest_accuracy == 0.20 and "no preset" in c.pi_floors_slice
    # a default-slice reading passes its own floors and fails another slice's
    for floors, want in (
        (memory_app.PI_FLOORS["10,15,20"], "PASS"),
        (memory_app.PI_FLOORS["5,10,15"], "FAIL (precision)"),
    ):
        args = argparse.Namespace(
            base_url="u",
            consumer="c",
            pi_min_depth=10,
            min_knowledge_update=0.65,
            max_usd_per_ask=0.05,
            **floors,
        )
        pi = {
            "accuracy": 0.25,
            "stale_rate": 0.75,
            "clean_correct": 0.25,
            "clean_correct_deep": 0.25,
            "n": 48.0,
            "n_deep": 48.0,
            "errors": 0.0,
        }
        rep = memory_app.build_report(
            args,
            pi,
            {"accuracy": None, "n": 0.0},
            {"memory.ask": {"ops": 48.0, "cost_usd": 0.11}},
            probe_line="p",
            lme_note="",
        )
        assert rep.verdict()[0] == want


def test_a_broken_shunt_module_fails_instead_of_skipping(tmp_path: Path) -> None:
    import shutil

    if shutil.which("node") is None:
        pytest.skip("node not available")
    broken = tmp_path / "code-shunt.mjs"
    broken.write_text("throw new Error('broken on import');\n")
    with pytest.raises(plugin.HookRunError, match="exited"):
        plugin.run_hook_fixture(mjs=broken)


def test_pi_update_dates_increase_at_every_depth() -> None:
    """valid_at orders updates by date, so a date that wrapped would rank an older
    value as current at depth."""
    dates = [memory_app._pi_text("e", k, "v")[0] for k in range(40)]
    assert dates == sorted(dates) and len(set(dates)) == 40
    assert dates[0] == "2026-01-02"


def test_erase_namespaces_goes_through_bench_namespaces_and_skips_external_backends() -> None:
    import asyncio
    import json

    from bench.acceptance import _common

    ok = _FakeClient({"erasure.request": [{"erasure_id": "e1"}]})
    ok.org = "o"
    assert asyncio.run(_common.erase_namespaces(ok, ["b", "a", "a"])) == 2
    op, body = ok.calls[0]
    assert op == "erasure.request" and body["external"] is False and body["org"] == "o"
    assert json.loads(body["scope_value"]) == {"namespaces": ["a", "b"], "purge": True}
    refused = _FakeClient({"erasure.request": [RuntimeError("AUTH_INSUFFICIENT")]})
    refused.org = "o"
    assert asyncio.run(_common.erase_namespaces(refused, ["a"])) == 0
    assert "refused" in _common.erasure_note(0, 1)
    assert asyncio.run(_common.erase_namespaces(refused, [])) == 0
