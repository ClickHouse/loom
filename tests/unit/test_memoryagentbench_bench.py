"""MemoryAgentBench adapter: the pure parts.

The harness needs a live server and a reader; these pin the protocol
constants, the chunker, the per-competency scoring, the split/task mapping,
the loader, the reader prompts and the summary shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bench.memoryagentbench import (  # noqa: E402
    CHUNK_TOKENS,
    SPLITS,
    chunk_text,
    f1,
    label_accuracy,
    load_rows,
    normalize_answer,
    pairs_of,
    reader_prompt,
    recall_at_k,
    score,
    sub_em,
    summarize,
    task_of,
)


def test_protocol_constants_match_the_paper() -> None:
    assert set(SPLITS.values()) == {"AR", "TTL", "LRU", "CR"}
    assert CHUNK_TOKENS == {"AR": 512, "CR": 512, "TTL": 4096, "LRU": 4096}


def test_chunker_is_sentence_aware_and_bounded() -> None:
    text = " ".join(f"Sentence number {i} is here." for i in range(200))
    chunks = chunk_text(text, 64)
    assert len(chunks) > 5 and "".join(chunks).replace(" ", "") == text.replace(" ", "")
    assert all(c.endswith(".") for c in chunks)
    assert chunk_text("", 64) == []


def test_scoring_per_competency() -> None:
    assert normalize_answer("The Answer, is: Paris!") == "answer is paris"
    assert sub_em("It was Paris, France.", ["paris"]) == 1.0 and sub_em("Rome", ["paris"]) == 0.0
    assert f1("Miquette Giraudy", ["Miquette Giraudy"]) == 1.0 and f1("x", ["y"]) == 0.0
    assert label_accuracy("card_arrival", "card_arrival") == 1.0
    assert recall_at_k("1. Heat\n2. Casino\n3. Se7en", ["Casino", "Heat", "Alien"], k=5) == 2 / 3
    assert recall_at_k("nothing", [], 5) == 0.0
    assert score("CR", "qa", "Donald Trump", ["Donald Trump"]) == {"sub_em": 1.0, "f1": 1.0}
    assert score("TTL", "icl", "banking", "banking") == {"accuracy": 1.0}
    assert "recall@5" in score("TTL", "recsys", "1. Heat", ["Heat"])
    assert "token_f1_proxy" in score("LRU", "summary", "a b c", ["a b d"])
    assert set(score("LRU", "qa", "x", ["x"])) == {"sub_em", "f1"}


def test_task_mapping_and_loader(tmp_path: Path) -> None:
    assert task_of({"metadata": {"source": "infbench_sum_eng_shots2"}}) == "summary"
    assert task_of({"metadata": {"source": "icl_banking77_5900shot_balance"}}) == "icl"
    assert task_of({"metadata": {"source": "recsys_redial"}}) == "recsys"
    assert task_of({"metadata": {"source": "eventQA_full"}}) == "eventqa"
    assert task_of({"metadata": {"source": "factconsolidation_mh"}}) == "qa"
    rows = [
        {
            "id": "a",
            "split": "Conflict_Resolution",
            "context": "1. Fact one. 2. Fact two.",
            "questions": ["q1", "q2"],
            "answers": ["x", "y"],
            "metadata": {"source": "factconsolidation_sh"},
        },
        {
            "id": "b",
            "split": "Long_Range_Understanding",
            "context": "book",
            "questions": ["q"],
            "answers": ["z"],
            "metadata": {"source": "infbench_sum"},
        },
    ]
    p = tmp_path / "mab.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    got = load_rows(p, ["Conflict_Resolution"], 0)
    assert (
        [r["id"] for r in got] == ["a"] and got[0]["competency"] == "CR" and got[0]["task"] == "qa"
    )
    assert pairs_of(got[0]) == [("q1", "x"), ("q2", "y")]
    both = load_rows(p, [], 0)
    assert [r["competency"] for r in both] == ["CR", "LRU"] and both[1]["task"] == "summary"
    assert len(load_rows(p, [], 1)) == 1
    assert [r["id"] for r in load_rows(p, [], 0, source_regex="infbench")] == ["b"]
    assert load_rows(p, [], 0, source_regex="nothing-matches") == []


def test_reader_prompt_picks_the_competency_template() -> None:
    cr = reader_prompt("CR", "qa", "who is president?", ["1. A is president", "2. B is president"])
    assert "newer fact has the larger serial number" in cr and "[2] 2. B is president" in cr
    icl = reader_prompt("TTL", "icl", "I lost my card", ["text -> label"])
    assert "output only the label" in icl
    assert "20 recommendations" in reader_prompt("TTL", "recsys", "hi", [])
    assert "Question: q" in reader_prompt("AR", "eventqa", "q", [])


def test_summary_groups_by_competency_with_cost_columns() -> None:
    rows = [
        {
            "competency": "CR",
            "context_id": "c1",
            "sub_em": 1.0,
            "f1": 1.0,
            "build_ms": 900,
            "query_ms": 40,
            "chunks": 12,
        },
        {
            "competency": "CR",
            "context_id": "c1",
            "sub_em": 0.0,
            "f1": 0.5,
            "build_ms": 900,
            "query_ms": 60,
            "chunks": 12,
        },
        {
            "competency": "TTL",
            "context_id": "t1",
            "accuracy": 1.0,
            "build_ms": 3000,
            "query_ms": 80,
            "chunks": 30,
        },
    ]
    s = summarize(rows)
    assert s["n_questions"] == 3
    assert s["CR"] == {
        "n": 2,
        "sub_em": 0.5,
        "f1": 0.75,
        "build_ms_per_context_p50": 900,
        "query_ms_p50": 60,
        "chunks_per_context_mean": 12.0,
    }
    assert s["TTL"]["accuracy"] == 1.0 and "sub_em" not in s["TTL"]


def test_namespace_json_registers_the_fresh_namespace_before_the_first_write() -> None:
    """`--namespace-json`: the fresh per-context
    namespace is registered with the caller's settings before any chunk is
    memorized, so a write-time policy such as newest_wins applies from the
    first row."""
    import asyncio

    from bench import memoryagentbench as mab

    calls: list[tuple[str, dict]] = []

    async def fake_post(client, url, body, token=""):
        calls.append((url.rsplit("/", 1)[-1], body))
        if url.endswith("memory.ask"):
            return {"answer": "x", "results": []}
        return {}

    row = {
        "competency": "CR",
        "task": "qa",
        "context": "Fact one. Fact two.",
        "questions": ["q?"],
        "answers": [["x"]],
        "split": "Conflict_Resolution",
        "id": "c1",
    }
    orig = mab._post
    mab._post = fake_post
    try:
        asyncio.run(
            mab.run_context(
                None,
                row,
                base_url="http://s",
                token="t",
                org="o",
                memorize="extract",
                answer="ask",
                top_k=5,
                reader_model="",
                api_key="",
                ask_json={},
                max_questions=0,
                chunk_override=0,
                namespace_json={"default_conflict_policy": "newest_wins"},
            )
        )
    finally:
        mab._post = orig
    assert calls[0][0] == "namespace.register"
    assert calls[0][1]["default_conflict_policy"] == "newest_wins"
    assert calls[0][1]["namespace"].startswith("mab-cr-") and calls[0][1]["org"] == "o"
    assert (
        calls[1][0] == "memory.set_from_messages"
        and calls[1][1]["namespace"] == calls[0][1]["namespace"]
    )
