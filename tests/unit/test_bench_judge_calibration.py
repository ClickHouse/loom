"""bench/judge_calibration.py — probes of known truth, and no calibration file
when nothing was judged."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from bench import judge_calibration as jc

_DATA = [
    {
        "sample_id": "c1",
        "qa": [
            {"question": "Where did she go?", "answer": "Paris", "category": 1},
            {"question": "What did he buy?", "answer": "a red bike", "category": 1},
            {"question": "adversarial", "category": 5},
        ],
    }
]


def test_probes_pair_each_gold_with_a_wrong_answer_from_its_category() -> None:
    probes = jc.build_probes(_DATA, 2, seed=7)
    assert probes == jc.build_probes(_DATA, 2, seed=7)
    assert {p["truth"] for p in probes} == {"CORRECT", "WRONG"}
    for p in probes:
        own = p["gold"] in p["generated"]
        assert own is (p["truth"] == "CORRECT")


def test_a_run_that_judged_nothing_writes_no_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "locomo.json"
    data.write_text(json.dumps(_DATA))
    monkeypatch.setenv("OPENAI_API_KEY", "k")

    async def down(*a: object, **k: object) -> str:
        raise httpx.ConnectError("judge unreachable")

    monkeypatch.setattr(jc, "_chat", down)
    out = tmp_path / "cal.json"
    args = argparse.Namespace(
        dataset=str(data),
        n=2,
        seed=7,
        judge_model="j",
        concurrency=2,
        out=str(out),
        probes_out="",
    )
    assert asyncio.run(jc.run(args)) == 1
    assert not out.exists()
