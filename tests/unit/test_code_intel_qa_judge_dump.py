"""--judge-dump writes one JSON line per judged question and run."""

import json
from pathlib import Path

from bench.code_intel_qa import dump_verdicts


def test_dump_appends_one_line_per_verdict(tmp_path: Path) -> None:
    p = tmp_path / "j" / "v.jsonl"
    dump_verdicts(
        p,
        [
            {
                "qid": "a",
                "verdict": "accept",
                "context_tokens": 3,
                "answer": "A [x:1-2]",
                "citations": "x:1",
            }
        ],
        run=1,
    )
    dump_verdicts(p, [{"qid": "a", "verdict": "reject", "reason": "x"}], run=2)
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    assert [(r["qid"], r["run"], r["verdict"]) for r in rows] == [
        ("a", 1, "accept"),
        ("a", 2, "reject"),
    ]
    assert rows[1]["reason"] == "x" and rows[0]["context_tokens"] == 3
    assert (
        rows[0]["answer"] == "A [x:1-2]"
        and rows[0]["citations"] == "x:1"
        and rows[1]["answer"] == ""
    )
