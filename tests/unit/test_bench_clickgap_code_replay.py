"""The ClickGap replay gate refuses a run it could not fully measure."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bench"))

import clickgap_code_replay as R


def _data(tmp_path: Path, rows: list[dict]) -> str:
    p = tmp_path / "prs.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(p)


def _args(data: str, **kw: object) -> Any:
    import argparse

    ap = argparse.Namespace
    base = {
        "data": data,
        "base_url": "http://bench.invalid",
        "token": "",
        "concurrency": 2,
        "min_pack_hit": None,
        "min_symbol_coverage": 0.7,
        "max_mean_tokens": None,
    }
    base.update(kw)
    return ap(**base)


def _fake(by_pr: dict[int, R.PrReplay | Exception]):
    async def replay(_client, row):
        got = by_pr[int(row["pr_number"])]
        if isinstance(got, Exception):
            raise got
        return got

    return replay


def test_one_errored_pr_refuses_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = R.PrReplay(pr_number=1, pack_hit=True, symbols_total=2, symbols_resolved=2)
    monkeypatch.setattr(R, "replay_pr", _fake({1: good, 2: RuntimeError("503")}))
    data = _data(tmp_path, [{"pr_number": 1}, {"pr_number": 2}])
    assert asyncio.run(R.run(_args(data))) == 1


def test_no_changed_symbols_is_not_full_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = R.PrReplay(pr_number=1, pack_hit=True)
    monkeypatch.setattr(R, "replay_pr", _fake({1: empty}))
    data = _data(tmp_path, [{"pr_number": 1}])
    assert asyncio.run(R.run(_args(data))) == 1
    assert asyncio.run(R.run(_args(data, min_symbol_coverage=None))) == 0


def test_a_clean_run_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = R.PrReplay(pr_number=1, pack_hit=True, symbols_total=4, symbols_resolved=3)
    monkeypatch.setattr(R, "replay_pr", _fake({1: good}))
    assert asyncio.run(R.run(_args(_data(tmp_path, [{"pr_number": 1}])))) == 0
