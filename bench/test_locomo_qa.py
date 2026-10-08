"""Pure-function tests for the LoCoMo QA bench — no server, no LLM spend.

Pins the dataset reading (`conversation` is a dict of sessions), the published
category mapping, and the aggregation that produces the
two overalls."""

from __future__ import annotations

from bench.locomo_qa import (
    COMPARABLE_CATEGORIES,
    QuestionResult,
    aggregate,
    evidence_distance_bucket,
    evidence_gap,
    evidence_gap_bucket,
    evidence_session_count,
    format_history,
    gold_answer,
    hit_session_numbers,
    iter_sessions,
    parse_judge_label,
    session_date_to_iso,
    source_recall_at,
    turn_to_message,
)


def test_session_date_parses_the_dataset_stamp() -> None:
    assert session_date_to_iso("1:56 pm on 8 May, 2023") == "2023-05-08"
    assert session_date_to_iso("10:15 am on 25 December, 2022") == "2022-12-25"
    assert session_date_to_iso("") == ""
    assert session_date_to_iso("sometime") == ""


def test_iter_sessions_orders_numerically_and_tolerates_gaps() -> None:
    conv = {
        "speaker_a": "Caroline",
        "speaker_b": "Melanie",
        "session_10": [{"speaker": "Caroline", "text": "late"}],
        "session_10_date_time": "1:00 pm on 1 June, 2023",
        "session_2": [{"speaker": "Melanie", "text": "early"}],
        "session_2_date_time": "1:00 pm on 8 May, 2023",
        "session_3": [],  # empty sessions are skipped
    }
    got = iter_sessions(conv)
    assert [k for k, _, _ in got] == ["session_2", "session_10"]
    assert [d for _, d, _ in got] == ["2023-05-08", "2023-06-01"]


def test_turn_to_message_prefixes_speaker_and_inlines_caption() -> None:
    assert turn_to_message({"speaker": "Caroline", "text": "Hey Mel!"}) == {
        "role": "user",
        "content": "Caroline: Hey Mel!",
        "name": "Caroline",
    }
    msg = turn_to_message(
        {"speaker": "Melanie", "text": "Look!", "img_url": "x", "blip_caption": "a dog on a beach"}
    )
    assert msg["content"] == "Melanie: Look! [shared a photo: a dog on a beach]"


def test_gold_answer_is_empty_for_adversarial() -> None:
    assert gold_answer({"category": 5, "adversarial_answer": "self-care"}) == ""
    assert gold_answer({"category": 2, "answer": "7 May 2023"}) == "7 May 2023"
    assert gold_answer({"category": 1, "answer": 42}) == "42"


def test_parse_judge_label() -> None:
    assert parse_judge_label('{"label": "CORRECT"}') == "CORRECT"
    assert parse_judge_label('Reasoning... {"label":"WRONG"}') == "WRONG"
    assert parse_judge_label("WRONG") == "WRONG"
    assert parse_judge_label("CORRECT and WRONG") is None
    assert parse_judge_label("INCORRECT") == "WRONG"
    assert parse_judge_label("Incorrect: the date is off.") == "WRONG"
    assert parse_judge_label("") is None


def test_format_history_leads_with_the_event_date() -> None:
    text = format_history(
        [
            {
                "content_excerpt": "Caroline researched adoption agencies",
                "valid_at": "2023-05-20T00:00:00",
            },
            {
                "content_excerpt": "Melanie bought the figurines",
                "temporal_anchor": "2023-10-21T00:00:00",
                "valid_at": "2023-10-25T00:00:00",
            },
            {
                "content_excerpt": "Melanie went camping",
                "temporal_anchor": "2023-10-19T00:00:00",
                "valid_at": "2023-10-19T00:00:00",
            },
            {"content_excerpt": "Melanie likes camping"},
        ]
    )
    assert text.splitlines() == [
        "- [2023-05-20] Caroline researched adoption agencies",
        "- [2023-10-21, said on 2023-10-25] Melanie bought the figurines",
        "- [2023-10-19] Melanie went camping",
        "- Melanie likes camping",
    ]
    assert format_history([]) == "(no memories retrieved)"


def _qr(
    category: int,
    label: str,
    abstained: bool = False,
    sessions: int = 1,
    recall: float = -1.0,
) -> QuestionResult:
    names = {1: "multi_hop", 2: "temporal", 3: "open_domain", 4: "single_hop", 5: "adversarial"}
    return QuestionResult(
        sample_id="c1",
        namespace="ns",
        question_index=0,
        category=category,
        category_name=names[category],
        question="q",
        gold="g",
        generated="a",
        label=label,
        judge_reply="",
        abstained=abstained,
        hits=3,
        search_ms=10,
        embedding_model="e",
        search_mode="rrf",
        reader="external",
        reader_model="r",
        judge_model="j",
        top_keys="",
        evidence_sessions=sessions,
        source_recall_10=recall,
    )


def test_aggregate_reports_comparable_and_all_overalls() -> None:
    rows = [
        _qr(4, "CORRECT"),
        _qr(4, "WRONG"),
        _qr(1, "CORRECT"),
        _qr(5, "CORRECT", abstained=True),
        _qr(5, "WRONG"),
        _qr(2, "ERROR"),
    ]
    agg = aggregate(rows)
    assert COMPARABLE_CATEGORIES == {1, 2, 3, 4}
    # cat 1-4 judged: 3 rows, 2 correct
    assert agg["overall_comparable"] == round(100 * 2 / 3, 2)
    # cat 1-5 judged: 5 rows, 3 correct
    assert agg["overall_all"] == 60.0
    assert agg["errors"] == 1 and agg["judged"] == 5
    assert agg["by_category"]["single_hop"]["accuracy"] == 50.0
    assert agg["by_category"]["adversarial"]["abstain_rate"] == 50.0
    assert agg["by_category"]["temporal"]["n"] == 0
    assert agg["by_category"]["temporal"]["accuracy"] is None


def test_evidence_session_count_reads_locomo_ids() -> None:
    assert evidence_session_count({"evidence": ["D1:3"]}) == 1
    assert evidence_session_count({"evidence": ["D1:3", "D1:9", "D4:2"]}) == 2
    assert evidence_session_count({"evidence": ["D2:1", "D5:4", "D9:7", "junk"]}) == 3
    assert evidence_session_count({}) == 0
    assert [evidence_distance_bucket(n) for n in (0, 1, 2, 3, 7)] == ["1", "1", "2", "3+", "3+"]


def test_aggregate_slices_accuracy_by_evidence_distance() -> None:
    rows = [
        _qr(4, "CORRECT", sessions=1),
        _qr(4, "WRONG", sessions=1),
        _qr(1, "CORRECT", sessions=2),
        _qr(1, "WRONG", sessions=3),
        _qr(2, "ERROR", sessions=3),
        # adversarial cites nothing and is not a distance row
        _qr(5, "CORRECT", abstained=True, sessions=0),
    ]
    d = aggregate(rows)["by_evidence_sessions"]
    assert {k: d["1"][k] for k in ("n", "correct", "accuracy")} == {
        "n": 2,
        "correct": 1,
        "accuracy": 50.0,
    }
    assert (d["2"]["n"], d["2"]["accuracy"]) == (1, 100.0)
    assert (d["3+"]["n"], d["3+"]["accuracy"]) == (1, 0.0)
    # no hit carried a session: coverage is "not measured", not zero
    assert d["1"]["coverage_n"] == 0 and d["1"]["gold_in_window_pct"] is None


def test_aggregate_splits_misses_into_unretrieved_and_retrieved() -> None:
    rows = [
        _qr(1, "CORRECT", sessions=3, recall=1.0),
        _qr(1, "WRONG", sessions=3, recall=1.0),  # every gold session in the window, misread
        _qr(1, "WRONG", sessions=3, recall=0.33),  # a gold session never retrieved
        _qr(1, "WRONG", sessions=3, recall=0.0),
        _qr(1, "WRONG", sessions=3),  # no session on the hits: not in the coverage count
    ]
    b = aggregate(rows)["by_evidence_sessions"]["3+"]
    assert b["n"] == 5 and b["coverage_n"] == 4
    assert b["gold_in_window"] == 2 and b["gold_in_window_pct"] == 50.0
    assert b["miss_unretrieved"] == 2 and b["miss_retrieved"] == 1


def test_aggregate_reports_abstention_both_ways() -> None:
    rows = [
        _qr(4, "CORRECT"),
        _qr(4, "WRONG", abstained=True),  # answerable, abstained: a false abstain
        _qr(1, "CORRECT"),
        _qr(5, "CORRECT", abstained=True),  # adversarial, abstained: right
        _qr(5, "WRONG"),  # adversarial, answered: wrong
        _qr(2, "ERROR", abstained=True),  # errors do not count either way
    ]
    ab = aggregate(rows)["abstain"]
    assert ab["abstained"] == 2 and ab["adversarial_abstained"] == 1
    assert ab["answerable"] == 3 and ab["answerable_abstained"] == 1
    assert ab["abstain_precision"] == 50.0
    assert ab["false_abstain_rate"] == round(100 / 3, 2)
    assert aggregate([_qr(4, "CORRECT")])["abstain"]["abstain_precision"] is None


def test_evidence_gap_is_last_session_minus_earliest_evidence() -> None:
    q = {"evidence": ["D3:2", "D11:5"]}
    assert evidence_gap(q, 30) == 27
    assert evidence_gap({"evidence": []}, 30) == 0  # adversarial: no evidence
    assert evidence_gap(q, 0) == 0  # unknown conversation length
    assert evidence_gap_bucket(0) == "0"
    assert evidence_gap_bucket(3) == "1-5"
    assert evidence_gap_bucket(27) == "26+"
    assert evidence_gap_bucket(16) == "16-20"


def test_source_recall_reads_hit_session_ids_in_rank_order() -> None:
    hits = [
        {"memory_key": "a", "session_id": "ns:session_4"},
        {"memory_key": "b"},  # summary/chunk rows carry no session
        {"memory_key": "c", "session_id": "ns:session_9"},
        {"memory_key": "d", "session_id": "ns:session_1"},
    ]
    sessions = hit_session_numbers(hits, "ns")
    assert sessions == [4, 9, 1]
    assert source_recall_at(sessions, {4, 1}, 2) == 0.5  # session 1 sits at rank 3
    assert source_recall_at(sessions, {4, 1}, 5) == 1.0
    assert source_recall_at(sessions, set(), 5) is None  # no gold → not measured
    assert hit_session_numbers([{"memory_key": "x"}], "ns") == []


def test_aggregate_slices_by_evidence_gap_with_recall_coverage() -> None:
    def r(cat: int, gap: int, ok: bool, rec10: float) -> QuestionResult:
        return QuestionResult(
            sample_id="c",
            namespace="c",
            question_index=0,
            category=cat,
            category_name="x",
            question="q",
            gold="g",
            generated="a",
            label="CORRECT" if ok else "WRONG",
            judge_reply="",
            abstained=False,
            hits=10,
            search_ms=1,
            embedding_model="",
            search_mode="semantic",
            reader="external",
            reader_model="m",
            judge_model="j",
            top_keys="",
            evidence_gap=gap,
            source_recall_5=rec10,
            source_recall_10=rec10,
        )

    agg = aggregate(
        [r(1, 2, True, 1.0), r(1, 4, False, 0.0), r(2, 12, True, -1.0), r(5, 0, False, -1.0)]
    )
    gap = agg["by_evidence_gap"]
    assert gap["1-5"]["n"] == 2 and gap["1-5"]["accuracy"] == 50.0
    assert gap["1-5"]["source_recall_10"] == 50.0 and gap["1-5"]["recall_coverage"] == 2
    # a hit set without session ids is not measured, not zero recall
    assert gap["11-15"]["n"] == 1 and gap["11-15"]["source_recall_10"] is None
    assert gap["11-15"]["recall_coverage"] == 0


# ---------------------------------------------------------------------------
# Write-policy arms, paired comparison, provenance.
# ---------------------------------------------------------------------------


def test_policy_arms_parse_and_name_namespaces() -> None:
    from bench.locomo_qa import arm_namespace, parse_policies, register_extra_for

    assert parse_policies("") == ["default"]
    assert parse_policies("default, newest_wins,newest_wins") == ["default", "newest_wins"]
    assert arm_namespace("locomo", "default", "conv-1", "abc123") == "locomo-conv-1-abc123"
    assert (
        arm_namespace("locomo", "newest_wins", "conv-1", "abc123")
        == "locomo-newest_wins-conv-1-abc123"
    )
    assert register_extra_for("default") is None
    assert register_extra_for("newest_wins") == {"default_conflict_policy": "newest_wins"}


def _row(sample: str, qi: int, label: str, ns: str, keys: str, policy: str) -> dict[str, str]:
    return {
        "sample_id": sample,
        "question_index": str(qi),
        "label": label,
        "namespace": ns,
        "top_keys": keys,
        "policy": policy,
        "category_name": "multi_hop",
        "question": "q",
        "gold": "g",
    }


def test_paired_compare_reports_delta_and_flips() -> None:
    from bench.locomo_qa import paired_compare

    a = [
        _row("s1", 0, "WRONG", "ns-a", "k1", "default"),
        _row("s1", 1, "CORRECT", "ns-a", "k2", "default"),
        _row("s2", 0, "CORRECT", "ns-a2", "k3", "default"),
        _row("s2", 9, "CORRECT", "ns-a2", "k9", "default"),
    ]
    b = [  # s2/9 only in A → not paired
        _row("s1", 0, "CORRECT", "ns-b", "k1", "newest_wins"),
        _row("s1", 1, "CORRECT", "ns-b", "k2", "newest_wins"),
        _row("s2", 0, "WRONG", "ns-b2", "k3", "newest_wins"),
    ]
    rep = paired_compare(a, b)
    assert rep["paired_questions"] == 3 and rep["conversations"] == 2
    assert rep["policy_a"] == "default" and rep["policy_b"] == "newest_wins"
    assert rep["a_wrong_b_right"] == [("s1", "0")] and rep["a_right_b_wrong"] == [("s2", "0")]
    assert rep["delta_pp"] == 0.0 and rep["ci95_pp"] is not None


def test_attribute_flips_names_the_write_decision() -> None:
    from bench.locomo_qa import attribute_flips

    a = [_row("s1", 0, "CORRECT", "ns-a", "user_dog | user_city", "default")]
    b = [  # the second row is unpaired and ignored
        _row("s1", 0, "WRONG", "ns-b", "user_dog | user_city", "newest_wins"),
        _row("s1", 1, "WRONG", "ns-b", "x", "newest_wins"),
    ]
    mem = {
        "ns-a": {"user_dog": {"status": "active"}, "user_city": {"status": "active"}},
        "ns-b": {
            "user_dog": {"status": "superseded", "superseded_by": "id9"},
            "user_city": {"status": "active"},
        },
    }
    drops = {"ns-b": [{"stage": "write", "reason": "superseded_in_batch", "item": "user_dog_2"}]}
    flips = attribute_flips(a, b, mem, drops)
    assert len(flips) == 1
    fl = flips[0]
    assert fl["a"]["decision"] == "intact" and fl["b"]["decision"] == "superseded"
    assert fl["b"]["superseded"] == ["user_dog"] and fl["b"]["dropped_in_batch"] == ["user_dog_2"]
    # Same verdict on both sides → no flip.
    same = [_row("s1", 0, "CORRECT", "ns-b", "k", "newest_wins")]
    assert attribute_flips(a, same, mem, drops) == []
    # Flagged-but-read rows are named too.
    mem["ns-b"]["user_dog"] = {"status": "conflicted"}
    assert attribute_flips(a, b, mem, {})[0]["b"]["decision"] == "conflicted"


def test_last_session_is_read_from_the_sample_conversation() -> None:
    """The evidence-gap axis takes the last session from the sample's
    `conversation`, not from the sample itself."""
    from bench.locomo_qa import evidence_gap, iter_sessions

    sample = {
        "sample_id": "conv-x",
        "conversation": {
            "session_1": [{"speaker": "A", "text": "hi"}],
            "session_1_date_time": "1:00 pm on 1 May, 2023",
            "session_19": [{"speaker": "A", "text": "bye"}],
            "session_19_date_time": "1:00 pm on 1 June, 2023",
        },
        "qa": [{"question": "q", "answer": "a", "evidence": ["D1:3"], "category": 2}],
    }
    assert list(iter_sessions(sample)) == []
    sessions = iter_sessions(sample["conversation"])
    last = max((int(k.split("_")[1]) for k, _, _ in sessions), default=0)
    assert last == 19
    assert evidence_gap(sample["qa"][0], last) == 18


def test_aggregate_reports_the_papers_columns() -> None:
    """Token F1 / BLEU-1 per category and two overall averages,
    plus the mean prompt size memory.ask reported."""
    from bench.locomo_qa import QuestionResult, aggregate

    def _r(
        cat: int, name: str, f1: float, bleu: float, toks: int, label: str = "CORRECT"
    ) -> QuestionResult:
        return QuestionResult(
            sample_id="s",
            namespace="n",
            question_index=0,
            category=cat,
            category_name=name,
            question="q",
            gold="g",
            generated="a",
            label=label,
            judge_reply="",
            abstained=False,
            hits=1,
            search_ms=1,
            embedding_model="",
            search_mode="",
            reader="ask",
            reader_model="",
            judge_model="",
            top_keys="",
            f1=f1,
            bleu1=bleu,
            f1_simplified=f1,
            bleu1_simplified=bleu,
            context_tokens=toks,
        )

    rows = [
        _r(1, "single_hop", 0.5, 0.25, 900),
        _r(1, "single_hop", 1.0, 0.75, 1100),
        _r(2, "multi_hop", 0.2, 0.1, 2000),
        _r(5, "adversarial", -1.0, -1.0, -1),  # no gold → unscored
        _r(1, "single_hop", -1.0, -1.0, -1, label="ERROR"),
    ]
    agg = aggregate(rows)
    assert agg["by_category"]["single_hop"]["f1"] == 75.0
    assert agg["by_category"]["single_hop"]["context_tokens_mean"] == 1000
    assert agg["by_category"]["adversarial"]["f1"] is None
    pt = agg["papers_table"]
    assert pt["scored"] == 3  # the adversarial row has no gold; the ERROR row was not scored
    assert pt["f1_over_questions"] == round(100 * (0.5 + 1.0 + 0.2) / 3, 2)
    assert pt["bleu1_over_questions"] == round(100 * (0.25 + 0.75 + 0.1) / 3, 2)
    assert pt["f1_over_categories"] == round((75.0 + 20.0) / 2, 2)
    assert pt["context_tokens_mean"] == round((900 + 1100 + 2000) / 3)


def test_majority_label_is_conservative_on_ties_and_outvotes_errors() -> None:
    from bench.locomo_qa import majority_label

    assert majority_label(["CORRECT", "CORRECT", "WRONG"]) == "CORRECT"
    assert majority_label(["CORRECT", "WRONG"]) == "WRONG"  # tie → WRONG
    # judged runs outvote errors
    assert majority_label(["CORRECT", "ERROR", "ERROR"]) == "CORRECT"
    assert majority_label(["ERROR", "ERROR"]) == "ERROR"
    assert majority_label([]) == "WRONG"


def test_fold_repeats_keeps_the_first_row_and_records_every_label() -> None:
    from bench.locomo_qa import aggregate, fold_repeats

    def _r(label: str) -> QuestionResult:
        return QuestionResult(
            sample_id="c",
            namespace="ns",
            question_index=1,
            category=1,
            category_name="multi_hop",
            question="q",
            gold="g",
            generated=f"a-{label}",
            label=label,
            judge_reply="",
            abstained=False,
            hits=5,
            search_ms=10,
            embedding_model="m",
            search_mode="rrf",
            reader="ask",
            reader_model="x",
            judge_model="j",
            top_keys="k1",
        )

    folded = fold_repeats([_r("CORRECT"), _r("WRONG"), _r("CORRECT")])
    assert folded.label == "CORRECT" and folded.generated == "a-CORRECT"
    assert folded.repeat_labels == "CORRECT|WRONG|CORRECT" and folded.repeat_agreement == 0.667
    stable = fold_repeats([_r("WRONG"), _r("WRONG")])
    agg = aggregate([folded, stable])
    # `repeats` also carries run_accuracy_* blocks; pin the
    # fold's own keys only, so a new block does not fail the fold's test.
    assert {
        k: agg["repeats"][k]
        for k in ("questions_repeated", "unstable", "unstable_rate", "agreement_mean")
    } == {
        "questions_repeated": 2,
        "unstable": 1,
        "unstable_rate": 50.0,
        "agreement_mean": 0.834,
    }
    # a single run leaves the columns at their defaults and reports nothing
    assert aggregate([_r("CORRECT")])["repeats"]["questions_repeated"] == 0


def test_aggregate_reports_accuracy_exceeding_recall() -> None:
    from bench.locomo_qa import aggregate

    def _r(label: str, r5: float, r10: float) -> QuestionResult:
        return QuestionResult(
            sample_id="c",
            namespace="ns",
            question_index=1,
            category=1,
            category_name="multi_hop",
            question="q",
            gold="g",
            generated="a",
            label=label,
            judge_reply="",
            abstained=False,
            hits=5,
            search_ms=10,
            embedding_model="m",
            search_mode="rrf",
            reader="ask",
            reader_model="x",
            judge_model="j",
            top_keys="k1",
            source_recall_5=r5,
            source_recall_10=r10,
        )

    rows = [
        _r("CORRECT", 1.0, 1.0),
        _r("CORRECT", 0.0, 0.0),
        _r("WRONG", 0.0, 0.0),
        _r("WRONG", 0.0, 0.5),
    ]
    rec = aggregate(rows)["recall"]
    assert rec["recall_at_5"] == 25.0 and rec["recall_at_10"] == 37.5
    assert rec["zero_recall_at_10"] == 2 and rec["zero_recall_at_10_correct"] == 1
    assert rec["zero_recall_at_5"] == 3 and rec["zero_recall_at_5_correct"] == 1
    # a row the server gave no session ids for (-1) is left out of the denominator
    assert aggregate([_r("CORRECT", -1.0, -1.0)])["recall"]["recall_at_10"] is None


def test_plane_mix_classifies_hit_keys_by_prefix() -> None:
    from bench.locomo_qa import plane_mix, plane_of

    assert plane_of("summary:ns:session_3") == "digest"
    assert plane_of("chunk:ns:session_3:0") == "chunk"
    assert plane_of("period_digest_session_1_session_5") == "period"
    assert plane_of("caroline_support_group") == "fact"
    mix = plane_mix(["summary:a | chunk:b | fact_c", "summary:d", "", None])
    assert mix == {"chunk": 0.25, "digest": 0.5, "fact": 0.25}
    assert plane_mix([]) == {}


def test_aggregate_reports_plane_mix_per_category() -> None:
    rows = [_qr(4, "CORRECT"), _qr(4, "WRONG"), _qr(2, "ERROR")]
    rows[0] = rows[0].__class__(**{**rows[0].__dict__, "top_keys": "summary:x | chunk:y"})
    rows[1] = rows[1].__class__(**{**rows[1].__dict__, "top_keys": "fact_z"})
    agg = aggregate(rows)
    assert agg["by_category"]["single_hop"]["plane_mix"] == {
        "chunk": 0.333,
        "digest": 0.333,
        "fact": 0.333,
    }
    assert agg["by_category"]["temporal"]["plane_mix"] == {}


def test_hit_session_numbers_credit_a_period_digest_via_its_source_tags() -> None:
    from bench.locomo_qa import hit_session_numbers

    hits = [
        {"memory_key": "chunk:ns:session_3:0", "session_id": "ns:session_3"},
        {
            "memory_key": "period_digest_session_1_session_5",
            "session_id": "-",
            "tags": ["period_digest", "src:summary:ns:session_1", "src:summary:ns:session_2"],
        },
        {"memory_key": "fact_without_session", "session_id": ""},
    ]
    assert hit_session_numbers(hits, "ns") == [3, 1, 2]


def test_evidence_speaker_reads_the_transcript() -> None:
    from bench.locomo_qa import evidence_speaker

    conv = {
        "speaker_a": "Caroline",
        "speaker_b": "Melanie",
        "session_1": [
            {"speaker": "Caroline", "dia_id": "D1:1", "text": "I painted a bowl."},
            {"speaker": "Melanie", "dia_id": "D1:2", "text": "I ran a marathon."},
        ],
        "session_1_date_time": "1:00 pm on 1 June, 2023",
    }
    # Speaker swap: Melanie is named, Caroline said it → "never said that".
    swap = {"question": "What did Melanie paint?", "evidence": ["D1:1"], "category": 5}
    assert evidence_speaker(swap, conv) == "other"
    # Content swap: Caroline is named and Caroline said the evidence turn; the
    # question changed the fact → "the memories do not record that".
    content = {"question": "What did Caroline sculpt?", "evidence": ["D1:1"], "category": 5}
    assert evidence_speaker(content, conv) == "named"
    # Answerable items split the same way.
    ans = {"question": "What did Caroline run?", "evidence": ["D1:2"], "category": 4}
    assert evidence_speaker(ans, conv) == "other"
    assert evidence_speaker({**ans, "evidence": ["D1:1"]}, conv) == "named"
    # Unclassifiable: both speakers named, no evidence, no conversation, mixed.
    both = {"question": "Did Caroline tell Melanie?", "evidence": ["D1:1"], "category": 5}
    assert evidence_speaker(both, conv) == ""
    assert evidence_speaker({**swap, "evidence": []}, conv) == ""
    assert evidence_speaker(swap, None) == ""
    assert evidence_speaker({**swap, "evidence": ["D1:1", "D1:2"]}, conv) == ""
    # Name matching is a whole word: "Mel" is not Melanie.
    assert evidence_speaker({**swap, "question": "What did Mel paint?"}, conv) == ""


def test_aggregate_splits_adversarial_by_kind() -> None:
    def row(i: int, who: str, label: str, category: int = 5) -> QuestionResult:
        return QuestionResult(
            sample_id="s",
            namespace="ns",
            question_index=i,
            category=category,
            category_name="adversarial" if category == 5 else "single_hop",
            question="q",
            gold="",
            generated="",
            label=label,
            judge_reply="",
            abstained=label == "CORRECT",
            hits=0,
            search_ms=0,
            embedding_model="",
            search_mode="semantic",
            reader="ask",
            reader_model="",
            judge_model="",
            top_keys="",
            evidence_speaker=who,
        )

    rows = [
        row(0, "other", "CORRECT"),
        row(1, "other", "CORRECT"),
        row(2, "other", "WRONG"),
        row(3, "named", "WRONG"),
        row(4, "", "CORRECT"),
        row(5, "other", "WRONG", category=4),
        row(6, "named", "CORRECT", category=4),
    ]
    es = aggregate(rows)["by_evidence_speaker"]
    assert es["adversarial"] == {"n": 5, "accuracy": 60.0}
    assert es["adversarial_speaker_swap"] == {"n": 3, "accuracy": round(100 * 2 / 3, 2)}
    assert es["adversarial_content_swap"] == {"n": 1, "accuracy": 0.0}
    assert es["adversarial_unclassified"] == 1
    assert es["answerable_other_speaker"] == {"n": 1, "accuracy": 0.0}
    assert es["answerable_named_speaker"] == {"n": 1, "accuracy": 100.0}
