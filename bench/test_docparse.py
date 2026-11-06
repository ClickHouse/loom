"""The document-parsing bench's corpus labels and scorer."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from bench.docparse import prepare as P
from bench.docparse import run as R
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
        for m in (
            "order_acc",
            "heading_f1",
            "heading_level_acc",
            "table_cell_f1",
            "table_det_f1",
            "locator_acc",
            "notes_acc",
        ):
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


def test_every_gold_page_span_is_ordered_and_every_pdf_item_has_its_page() -> None:
    for g in _gold():
        for b in g["blocks"]:
            assert b.get("page_end", b.get("page", 0)) >= b.get("page", 0), (
                g["file"],
                b["text"][:40],
            )
            if (g["format"] == "pdf" and not g["ocr"]) or g["format"] == "pptx":
                assert b.get("page"), (g["file"], b["text"][:40])
        for t in g["tables"]:
            assert "_at" not in t
            if g["format"] != "docx":
                assert 1 <= t["page_start"] <= t["page_end"] <= g.get("pages", t["page_end"]), g[
                    "file"
                ]


def test_a_block_over_every_page_cites_nothing_and_invented_tables_score_zero() -> None:
    g = next(g for g in _gold() if g["file"] == "web_longtable_0.pdf")
    p = _perfect(g)
    assert S.locator_scores(g, p)["locator_acc"] == 1.0
    wide = {**p, "blocks": [{**b, "page_start": 1, "page_end": g["pages"]} for b in p["blocks"]]}
    assert S.locator_scores(g, wide)["locator_acc"] < 0.1
    prose = next(g for g in _gold() if not g["tables"] and g["format"] == "pdf" and not g["ocr"])
    invented = {
        **_perfect(prose),
        "tables": [{"rows": [["a", "b"], ["1", "2"]], "page_start": 1, "page_end": 1}],
    }
    assert S.score_file(prose, invented)["table_det_f1"] == 0.0


def test_a_failed_parse_scores_zero_on_every_metric_its_gold_defines(tmp_path: Path) -> None:
    g = next(g for g in _gold() if g["tables"] and g["format"] == "pdf" and not g["ocr"])
    s = S.score_file(g, {"error": "boom", "wall_seconds": 2.0})
    assert (
        s["error"] == 1 and s["silent_loss"] == 0 and s["seconds"] is None and s["peak_mb"] is None
    )
    for m in (
        "text_recall",
        "order_acc",
        "heading_f1",
        "table_cell_f1",
        "table_det_f1",
        "locator_acc",
    ):
        assert s[m] == 0.0, m
    assert s["lost_blocks"] > 0
    good = tmp_path / "good.jsonl"
    bad = tmp_path / "bad.jsonl"
    good.write_text("".join(json.dumps({"file": x["file"], **_perfect(x)}) + "\n" for x in _gold()))
    # one file failed, and every other one left out: each scored as a failure
    bad.write_text(json.dumps({"file": g["file"], "error": "boom"}) + "\n")
    report = S.score(CORPUS, [("good", good), ("bad", bad)])
    assert report["ALL"]["n"] == len(_gold())
    assert report["ALL"]["error"] == {"good": 0, "bad": len(_gold())}
    assert report["ALL"]["locator_acc"] == {"good": 1.0, "bad": 0.0}


def test_a_parse_past_its_time_is_an_error_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def slow(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="x", timeout=R.TIMEOUT_S)

    monkeypatch.setattr(subprocess, "run", slow)
    out = tmp_path / "p.jsonl"
    R.run(CORPUS, out, tmp_path, only={"web_doc_0.pdf", "deck_0.pptx"})
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["file"] for line in lines] == ["web_doc_0.pdf", "deck_0.pptx"]
    assert all(line["error"].startswith("timeout") for line in lines)


def test_a_heading_is_cited_whether_the_parse_keeps_its_number_or_not() -> None:
    g = {
        "format": "pdf",
        "tables": [],
        "blocks": [
            {"kind": "heading", "level": 2, "text": "2. Goals", "page": 3},
            {"kind": "paragraph", "text": "the non goals of the plan stay out of it", "page": 3},
        ],
    }
    block = {"page_start": 3, "page_end": 3, "level": 0, "notes": False}
    p = {
        "blocks": [
            {**block, "type": "heading", "text": "Goals"},
            {**block, "type": "paragraph", "text": g["blocks"][1]["text"]},
        ]
    }
    assert S.locator_scores(g, p)["locator_acc"] == 1.0


@pytest.fixture
def pep_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for n, _ in P.PEPS:
        (src / f"pep{n}.html").write_text("")
    return src


def _no_container(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    def fail(*cmd: str) -> None:
        raise error

    monkeypatch.setattr(P, "docker", fail)


def test_a_build_the_container_cannot_see_leaves_its_directory_as_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pep_src: Path
) -> None:
    _no_container(
        monkeypatch, subprocess.CalledProcessError(1, ["docker"], stderr=b"no such container")
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "gold.jsonl").write_text("{}\n")
    (corpus / "keep.pdf").write_bytes(b"%PDF")
    stopped = tmp_path / "stopped"  # a build stopped midway: rendered files and _work, no gold yet
    (stopped / "_work").mkdir(parents=True)
    (stopped / "web_doc_0.pdf").write_bytes(b"%PDF")
    for out in (corpus, tmp_path / "empty", stopped):
        before = sorted(p.name for p in out.iterdir()) if out.exists() else []
        for _ in range(2):  # refused twice: nothing the first refusal left stops the second
            with pytest.raises(SystemExit, match="no such container"):
                P.prepare(out, pep_src)
        assert (sorted(p.name for p in out.iterdir()) if out.exists() else []) == before


def test_a_build_without_docker_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pep_src: Path
) -> None:
    _no_container(monkeypatch, FileNotFoundError("docker"))
    with pytest.raises(SystemExit, match="no docker program"):
        P.prepare(tmp_path / "corpus", pep_src)


def test_a_build_refuses_before_making_or_removing_anything(tmp_path: Path, pep_src: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "gold.jsonl").write_text("{}\n")
    with pytest.raises(SystemExit, match="lacks pep518.html"):
        P.prepare(corpus, tmp_path / "nowhere")
    with pytest.raises(SystemExit, match="is not a directory"):  # a corpus's parent is not made
        P.prepare(tmp_path / "missing" / "corpus", pep_src)
    other = tmp_path / "other"
    other.mkdir()
    (other / "notes.txt").write_text("x")
    with pytest.raises(SystemExit, match="neither gold.jsonl"):
        P.prepare(other, pep_src)
    assert not (tmp_path / "missing").exists() and [p.name for p in other.iterdir()] == [
        "notes.txt"
    ]
    assert sorted(p.name for p in corpus.iterdir()) == ["gold.jsonl"]


def test_a_build_the_container_sees_empties_its_directory_markers_last(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pep_src: Path
) -> None:
    monkeypatch.setattr(P, "docker", lambda *cmd: None)
    removed: list[str] = []
    real = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        removed.append(self.name)
        real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    stopped = tmp_path / "stopped"
    (stopped / "_work").mkdir(parents=True)
    (stopped / "_work" / "scan_old-1.png").write_bytes(b"png")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in ("a.pdf", "gold.jsonl", "z.pdf"):
        (corpus / name).write_bytes(b"x")
    for out in (corpus, stopped):
        d = P.prepare(out, pep_src)
        assert [p.name for p in out.iterdir()] == ["_work"] and list(d.work.iterdir()) == []
    order = [n for n in removed if not n.startswith("mount-")]
    assert order.index("gold.jsonl") > max(order.index("a.pdf"), order.index("z.pdf"))
