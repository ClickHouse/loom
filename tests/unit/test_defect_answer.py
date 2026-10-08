"""A defect-shaped loom_search answers the defect — the draft
is the report without its question, Loom's quoted output is never a hit, and a
tracker verdict narrows the other sections."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.loom_search import answer_first as af
from server.memory.app.loom_search import tracker_leg as tl
from server.tracker.domain import defect_answer as da


@pytest.mark.parametrize(
    ("query", "report"),
    [
        (
            "Logical error: Bad cast from type DB::ColumnNullable to DB::ColumnString — already reported?",
            "Logical error: Bad cast from type DB::ColumnNullable to DB::ColumnString",
        ),
        (
            "Is this already reported? Code: 49. DB::Exception: LOGICAL_ERROR: Part intersects",
            "Code: 49. DB::Exception: LOGICAL_ERROR: Part intersects",
        ),
        (
            "Check the QA record before I file it: std::out_of_range in DB::ColumnLowCardinality::insertFrom",
            "std::out_of_range in DB::ColumnLowCardinality::insertFrom",
        ),
        (
            "Heap-use-after-free in DB::ReadBufferFromS3::nextImpl — already filed?",
            "Heap-use-after-free in DB::ReadBufferFromS3::nextImpl",
        ),
        (
            "Assertion failed in DB::IMergeTreeDataPart::checkConsistency — open issue for this?",
            "Assertion failed in DB::IMergeTreeDataPart::checkConsistency",
        ),
        # a clause that carries anything else stays; so does a real error word
        (
            "Logical error: x — is this a known issue? More text here",
            "Logical error: x — More text here",
        ),
        ("Duplicate column name in DB::Exception", "Duplicate column name in DB::Exception"),
    ],
)
def test_the_draft_is_the_report_without_its_question(query: str, report: str) -> None:
    assert da.strip_intent(query) == report


def test_the_defect_shape_titles_the_report_and_reads_error_names() -> None:
    shape = tl.defect_report_shape(
        "Logical error: Bad cast from type DB::ColumnNullable to DB::ColumnString — already reported?"
    )
    assert (
        shape
        and shape["title"]
        == "Logical error: Bad cast from type DB::ColumnNullable to DB::ColumnString"
    )
    # an ErrorCodes name, CamelCase or UPPER_SNAKE, is an error signal
    assert tl.defect_report_shape("IllegalTypeOfArgument in toStartOfInterval — already reported?")
    assert tl.defect_report_shape("ILLEGAL_TYPE_OF_ARGUMENT in toStartOfInterval — known issue?")
    # a plain CamelCase identifier is not
    assert tl.defect_report_shape("MergeTreeDataPart layout — already known?") is None


def _payload(tracker: bool = True) -> dict[str, Any]:
    p: dict[str, Any] = {
        "memory": {
            "results": [
                {
                    "memory_key": "chunk:a:1",
                    "source": "chunk",
                    "content_excerpt": "It returned:\n> loom-code-shunt: this GitHub search is answered by Loom's tracker record",
                },
                {
                    "memory_key": "m1",
                    "source": "fact",
                    "content_excerpt": "the fix landed in #1234",
                },
                {"memory_key": "m2", "source": "fact", "content_excerpt": "b"},
                {"memory_key": "m3", "source": "fact", "content_excerpt": "c"},
                {"memory_key": "m4", "source": "fact", "content_excerpt": "d"},
            ]
        },
        "knowledge": {
            "results": [
                {
                    "source_uri": "k-low",
                    "similarity": 0.38,
                    "content": "x" * 900,
                    "namespace": "other",
                },
                {
                    "source_uri": "k-high",
                    "similarity": 0.57,
                    "content": "y" * 900,
                    "namespace": "other",
                },
                {"source_uri": "k-high2", "similarity": 0.55, "content": "z", "namespace": "other"},
            ]
        },
        "profile": {"results": []},
        "conversations": {
            "results": [
                {"role": "assistant", "content_excerpt": "Two reproduced, unfiled bugs qualify"},
                {"role": "assistant", "content_excerpt": "# Loom memory primer — project: x"},
            ]
        },
    }
    if tracker:
        p["tracker"] = {"verdict": "none", "results": []}
    return p


def test_a_tracker_verdict_leads_and_the_other_sections_narrow() -> None:
    p = af.answer_first(_payload(), "jdoe")
    assert [h["memory_key"] for h in p["memory"]["results"]] == ["m1", "m2", "m3"]
    kn = p["knowledge"]["results"]
    assert [k["source_uri"] for k in kn] == ["k-low", "k-high", "k-high2"][1:2]
    assert (
        kn[0]["content"].endswith("…") and len(kn[0]["content"]) <= da.VERDICT_KNOWLEDGE_CHARS + 1
    )
    assert p["conversations"]["results"] == []
    w = p["withheld"]
    assert w["reason"] == "tracker_verdict_leads" and w["loom_output"] == 2
    assert w["knowledge"] == 2 and w["conversations"] == 1 and w["memory"] == 1
    assert "tracker verdict answers" in af.withheld_note(p)


def test_without_a_verdict_only_loom_output_goes() -> None:
    p = _payload(tracker=False)
    af.answer_first(p, "jdoe")
    assert [h["memory_key"] for h in p["memory"]["results"]] == ["m1", "m2", "m3", "m4"]
    assert len(p["knowledge"]["results"]) == 3 and len(p["conversations"]["results"]) == 1
    assert p["withheld"] == {"loom_output": 2, "reason": "loom_output"}


def test_explain_keeps_everything() -> None:
    p = af.answer_first(_payload(), "jdoe", explain=True)
    assert len(p["memory"]["results"]) == 5 and "withheld" not in p


def test_loom_output_markers_are_real_plugin_strings() -> None:
    """Each marker is printed by the plugin or the server; a renamed prefix
    would silently stop the filter."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    corpus = "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for base in (
            "integrations/claude-code-plugin/lib",
            "integrations/claude-code-plugin/hooks",
            "server",
        )
        for p in (root / base).rglob("*")
        # the marker regex itself is not evidence a marker is printed
        if p.suffix in (".mjs", ".py") and p.name not in ("defect_answer.py", "loom_output.py")
    )
    for marker in (
        "loom-code-shunt:",
        "Loom test gate",
        "Loom validate gate:",
        "Loom task inbox:",
        "# Loom memory primer",
        "Loom recall for this prompt",
        "tracker record (",
        "has no likely duplicate in the tracker index",
    ):
        assert marker in corpus, marker


@pytest.mark.parametrize(
    "marker",
    [
        "has no likely duplicate in the tracker index",
        "no likely duplicate in the indexed records",
    ],
)
def test_tracker_output_from_the_plugin_and_server_is_withheld(marker: str) -> None:
    payload = {"memory": {"results": [{"content_excerpt": f"tracker record: {marker}"}]}}
    assert da.drop_loom_output(payload) == 1
    assert payload["memory"]["results"] == []


def test_list_mine_over_mcp_names_only_what_differs() -> None:
    from server.transport.lean_results import lean_memberships

    rows = [
        {
            "namespace": "jdoe",
            "role": "admin",
            "status": "active",
            "visibility": "private",
            "kind": "memory",
            "description": "",
            "joined_at": "2026-09-01T10:00:00",
            "is_new": False,
            "inherited": False,
        },
        {
            "namespace": "code-loom",
            "role": "admin",
            "status": "active",
            "visibility": "private",
            "kind": "code",
            "description": "Code index of ClickHouse/loom @ main (read-only reference data)",
            "joined_at": "2026-09-17T15:17:07",
            "is_new": True,
            "inherited": False,
        },
        {
            "namespace": "x",
            "role": "viewer",
            "status": "pending",
            "visibility": "public",
            "kind": "memory",
            "description": "d" * 200,
            "joined_at": None,
            "is_new": False,
            "inherited": True,
        },
    ]
    out = lean_memberships({"rows": rows})["rows"]
    assert out[0] == {
        "namespace": "jdoe",
        "role": "admin",
        "description": "",
        "joined_at": "2026-09-01",
    }
    assert out[1] == {
        "namespace": "code-loom",
        "role": "admin",
        "kind": "code",
        "description": "ClickHouse/loom @ main",
        "joined_at": "2026-09-17",
        "is_new": True,
    }
    assert (
        out[2]["status"] == "pending"
        and out[2]["inherited"] is True
        and out[2]["visibility"] == "public"
    )
    assert len(out[2]["description"]) == 81
