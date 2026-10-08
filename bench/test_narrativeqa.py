"""Pure-function tests for the NarrativeQA harness — no server, no LLM spend."""

from __future__ import annotations

from bench.narrativeqa import (
    best_f1,
    best_rouge_l,
    group_rows,
    namespace_for,
    parse_grade,
    strip_html,
    summarize,
    title_in_text,
)


def _row(doc_id: str, q: str, answers: list[str], title: str = "T") -> dict:
    return {
        "row_idx": 0,
        "row": {
            "document": {
                "id": doc_id,
                "kind": "movie",
                "word_count": 3,
                "text": "a b c",
                "summary": {"title": title, "text": "s"},
            },
            "question": {"text": q, "tokens": []},
            "answers": [{"text": a, "tokens": []} for a in answers],
        },
        "truncated_cells": [],
    }


def test_group_rows_keeps_document_order_and_all_questions() -> None:
    rows = [
        _row("d1", "Who?", ["Mark", "A student"]),
        _row("d1", "Where?", ["Phoenix"]),
        _row("d2", "When?", ["1990"], title="Other"),
        {"row": {"document": {}, "question": {"text": "x"}, "answers": []}},  # no id: dropped
        _row("d1", "", ["empty question dropped"]),
    ]
    docs = group_rows(rows)
    assert list(docs) == ["d1", "d2"]
    assert [q["q"] for q in docs["d1"]["questions"]] == ["Who?", "Where?"]
    assert docs["d1"]["questions"][0]["answers"] == ["Mark", "A student"]
    assert docs["d2"]["title"] == "Other" and docs["d2"]["text"] == "a b c"


def test_scores_take_the_better_reference() -> None:
    assert (
        best_f1(
            "a high school student in Phoenix",
            ["He is a high school student in Phoenix.", "A loner"],
        )
        > 0.8
    )
    assert best_f1("nothing alike", ["He is a student.", "A loner"]) == 0.0
    assert best_rouge_l("Mark Hunter", ["Mark Hunter", "the DJ"]) == 1.0
    # F1, not recall: a verbose answer that contains the gold is not a perfect score
    assert best_rouge_l("the pirate radio DJ Mark Hunter", ["Mark Hunter"]) < 1.0
    assert best_f1("x", []) == 0.0


def test_parse_grade() -> None:
    assert parse_grade("CORRECT") is True
    assert parse_grade(" wrong.") is False
    assert parse_grade("maybe") is None


def test_namespace_is_deterministic_per_salt() -> None:
    a = namespace_for("abcdef1234567890", "s1")
    assert a == namespace_for("abcdef1234567890", "s1") and a.startswith("bench-nqa-abcdef12-")
    assert a != namespace_for("abcdef1234567890", "s2")


def test_summarize_means_and_judged_only_when_present() -> None:
    rows = [
        {
            "doc_id": "d1",
            "title": "T",
            "f1": 1.0,
            "rouge_l": 1.0,
            "search_ms": 100,
            "reader_tokens_in": 1000,
            "judged": True,
        },
        {
            "doc_id": "d1",
            "title": "T",
            "f1": 0.0,
            "rouge_l": 0.5,
            "search_ms": 300,
            "reader_tokens_in": 3000,
            "judged": False,
            "abstained": True,
        },
        {
            "doc_id": "d2",
            "title": "U",
            "f1": 0.5,
            "rouge_l": 0.5,
            "search_ms": 200,
            "reader_tokens_in": 2000,
        },
        {"doc_id": "d2", "title": "U", "error": "boom", "f1": 0.0, "rouge_l": 0.0},
    ]
    s = summarize(rows)
    assert s["questions"] == 4 and s["errors"] == 1 and s["documents"] == 2
    assert s["f1"] == 50.0 and s["rouge_l"] == 66.67
    assert (
        s["abstain_rate"] == 33.33
        and s["search_p50_ms"] == 200
        and s["reader_tokens_per_q"] == 2000
    )
    assert s["judged_n"] == 2 and s["judged_acc"] == 50.0
    assert (
        s["per_document"]["d1"]["judged_acc"] == 50.0
        and "judged_acc" not in s["per_document"]["d2"]
    )
    assert "judged_acc" not in summarize([rows[2]])


def test_strip_html_keeps_script_lines_and_folds_blanks() -> None:
    raw = (
        "<html><head><title>Up in the Air Script at IMSDb.</title><script>x=1</script></head>"
        "<body><pre><b>          RYAN</b>\n<b>                         \n</b>"
        "          Ryan lets the weight sink in.\n\n\n\n"
        "<TT><FONT SIZE=-1>fix &amp; go</FONT></TT></pre></body></html>"
    )
    out = strip_html(raw)
    assert "<" not in out and "x=1" not in out
    assert "RYAN" in out and "Ryan lets the weight sink in." in out and "fix & go" in out
    assert "\n\n\n" not in out
    assert strip_html("plain text") == "plain text"


def test_title_in_text_flags_a_wrong_source() -> None:
    assert title_in_text("The Rescuers Down Under", "...Rescuers Down Under, The Script...")
    assert title_in_text("Up in the Air (1940 film)", "Up in the Air Script at IMSDb")
    assert not title_in_text(
        "Mrs. Frisby and the Rats of NIMH", "The Project Gutenberg EBook of House Rats and Mice"
    )


def test_suspect_documents_are_flagged_and_excluded_from_clean_figures() -> None:
    good = [
        {"doc_id": "g", "title": "G", "f1": 0.6, "rouge_l": 0.6, "judged": True} for _ in range(4)
    ]
    wrong_text = [
        {
            "doc_id": "w",
            "title": "W",
            "title_match": False,
            "f1": 0.0,
            "rouge_l": 0.0,
            "judged": False,
        }
        for _ in range(4)
    ]
    all_abstain = [
        {"doc_id": "a", "title": "A", "f1": 0.0, "rouge_l": 0.0, "judged": False, "abstained": True}
        for _ in range(4)
    ]
    s = summarize(good + wrong_text + all_abstain)
    assert s["per_document"]["w"]["suspect_source"] and s["per_document"]["a"]["suspect_source"]
    assert not s["per_document"]["g"]["suspect_source"]
    assert s["per_document"]["a"]["abstain_rate"] == 100.0
    assert s["clean_documents"] == 1 and s["suspect_documents"] == 2
    assert s["f1"] == 20.0 and s["f1_clean"] == 60.0
    assert s["judged_acc"] == 33.33 and s["judged_acc_clean"] == 100.0
