"""bench/beam.py — the pure halves of the BEAM adapter: chat → sessions,
question loading, BEAM's event-ordering metric (Kendall τ_b × F1), rubric
aggregation, judge-JSON tolerance, evidence recall. No network."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench import beam


def _chat() -> list[dict]:
    return [
        {
            "batch_number": 1,
            "time_anchor": "March-15-2024",
            "turns": [
                [
                    {
                        "role": "user",
                        "id": 0,
                        "time_anchor": "March-15-2024",
                        "question_type": "main_question",
                        "content": "Plan my budget tracker ->-> 1,1",
                    },
                    {"role": "assistant", "id": 1, "content": "Sure, here is a plan."},
                    {
                        "role": "user",
                        "id": 2,
                        "question_type": "answer_ai_question",
                        "content": "Milestones: Nov 1 setup. ->-> 1,1",
                    },
                    {"role": "assistant", "id": 3, "content": "Noted."},
                ],
                [
                    {"role": "user", "id": 4, "content": "   "},
                    {"role": "assistant", "id": 5, "content": ""},
                ],
                [
                    {
                        "role": "user",
                        "id": 6,
                        "question_type": "main_question",
                        "content": "Average API time is 400ms ->-> 1,3",
                    },
                    {"role": "assistant", "id": 7, "content": "Got it."},
                ],
            ],
        },
        {
            "batch_number": 2,
            "time_anchor": "April-05-2024",
            "turns": [
                [
                    {"role": "user", "id": 8, "content": "Now it's 250ms after caching ->-> 2,1"},
                    {"role": "assistant", "id": 9, "content": "Great improvement."},
                ]
            ],
        },
    ]


def test_sessions_group_turns_date_them_and_strip_markers() -> None:
    sessions = beam.sessions_of(_chat())
    assert [s.sid for s in sessions] == ["0", "6", "8"]  # the empty group is dropped
    assert sessions[0].date == "2024-03-15" and sessions[2].date == "2024-04-05"
    assert sessions[0].turn_ids == [0, 1, 2, 3]
    assert sessions[0].messages[0]["content"] == "Plan my budget tracker"
    assert "->->" not in sessions[0].messages[2]["content"]
    assert [m["role"] for m in sessions[0].messages] == ["user", "assistant", "user", "assistant"]


@pytest.mark.parametrize(
    "raw,iso",
    [
        ("March-15-2024", "2024-03-15"),
        ("Apr-05-2024", "2024-04-05"),
        ("2024-06-01", "2024-06-01"),
        ("someday", ""),
        ("", ""),
    ],
)
def test_parse_anchor(raw: str, iso: str) -> None:
    assert beam.parse_anchor(raw) == iso


def test_questions_of_reads_every_ability_shape() -> None:
    pq = {
        "knowledge_update": [
            {
                "question": "API time?",
                "answer": "250ms",
                "rubric": ["LLM response should state: 250ms"],
                "source_chat_ids": {"original_info": [6], "updated_info": [8]},
                "difficulty": "easy",
            }
        ],
        "abstention": [
            {
                "question": "Did feedback change the UI?",
                "ideal_response": "No information.",
                "rubric": ["says there is no information"],
                "source_chat_ids": [1, "2", "x"],
            }
        ],
        "summarization": [
            {"question": "Summarize", "ideal_summary": "…", "rubric": []}
        ],  # no rubric → skipped
        "event_ordering": [],
    }
    qs = beam.questions_of("100K/1", pq, beam.ABILITIES)
    assert [(q.ability, q.idx) for q in qs] == [("abstention", 0), ("knowledge_update", 0)]
    ku = next(q for q in qs if q.ability == "knowledge_update")
    assert ku.reference == "250ms" and ku.source_turn_ids == [6, 8] and ku.difficulty == "easy"
    ab = next(q for q in qs if q.ability == "abstention")
    assert ab.reference == "No information." and ab.source_turn_ids == [1, 2]


def test_load_chats_from_directory(tmp_path: Path) -> None:
    import json

    d = tmp_path / "100K" / "3"
    (d / "probing_questions").mkdir(parents=True)
    (d / "chat.json").write_text(json.dumps(_chat()))
    (d / "probing_questions" / "probing_questions.json").write_text(
        json.dumps({"knowledge_update": [{"question": "q", "answer": "a", "rubric": ["r"]}]})
    )
    (tmp_path / "100K" / "10").mkdir()  # no chat.json → ignored
    chats = beam.load_chats(tmp_path, "100K", 0, beam.ABILITIES)
    assert len(chats) == 1 and chats[0].name == "100K/3"
    assert len(chats[0].sessions) == 3 and len(chats[0].questions) == 1
    assert chats[0].chars > 0
    assert beam.load_chats(tmp_path, "1M", 0, beam.ABILITIES) == []


# ---- metrics --------------------------------------------------------------


def test_kendall_tau_b_matches_known_values() -> None:
    assert beam.kendall_tau_b([1, 2, 3, 4], [1, 2, 3, 4]) == pytest.approx(1.0)
    assert beam.kendall_tau_b([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    # scipy.stats.kendalltau([1,2,3,4],[1,3,2,4], variant="b") == 0.6667
    assert beam.kendall_tau_b([1, 2, 3, 4], [1, 3, 2, 4]) == pytest.approx(2 / 3)
    # ties: scipy.stats.kendalltau([1,2,3,4],[1,2,4,4], variant="b") == 0.9129
    assert beam.kendall_tau_b([1, 2, 3, 4], [1, 2, 4, 4]) == pytest.approx(0.9129, abs=1e-3)
    assert beam.kendall_tau_b([1, 1, 1], [1, 2, 3]) == 0.0
    assert beam.kendall_tau_b([1], [1]) == 0.0


def test_event_ordering_score_perfect_partial_and_reversed() -> None:
    perfect = beam.event_ordering_score(3, [1, 2, 3], 0)
    assert perfect["f1"] == 1.0 and perfect["tau_norm"] == 1.0 and perfect["final_score"] == 1.0
    reversed_ = beam.event_ordering_score(3, [3, 2, 1], 0)
    assert (
        reversed_["f1"] == 1.0 and reversed_["tau_norm"] == 0.0 and reversed_["final_score"] == 0.0
    )
    # Two of three mentioned in order, one extra event: recall 2/3, precision 2/3.
    partial = beam.event_ordering_score(3, [1, 3], 1)
    assert partial["recall"] == pytest.approx(2 / 3)
    assert partial["precision"] == pytest.approx(2 / 3)
    # BEAM ranks the unmentioned item at tie_rank (after everything), so the
    # missing middle item costs one discordant pair: τ_b = 1/3, normalised 2/3.
    assert partial["tau_norm"] == pytest.approx(2 / 3)
    assert partial["final_score"] == pytest.approx(4 / 9)
    # Out-of-range and repeated references are ignored.
    assert beam.event_ordering_score(3, [9, 1, 1, 2, 3], 0)["final_score"] == 1.0
    nothing = beam.event_ordering_score(3, [], 2)
    assert nothing["final_score"] == 0.0 and nothing["f1"] == 0.0


def test_rubric_score_clamps_and_counts_unparseable_against() -> None:
    assert beam.rubric_score([{"score": 1.0}, {"score": 0.5}]) == pytest.approx(0.75)
    assert beam.rubric_score([{"score": "1"}, None]) == pytest.approx(0.5)
    assert beam.rubric_score([{"score": 0.7}]) == 0.5  # snapped to BEAM's scale
    assert beam.rubric_score([{"score": 7}]) == 1.0
    assert beam.rubric_score([]) == 0.0


@pytest.mark.parametrize(
    "text,expected",
    [
        ('{"score": 1.0, "reason": "ok"}', {"score": 1.0, "reason": "ok"}),
        ('```json\n{"score": 0.5}\n```', {"score": 0.5}),
        ("no json here", None),
        ("[1, 2]", None),
    ],
)
def test_parse_json_object_tolerant(text: str, expected: dict | None) -> None:
    assert beam.parse_json_object(text) == expected


def test_evidence_recall_maps_keys_to_turns() -> None:
    k2t = {"m1": {0, 1}, "m2": {6, 7}, "m3": {8}}
    assert beam.evidence_recall([6, 8], ["m1", "m2"], k2t) == pytest.approx(0.5)
    assert beam.evidence_recall([6, 8], ["m2", "m3"], k2t) == 1.0
    assert beam.evidence_recall([], ["m1"], k2t) == -1.0  # not measured


def test_render_memories_dates_and_orders() -> None:
    text = beam.render_memories(
        [
            {"content_excerpt": "later", "valid_at": "2024-04-05 00:00:00"},
            {"content_excerpt": "undated", "valid_at": "1970-01-01 00:00:00"},
            {"content_excerpt": "earlier", "temporal_anchor": "2024-03-15"},
        ]
    )
    lines = text.splitlines()
    assert lines[0].startswith("- [2024-03-15] earlier")
    assert lines[1].startswith("- [2024-04-05] later")
    assert lines[2].startswith("- [undated] undated")
    assert beam.render_memories([]) == "(nothing retrieved)"


def test_summarize_groups_by_ability() -> None:
    rs = [
        beam.QResult("c", "knowledge_update", 0, "", "q", "a", 1.0, 1, None, 1.0, 15),
        beam.QResult("c", "knowledge_update", 1, "", "q", "a", 0.5, 2, None, -1.0, 15),
        beam.QResult(
            "c",
            "event_ordering",
            0,
            "",
            "q",
            "a",
            0.5,
            3,
            {"precision": 1, "recall": 1, "f1": 1.0, "tau_norm": 0.5, "final_score": 0.5},
            0.0,
            15,
        ),
    ]
    s = beam.summarize(rs)
    assert s["questions"] == 3 and s["rubric_mean"] == pytest.approx(0.667)
    ku = s["abilities"]["knowledge_update"]
    assert ku == {"n": 2, "rubric_mean": 0.75, "full_marks": 1, "evidence_recall": 1.0}
    eo = s["abilities"]["event_ordering"]
    assert eo["event_ordering_final"] == 0.5 and eo["event_ordering_tau"] == 0.5


@pytest.mark.asyncio
async def test_run_chat_registers_reads_without_crediting_and_erases_after_a_failure() -> None:
    """A live chat against a fake server: the namespace is registered as bench,
    the search carries the evaluation-read field, and a judge failure mid-run
    still erases the namespace."""
    import argparse
    import asyncio
    import json

    import httpx

    from bench.bench_namespaces import erase_on_exit

    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        seen.append((request.url.path, body))
        if request.url.path == "/v1/memory.search":
            return httpx.Response(200, json={"results": []})
        if "chat/completions" in str(request.url):
            return httpx.Response(500, json={"error": "judge down"})
        return httpx.Response(200, json={})

    a = argparse.Namespace(
        org="dev",
        base_url="http://loom.test",
        token="t",
        top_k=5,
        search_mode="rrf",
        rerank="none",
        answerer="reader",
        ask_json="",
        reader_model="r",
        judge_model="j",
        keep_namespaces=False,
        credit=False,
    )
    chat = beam.Chat(
        name="c1",
        scale="100K",
        sessions=[beam.Session("0", "2024-03-15", [{"role": "user", "content": "hi"}], [0])],
        questions=[beam.Question("c1", "abstention", 0, "q?", ["r"], "", [0])],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def post(path: str, body: dict) -> dict:
            return await beam._post(client, a.base_url + path, body, a.token)

        with pytest.raises(httpx.HTTPStatusError):
            async with erase_on_exit(post, org="dev"):
                await beam.run_chat(client, a, chat, "k", asyncio.Semaphore(1))
    paths = [p for p, _ in seen]
    register = next(b for p, b in seen if p == "/v1/namespace.register")
    assert register["kind"] == "bench" and register["traffic_class"] == "bench"
    search = next(b for p, b in seen if p == "/v1/memory.search")
    assert search["auto_touch"] is False
    erase = next(b for p, b in seen if p == "/v1/erasure.request")
    assert json.loads(erase["scope_value"])["namespaces"] == [register["namespace"]]
    assert paths.index("/v1/namespace.register") < paths.index("/v1/memory.set_from_messages")
