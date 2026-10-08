"""Pure parts of the with/without-Loom benches: answer parsing, gold files, the
full-history rendering and the grep strings."""

from bench.agent_localization_ab import answer_files, gold_files, score
from bench.code_grep_baseline import qualified_of, symbol_of
from bench.lme_full_context import history_text


def test_gold_files_reads_the_patch_headers():
    patch = "diff --git a/src/a.py b/src/a.py\n+x\ndiff --git a/b.py b/b.py\n-y\n"
    assert gold_files(patch) == ["b.py", "src/a.py"]


def test_answer_files_takes_the_last_json_list():
    text = 'I looked at ["x.py"].\nFinal:\n["./src/a.py", "src/b.py", "c.py", "d.py"]'
    assert answer_files(text) == ["src/a.py", "src/b.py", "c.py"]
    assert answer_files("no list here") == []


def test_score_hit_at_1_and_3():
    row = score({"files": ["b.py", "a.py"]}, ["a.py"])
    assert row["hit_at_1"] is False and row["hit_at_3"] is True


def test_history_text_orders_sessions_by_date():
    item = {
        "haystack_dates": ["2023/05/21", "2023/05/20"],
        "haystack_sessions": [
            [{"role": "user", "content": "later"}],
            [{"role": "user", "content": "earlier"}],
        ],
    }
    text = history_text(item)
    assert text.index("earlier") < text.index("later")
    assert "Session Date: 2023/05/20" in text


def test_grep_strings_drop_the_namespace():
    q = {"query": "x", "gold": {"qualified_name": "DB::IColumn::insert"}}
    assert symbol_of(q) == "insert"
    assert qualified_of(q) == "IColumn::insert"
    assert qualified_of({"query": "DB::executeQuery", "gold": {}}) == "executeQuery"


def test_question_scoring_reads_gold_entries_as_path_prefixes():
    from bench.agent_localization_ab import path_matches, score_question

    assert path_matches("src/Access/EnabledQuota.cpp", "src/Access/EnabledQuota")
    assert path_matches("src/Access/QuotaCache.h", "src/Access/")
    assert not path_matches("src/AccessControl.cpp", "src/Access")
    gold = ["src/Interpreters/AsynchronousInsertQueue.cpp", "src/Interpreters"]
    row = score_question({"files": ["src/Interpreters/Context.cpp"]}, gold)
    assert (row["hit_at_1"], row["hit_at_3"], row["area_hit_at_3"]) == (False, False, True)


def test_tasks_from_questions_keeps_the_chosen_type_with_gold_paths(tmp_path):
    from argparse import Namespace

    from bench.agent_localization_ab import QUESTION_PROMPT, tasks_from_questions

    qs = tmp_path / "q.jsonl"
    qs.write_text(
        "// header\n"
        '{"id": "co1", "type": "conceptual", "query": "q1", "gold": {"paths": ["a/b"]}}\n'
        '{"id": "sd1", "type": "symbol_def", "query": "q2", "gold": {"qualified_name": "X"}}\n'
    )
    args = Namespace(questions=str(qs), question_types="conceptual", checkout="/c", namespace="ns")
    tasks = tasks_from_questions(args)
    assert [t["id"] for t in tasks] == ["co1"]
    assert tasks[0]["prompt"] is QUESTION_PROMPT and tasks[0]["namespace"] == "ns"


def test_summarize_reports_means_per_arm_and_skips_errored_runs():
    from bench.agent_localization_ab import summarize

    base = {"hit_at_1": True, "hit_at_3": True, "output_tokens": 10, "turns": 2, "wall_s": 1.0}
    rows = [
        {**base, "arm": "without", "input_tokens": 100, "cost_usd": 0.02},
        {**base, "arm": "without", "input_tokens": 300, "cost_usd": 0.04, "hit_at_1": False},
        {**base, "arm": "with", "input_tokens": 50, "cost_usd": 0.01},
        {**base, "arm": "with", "error": "boom"},
    ]
    out = summarize(rows)
    assert out["without"]["n"] == 2 and out["without"]["input_tokens_mean"] == 200
    assert out["without"]["hit_at_1"] == 0.5
    assert out["with"]["n"] == 1 and "area_hit_at_3" not in out["with"]


def test_stream_result_counts_every_tool_call_and_keeps_the_result():
    import json

    from bench.agent_localization_ab import stream_result

    def call(name):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}

    lines = [
        json.dumps(call("Grep")),
        "not json",
        json.dumps(call("mcp__loom__loom_code_search")),
        json.dumps(call("Grep")),
        json.dumps({"type": "result", "num_turns": 3, "total_cost_usd": 0.01}),
    ]
    result, tools = stream_result("\n".join(lines))
    assert result["num_turns"] == 3
    assert tools == {"Grep": 2, "mcp__loom__loom_code_search": 1}


def test_stream_trace_pairs_each_call_with_its_result_and_the_context():
    import json

    from bench.agent_localization_ab import stream_trace

    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {"input_tokens": 5, "cache_read_input_tokens": 100},
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "x"}}
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a" * 400}]
                },
            }
        ),
        json.dumps({"type": "result", "num_turns": 1}),
    ]
    result, tools, steps = stream_trace("\n".join(lines))
    assert result["num_turns"] == 1 and tools == {"Grep": 1}
    assert steps[0]["context"] == 105
    assert steps[0]["calls"][0]["tool"] == "Grep"
    assert 100 <= steps[0]["calls"][0]["result_tokens"] <= 110


def test_caller_key_compares_the_last_two_components():
    from bench.agent_localization_ab import caller_key

    assert caller_key("DB::MergeTreeSink::consume") == "MergeTreeSink::consume"
    assert caller_key("`MergeTreeSink::consume(Chunk &) const`") == "MergeTreeSink::consume"
    assert caller_key("DB::executeQueryImpl") == "DB::executeQueryImpl"
    assert caller_key("DB::$anonymous_namespace_src/A.cpp::helper") == "DB::helper"
    assert caller_key("db::mergetreesink::consume") != caller_key("DB::MergeTreeSink::consume")


def test_answer_callers_reads_the_last_string_list_across_lines():
    from bench.agent_localization_ab import answer_callers

    text = 'Saw ["x"] early.\nCallers:\n[\n  "DB::A::f",\n  "DB::B::g",\n  "DB::A::f"\n]\n'
    assert answer_callers(text) == ["DB::A::f", "DB::B::g"]
    assert answer_callers("none found []") == []
    assert answer_callers("no list") == []


def test_score_callers_precision_recall_f1():
    from bench.agent_localization_ab import score_callers

    gold = ["DB::A::f", "DB::B::g", "DB::C::h", "DB::D::k"]
    row = score_callers({"callers": ["A::f", "NS::B::g", "DB::E::x"]}, gold)
    assert (row["caller_precision"], row["caller_recall"]) == (0.667, 0.5)
    assert row["caller_f1"] == 0.571
    empty = score_callers({"callers": []}, gold)
    assert (empty["caller_precision"], empty["caller_recall"], empty["caller_f1"]) == (0, 0, 0)
    exact = score_callers({"callers": ["DB::D::k", "C::h", "B::g", "A::f"]}, gold)
    assert exact["caller_f1"] == 1.0


def test_tasks_from_questions_routes_callers_hard_and_file_scored_types(tmp_path):
    from argparse import Namespace

    from bench.agent_localization_ab import (
        CALLERS_PROMPT,
        QUESTION_PROMPT,
        answer_callers,
        score_callers,
        score_question,
        tasks_from_questions,
    )

    qs = tmp_path / "q.jsonl"
    qs.write_text(
        "// header\n"
        '{"id": "ch1", "type": "callers_hard", "query": "q1",'
        ' "gold": {"symbol": "DB::A::f", "expected_callers": ["DB::B::g"]}}\n'
        '{"id": "mh1", "type": "multi_hop", "query": "q2", "gold": {"paths": ["src/a"]}}\n'
        '{"id": "rc1", "type": "recent", "query": "q3", "gold": {"paths": ["src/b.cpp"]}}\n'
        '{"id": "co1", "type": "conceptual", "query": "q4", "gold": {"paths": ["src/c"]}}\n'
    )
    args = Namespace(
        questions=str(qs),
        question_types="callers_hard,multi_hop,recent",
        checkout="/c",
        namespace="ns",
    )
    by_id = {t["id"]: t for t in tasks_from_questions(args)}
    assert sorted(by_id) == ["ch1", "mh1", "rc1"]
    ch = by_id["ch1"]
    assert ch["prompt"] is CALLERS_PROMPT and ch["score"] is score_callers
    assert ch["gold"] == ["DB::B::g"] and ch["answer"] == ("callers", answer_callers)
    for qid in ("mh1", "rc1"):
        t = by_id[qid]
        assert t["prompt"] is QUESTION_PROMPT and t["score"] is score_question and "answer" not in t
        assert t["namespace"] == "ns"


def test_summarize_keeps_file_and_caller_metrics_apart():
    from bench.agent_localization_ab import summarize

    base = {"arm": "with", "input_tokens": 10, "output_tokens": 1, "turns": 1, "cost_usd": 0.0}
    rows = [
        {**base, "wall_s": 1.0, "hit_at_1": True, "hit_at_3": True, "area_hit_at_3": True},
        {**base, "wall_s": 1.0, "caller_precision": 1.0, "caller_recall": 0.5, "caller_f1": 0.667},
        {**base, "wall_s": 1.0, "caller_precision": 0.5, "caller_recall": 0.5, "caller_f1": 0.5},
    ]
    out = summarize(rows)["with"]
    assert out["n"] == 3 and out["hit_at_1"] == 1.0 and out["area_hit_at_3"] == 1.0
    assert out["n_callers"] == 2 and out["caller_precision"] == 0.75
    assert out["caller_recall"] == 0.5 and out["caller_f1"] == 0.584
    only_callers = summarize(rows[1:])["with"]
    assert "hit_at_1" not in only_callers and only_callers["caller_f1"] == 0.584


def test_a_file_the_index_shows_at_its_new_path_scores_as_the_gold_one() -> None:
    from bench.agent_localization_ab import score_question

    moved = {"src/Exec/V1/": "src/Exec/"}
    files = ["src/Exec/V1/PipelineExecutor.cpp", "src/Other.cpp"]
    row = score_question({"files": list(files)}, ["src/Exec/PipelineExecutor"], moved)
    assert row["hit_at_1"] and row["hit_at_3"] and row["area_hit_at_3"]
    assert not score_question({"files": list(files)}, ["src/Exec/PipelineExecutor"])["hit_at_3"]


def test_a_real_work_question_is_asked_in_prose_and_keeps_the_reply(tmp_path):
    import argparse
    import json

    from bench.agent_localization_ab import WORK_PROMPT, tasks_from_questions

    q = {
        "id": "wk1",
        "kind": "explain",
        "query": "how does FINAL pick the surviving row?",
        "reference": {"answer": "…", "key_points": ["a"], "files": ["src/a.cpp"]},
    }
    path = tmp_path / "q.jsonl"
    path.write_text(json.dumps(q) + "\n")
    args = argparse.Namespace(
        questions=str(path), question_types="conceptual", checkout=str(tmp_path), namespace="ns"
    )
    (t,) = tasks_from_questions(args)
    assert t["prompt"] is WORK_PROMPT and t["gold"] == ["src/a.cpp"] and t["text"] == q["query"]
    assert "JSON list" not in WORK_PROMPT


def test_the_real_world_question_sets_are_well_formed():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "bench" / "fixtures"
    for name, kinds in (
        ("code_intel_qa_real.jsonl", {"debug", "change", "understand", "impact", "history"}),
        ("code_work_v2.jsonl", {"explain", "impact", "history"}),
        ("code_work_multihop.jsonl", {"fix", "test", "multihop"}),
    ):
        rows = [
            json.loads(line)
            for line in (root / name).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert rows, name
        assert len({r["id"] for r in rows}) == len(rows), f"{name}: duplicate ids"
        for r in rows:
            assert r["kind"] in kinds, (name, r["id"])
            assert r["query"].strip(), (name, r["id"])
            assert r["source"]["checkout"], (name, r["id"])
            if "reference" in r:
                ref = r["reference"]
                assert 3 <= len(ref["key_points"]) <= 6, (name, r["id"])
                assert ref["answer"].strip(), (name, r["id"])
            else:
                assert r["gold"]["paths"], (name, r["id"])
