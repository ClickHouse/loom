"""The document-parsing bench's corpus labels and scorer."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from bench.docparse import score as S

CORPUS = Path(__file__).resolve().parent / "docparse" / "corpus"


def _gold() -> list[dict]:
    return [json.loads(line) for line in (CORPUS / "gold.jsonl").read_text().splitlines()]


def _perfect(g: dict) -> dict:
    """The parse a parser that read everything right would write."""
    kinds = {"heading": "heading", "code": "code", "item": "list"}
    blocks = [
        {
            "type": kinds.get(b["kind"], "paragraph"),
            "text": b["text"],
            "page_start": b.get("page", 0),
            "page_end": b.get("page_end", b.get("page", 0)),
            "level": b.get("level", 0),
            "notes": b["kind"] == "note",
        }
        for b in g["blocks"]
    ]
    tables = [
        {"rows": t["rows"], "page_start": t.get("page_start", 0), "page_end": t.get("page_end", 0)}
        for t in g["tables"]
    ]
    return {"complete": True, "blocks": blocks, "tables": tables}


def test_every_gold_file_is_in_the_corpus_and_split() -> None:
    gold = _gold()
    assert len(gold) >= 40
    for g in gold:
        assert (CORPUS / g["file"]).is_file(), g["file"]
        assert g["split"] in ("dev", "heldout")
        assert g["format"] in ("pdf", "docx", "pptx")
        assert g["blocks"] or g["tables"]
    cats = {g["category"] for g in gold}
    for c in cats:  # every category is in both splits
        assert {g["split"] for g in gold if g["category"] == c} == {"dev", "heldout"}, c


def test_a_perfect_parse_scores_full_marks_and_loses_nothing() -> None:
    for g in _gold():
        s = S.score_file(g, _perfect(g))
        assert s["text_recall"] == 1.0 and s["silent_loss"] == 0 and s["lost_blocks"] == 0, g[
            "file"
        ]
        for m in ("order_acc", "heading_f1", "heading_level_acc", "table_cell_f1", "table_det_f1"):
            assert s.get(m, 1.0) == 1.0, (g["file"], m)


def test_a_dropped_block_in_a_complete_parse_is_a_silent_loss() -> None:
    g = next(g for g in _gold() if g["format"] == "pdf" and not g["ocr"])
    p = _perfect(g)
    dropped = next(i for i, b in enumerate(p["blocks"]) if len(b["text"].split()) >= 8)
    del p["blocks"][dropped]
    s = S.score_file(g, p)
    assert s["lost_blocks"] == 1 and s["silent_loss"] == 1
    p["complete"] = False  # reported incomplete: lost, but not silently
    assert S.score_file(g, p)["silent_loss"] == 0


def test_a_block_whose_lines_are_reordered_is_not_lost() -> None:
    lines = [
        "replicas: 4 in every zone",
        "retry: max attempts ten times",
        "backoff: grows by half a second",
    ]
    g = {
        "format": "pdf",
        "blocks": [{"kind": "code", "text": "\n".join(lines), "page": 1}],
        "tables": [],
    }
    block = {"type": "code", "page_start": 1, "page_end": 1, "level": 0, "notes": False}
    p = {
        "complete": True,
        "tables": [],
        "blocks": [
            {**block, "text": f"{lines[1]} {lines[0]}"},
            {**block, "text": lines[2]},
        ],
    }
    assert S.lost_blocks(g, p) == 0


def test_a_heading_matches_without_its_number_and_levels_compare_by_rank() -> None:
    g = {
        "blocks": [
            {"kind": "heading", "level": 2, "text": "1. Scope"},
            {"kind": "heading", "level": 3, "text": "1.1 Limits"},
        ],
        "tables": [],
    }
    p = {
        "blocks": [
            {"type": "heading", "level": 1, "text": "Scope"},
            {"type": "heading", "level": 2, "text": "1.1 Limits"},
        ]
    }
    s = S.heading_scores(g, p)
    assert s["heading_f1"] == 1.0 and s["heading_level_acc"] == 1.0


def test_a_table_split_over_pages_with_its_header_repeated_scores_whole() -> None:
    rows = [["Setting", "Default"], *[[f"k{i}", str(i)] for i in range(6)]]
    g = {"tables": [{"rows": rows}]}
    p = {"tables": [{"rows": rows[:4]}, {"rows": [rows[0], *rows[4:]]}]}
    s = S.table_scores(g, p)
    assert s["table_cell_f1"] == 1.0 and s["table_det_f1"] == 1.0
    p = {"tables": [{"rows": [r[::-1] for r in rows]}]}  # columns swapped: no cell where it belongs
    assert S.table_scores(g, p)["table_cell_f1"] == 0.0


def test_order_follows_a_repeated_text_to_its_next_occurrence() -> None:
    para = "the scheduler retries tenant quotas every hour"
    g = {
        "blocks": [
            {"kind": "paragraph", "text": t}
            for t in (
                para,
                "a second paragraph with five words",
                para,
                "a closing paragraph of six words here",
            )
        ]
    }
    p = {"blocks": [{"type": "paragraph", "text": b["text"]} for b in copy.deepcopy(g["blocks"])]}
    assert S.order_score(g, p) == 1.0
