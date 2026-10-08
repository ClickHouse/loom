"""bench/longmemeval.py — the memory.ask answerer and the k sweep (pure parts
plus the question stage with the HTTP layer faked).

The sweep is the accuracy-versus-tokens curve for naive retrieval over k; the
loom-ask answerer is the lever that A/Bs memory.ask's reading depth under one
synth model.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import pytest

from bench import longmemeval as lme
from bench.longmemeval import ItemResult, _parse_sweep, _write_csv, sweep_summary


def _row(
    k: int,
    *,
    recalled: bool,
    judge: str | None,
    tin: int,
    tout: int,
    answerer: str = "reader",
    deep: bool = False,
    ask_ms: int = 0,
) -> ItemResult:
    return ItemResult(
        question_id=f"q{k}",
        question_type="multi-session",
        question="?",
        gold_answer="x",
        top_k_excerpts=[],
        recalled=recalled,
        judge_label=judge,
        qa_tokens_in=tin,
        qa_tokens_out=tout,
        sweep_k=k,
        answerer=answerer,
        ask_deep_fired=deep,
        ask_latency_ms=ask_ms,
        search_latency_ms=100 + k,
    )


def test_parse_sweep_is_positive_deduplicated_ascending() -> None:
    assert _parse_sweep("10, 3,5,3,0,-2,abc,15") == [3, 5, 10, 15]
    assert _parse_sweep("") == []


def test_sweep_summary_reports_one_row_per_k_with_tokens_per_item() -> None:
    rows = sweep_summary(
        [
            _row(3, recalled=True, judge="CORRECT", tin=1000, tout=100),
            _row(3, recalled=False, judge="WRONG", tin=1200, tout=100),
            _row(
                10,
                recalled=True,
                judge="CORRECT",
                tin=3000,
                tout=100,
                answerer="loom-ask",
                deep=True,
                ask_ms=900,
            ),
            _row(
                10,
                recalled=True,
                judge="CORRECT",
                tin=2800,
                tout=100,
                answerer="loom-ask",
                ask_ms=700,
            ),
        ]
    )
    assert [r["k"] for r in rows] == [3, 10]
    assert rows[0]["recall"] == "1/2 (50.0%)"
    assert rows[0]["judge"] == "1/2 (50.0%)"
    assert rows[0]["qa_tokens_per_item"] == 1200
    assert rows[0]["deep_fired"] == "—"  # reader arm: the deep pass is not in play
    assert rows[1]["judge"] == "2/2 (100.0%)"
    assert rows[1]["qa_tokens_per_item"] == 3000
    assert rows[1]["deep_fired"] == "1/2"
    assert rows[1]["ask_p50_ms"] in (700, 900)


def test_csv_carries_the_sweep_and_ask_columns(tmp_path: Path) -> None:
    out = tmp_path / "r.csv"
    _write_csv(
        out,
        [
            _row(
                5,
                recalled=True,
                judge=None,
                tin=1,
                tout=1,
                answerer="loom-ask",
                deep=True,
                ask_ms=42,
            )
        ],
    )
    with out.open() as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["sweep_k"] == "5"
    assert rows[0]["answerer"] == "loom-ask"
    assert rows[0]["ask_deep_fired"] == "1"
    assert rows[0]["ask_latency_ms"] == "42"


@pytest.fixture
def faked_http(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`_post` answers memory.search and memory.ask from canned payloads and
    records every body; the judge always says CORRECT."""

    seen: dict[str, Any] = {"posts": []}

    async def fake_post(client: Any, url: str, body: dict, token: str = "") -> dict:
        seen["posts"].append((url, body))
        if url.endswith("/v1/memory.search"):
            return {
                "results": [
                    {"memory_key": "k1", "content_excerpt": "the deli was Roscioli"},
                    {"memory_key": "k2", "content_excerpt": "user likes espresso"},
                ]
            }
        if url.endswith("/v1/memory.ask"):
            return {
                "answer": "Roscioli [k1].",
                "depth": body.get("depth", "excerpt"),
                "deep_fired": body.get("depth") == "raw",
                "route_tier": "strong",
            }
        raise AssertionError(url)

    async def fake_judge(*a: Any, **k: Any) -> tuple[str, str]:
        return "CORRECT", ""

    async def fake_reader(*a: Any, **k: Any) -> str:
        seen["reader_called"] = True
        return "Roscioli"

    monkeypatch.setattr(lme, "_post", fake_post)
    monkeypatch.setattr(lme, "_llm_judge", fake_judge)
    monkeypatch.setattr(lme, "_llm_synthesize_answer", fake_reader)
    return seen


_ITEM = {
    "question_id": "q1",
    "question_type": "single-session-assistant",
    "question": "what was the deli near the Vatican called?",
    "answer": "Roscioli",
    "question_date": "2023/05/20 (Sat) 10:00",
    "answer_session_ids": ["s1"],
}
_IDENTITY = {"org": "dev", "namespace": "lme-test", "agent": "lme-bench", "user_id": "-"}


async def _answer(answerer: str, ask_json: dict | None = None, k: int = 5) -> ItemResult:
    return await lme._answer_at_k(
        client=None,
        base_url="http://x",
        item=_ITEM,
        identity=_IDENTITY,
        top_k=k,
        key_to_sessions={"k1": {"s1"}},
        qa_usage={"tin": 0, "tout": 0, "calls": 0},
        _t_qa0=0.0,
        build=None,
        build_seconds=0.0,
        judge_model="gpt-4o-2024-08-06",
        reader_model="gpt-4o-mini",
        judge_api_key="sk-test",
        token="",
        search_mode="rrf",
        search_json=None,
        rerank="none",
        pool_unmatched=0,
        reader="official",
        answerer=answerer,
        ask_json=ask_json,
        sweep_k=k,
    )


async def test_loom_ask_answerer_posts_depth_and_records_the_ask_fields(faked_http) -> None:
    r = await _answer("loom-ask", {"depth": "raw"})
    urls = [u for u, _ in faked_http["posts"]]
    assert urls == ["http://x/v1/memory.search", "http://x/v1/memory.ask"]
    ask_body = faked_http["posts"][1][1]
    assert ask_body["depth"] == "raw"
    assert ask_body["namespace_scope"] == "this"
    assert ask_body["top_k"] == 5
    assert ask_body["query"] == _ITEM["question"]
    assert r.hypothesis == "Roscioli [k1]."
    assert r.judge_label == "CORRECT"
    assert r.answerer == "loom-ask"
    assert r.ask_depth == "raw" and r.ask_deep_fired is True and r.ask_route_tier == "strong"
    assert r.recalled is True  # recall@k still comes from memory.search
    assert r.sweep_k == 5
    assert r.qa_calls == 2  # search + ask
    assert "reader_called" not in faked_http


async def test_reader_answerer_does_not_call_memory_ask(faked_http) -> None:
    r = await _answer("reader")
    urls = [u for u, _ in faked_http["posts"]]
    assert urls == ["http://x/v1/memory.search"]
    assert faked_http.get("reader_called") is True
    assert r.answerer == "reader"
    assert r.ask_depth == "" and r.ask_deep_fired is False


async def test_build_columns_ride_only_the_row_that_carries_build(faked_http) -> None:
    r = await _answer("reader")
    assert r.build_tokens_in == 0 and r.memories_written == 0


# ---- --ask-arms: several memory.ask configurations on one ingest ----------

from bench.longmemeval import arm_label, arm_summary, parse_ask_arms  # noqa: E402


def test_parse_ask_arms_accepts_list_or_object_and_drops_junk() -> None:
    assert parse_ask_arms("") == []
    assert parse_ask_arms('{"depth":"raw"}') == [{"depth": "raw"}]
    assert parse_ask_arms('[{"depth":"raw"}, 3, {"label":"x"}]') == [
        {"depth": "raw"},
        {"label": "x"},
    ]


def test_arm_label_prefers_label_then_fields_then_index() -> None:
    assert arm_label({"label": "raw", "depth": "raw"}, 0) == "raw"
    assert (
        arm_label({"depth": "raw", "max_answer_chars": 600}, 1) == "depth=raw,max_answer_chars=600"
    )
    assert arm_label({}, 2) == "arm2"


def _arm_row(qid: str, arm: str, judge: str, deep: bool = False) -> ItemResult:
    return ItemResult(
        question_id=qid,
        question_type="multi-session",
        question="?",
        gold_answer="x",
        top_k_excerpts=[],
        recalled=True,
        judge_label=judge,
        answerer="loom-ask",
        arm=arm,
        ask_deep_fired=deep,
        ask_latency_ms=500,
        qa_tokens_in=100,
        qa_tokens_out=10,
    )


def test_arm_summary_pairs_items_and_reports_the_bootstrap_delta() -> None:
    rows = []
    for i in range(12):
        rows.append(_arm_row(f"q{i}", "excerpt", "CORRECT" if i < 6 else "WRONG"))
        rows.append(_arm_row(f"q{i}", "raw", "CORRECT" if i < 10 else "WRONG", deep=True))
    summary = arm_summary(rows, seed=1)
    assert [r["arm"] for r in summary] == ["excerpt", "raw"]
    assert summary[0]["judge"] == "6/12 (50.0%)" and summary[0]["delta"] == ""
    assert summary[1]["judge"] == "10/12 (83.3%)"
    assert summary[1]["deep_fired"] == "12/12"
    assert summary[1]["delta"].startswith("+33.3pp [lb ")
    assert "W4/T8/L0" in summary[1]["delta"]


async def test_arms_run_on_one_ingest_and_label_rows(faked_http, monkeypatch) -> None:
    """Two arms → two rows per k with the arm label, one search each."""
    rows = []
    for k in [5]:
        for label, aj in [("excerpt", {"depth": "excerpt"}), ("raw", {"depth": "raw"})]:
            r = await lme._answer_at_k(
                client=None,
                base_url="http://x",
                item=_ITEM,
                identity=_IDENTITY,
                top_k=k,
                key_to_sessions={"k1": {"s1"}},
                qa_usage={"tin": 0, "tout": 0, "calls": 0},
                _t_qa0=0.0,
                build=None,
                build_seconds=0.0,
                judge_model="gpt-4o-2024-08-06",
                reader_model="gpt-4o-mini",
                judge_api_key="sk-test",
                token="",
                search_mode="rrf",
                search_json=None,
                rerank="none",
                pool_unmatched=0,
                reader="official",
                answerer="loom-ask",
                ask_json=aj,
                sweep_k=0,
                arm=label,
            )
            rows.append(r)
    assert [r.arm for r in rows] == ["excerpt", "raw"]
    assert [r.ask_depth for r in rows] == ["excerpt", "raw"]
    asks = [b for u, b in faked_http["posts"] if u.endswith("/v1/memory.ask")]
    assert [b["depth"] for b in asks] == ["excerpt", "raw"]
