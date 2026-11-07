"""The judge stage. Pure parts (prompt, parsing,
folding a judgement into the answer) and the handler gate — off by default,
only on a `related` verdict, fails open. CH-free, LLM faked."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.tracker.app import dup_judge as dj


class _LLM:
    def __init__(self, text: str = "", *, fail: bool = False) -> None:
        self.text, self.fail = text, fail
        self.calls: list[dict[str, Any]] = []
        self.model = "fake-judge"

    async def complete(self, system, user, max_tokens=1024, response_format=None):
        self.calls.append(
            {"system": system, "user": user, "max_tokens": max_tokens, "fmt": response_format}
        )
        if self.fail:
            raise RuntimeError("provider down at https://gw.internal:8443")
        return SimpleNamespace(text=self.text, model=self.model, tokens_in=321, tokens_out=45)


def _out(items: list[dict[str, Any]], verdict: str = "related") -> Any:
    return SimpleNamespace(verdict=verdict, items=items, undecided=False, judge={})


def test_prompt_carries_the_draft_and_each_candidate_by_number() -> None:
    text = dj.build_user_prompt(
        "Lazy proxy hides MergeTreeData",
        "system.parts empty " * 10,
        [
            {"number": 117420, "title": "TableProxy breaks part commands", "body": "x" * 2000},
            {"number": 120368, "title": "Other", "body": ""},
        ],
    )
    assert text.startswith("DRAFT:\nLazy proxy hides MergeTreeData\n")
    assert "[#117420] TableProxy breaks part commands\n" in text and "[#120368] Other\n" in text
    assert len(text) < 3000 + 300 + 1500 + 300 + 400  # bodies are clipped, not pasted whole
    assert "…" in text  # the 2000-char body was cut at the candidate limit


def test_verdicts_parse_only_the_numbers_asked_and_known_labels() -> None:
    raw = (
        'Sure. {"verdicts": [{"number": 117420, "label": "Duplicate", "reason": "same lazy-load path"},'
        ' {"number": 999, "label": "duplicate"}, {"number": 120368, "label": "maybe"},'
        ' {"number": 117441, "label": "related", "reason": "same component, other trigger"}]}'
    )
    labels, reasons = dj.parse_verdicts(raw, [117420, 120368, 117441])
    assert labels == {117420: "duplicate", 117441: "related"}
    assert reasons[117420] == "same lazy-load path" and reasons[117441].startswith("same component")
    # a `duplicate` the model gives no cause for is a `related`
    assert dj.parse_verdicts(
        '{"verdicts": [{"number": 1, "label": "duplicate", "reason": "  "}]}', [1]
    ) == ({1: "related"}, {1: ""})
    assert dj.parse_verdicts("no json here", [1]) == ({}, {})
    assert dj.parse_verdicts('{"verdicts": "nope"}', [1]) == ({}, {})


@pytest.mark.asyncio
async def test_judge_calls_once_with_json_format_and_fails_open() -> None:
    llm = _LLM(
        json.dumps({"verdicts": [{"number": 7, "label": "duplicate", "reason": "same cause"}]})
    )
    j = await dj.judge(
        llm,
        "t",
        "b",
        [{"number": 7, "title": "a", "body": "b"}, {"number": 8, "title": "c", "body": ""}],
    )
    assert j.ran and j.labels == {7: "duplicate"} and j.duplicates() == [7]
    assert j.model == "fake-judge" and j.tokens_in == 321 and j.tokens_out == 45
    (call,) = llm.calls
    assert call["fmt"] == {"type": "json_object"} and call["system"] == dj.SYSTEM
    assert call["max_tokens"] == 80 + 60 * 2 and "[#7] a" in call["user"]
    down = await dj.judge(_LLM(fail=True), "t", "b", [{"number": 7}])
    # A viewer reads the error: the kind; the provider's text is in the log.
    assert not down.ran and down.labels == {} and down.error == "RuntimeError"
    assert not (await dj.judge(None, "t", "b", [{"number": 7}])).ran
    assert not (await dj.judge(llm, "", "", [{"number": 7}])).ran  # nothing to judge against


def test_apply_promotes_a_related_band_duplicate_and_never_demotes_or_overrides_the_record() -> (
    None
):
    items = [
        {
            "number": 117440,
            "relation": "related",
            "relation_recorded": "none",
            "confidence": 0.18,
            "evidence": ["found by: lexical"],
        },
        {
            "number": 117420,
            "relation": "related",
            "relation_recorded": "none",
            "confidence": 0.20,
            "evidence": ["found by: vector"],
        },
        {
            "number": 117441,
            "relation": "related",
            "relation_recorded": "related_to",
            "confidence": 0.09,
            "evidence": [],
        },
    ]
    j = dj.Judgement(
        labels={117420: "duplicate", 117440: "different", 117441: "duplicate"},
        reasons={117420: "same lazy-load path hides MergeTreeData"},
        ran=True,
        model="m",
        tokens_in=300,
        tokens_out=40,
    )
    out = dj.apply(_out(items), j)
    assert out.verdict == "duplicate_of" and out.undecided is False
    assert [it["number"] for it in out.items] == [117420, 117440, 117441]
    top = out.items[0]
    assert top["relation"] == "duplicate" and top["judge"] == {
        "label": "duplicate",
        "reason": j.reasons[117420],
    }
    assert top["evidence"][-1] == "judge: same defect — same lazy-load path hides MergeTreeData"
    assert top["confidence"] == 0.20  # the fit's number is kept; the verdict is the judge's
    # `different` never demotes; a recorded related_to never promotes
    assert out.items[1]["relation"] == "related" and out.items[1]["judge"]["label"] == "different"
    assert out.items[2]["relation"] == "related" and out.items[2]["judge"]["label"] == "duplicate"
    assert out.judge["promoted"] == [117420] and out.judge["labels"] == {
        "117420": "duplicate",
        "117440": "different",
        "117441": "duplicate",
    }
    assert out.judge["ran"] and out.judge["model"] == "m" and out.judge["tokens_in"] == 300


def test_apply_with_a_judgement_that_did_not_run_changes_nothing_but_says_so() -> None:
    items = [{"number": 1, "relation": "related", "relation_recorded": "none", "confidence": 0.1}]
    out = dj.apply(_out(items), dj.Judgement(error="RuntimeError: provider down"))
    assert (
        out.verdict == "related"
        and out.items[0]["relation"] == "related"
        and "judge" not in out.items[0]
    )
    assert out.judge["ran"] is False and out.judge["error"] == "RuntimeError: provider down"


@pytest.mark.asyncio
async def test_handler_gate_runs_only_when_enabled_and_the_verdict_is_related(monkeypatch) -> None:
    from server.settings import settings
    from server.tracker.app import dupcheck_answer as hd_answer

    calls: list[Any] = []

    class _Src:
        async def embed_items_by_number(self, org, ns, *, kind, numbers, body_chars, stamp=""):
            calls.append(("text", list(numbers), body_chars))
            return [{"number": n, "title": f"t{n}", "body": f"b{n}"} for n in numbers]

    llm = _LLM(
        json.dumps({"verdicts": [{"number": 5, "label": "duplicate", "reason": "same cause"}]})
    )
    monkeypatch.setattr("server.llm.client.get_llm_client_for_model", lambda model: llm)
    inp = SimpleNamespace(org="o", namespace="qa", title="draft title", body="draft body", top_k=5)
    run = {
        "q": SimpleNamespace(text="draft title\ndraft body"),
        "item": None,
        "unserved": [
            {
                "number": 7,
                "kind": "issue",
                "relation": "none",
                "relation_recorded": "none",
                "confidence": 0.046,
                "evidence": ["found by: vector"],
            },
            {
                "number": 8,
                "kind": "issue",
                "relation": "none",
                "relation_recorded": "none",
                "confidence": 0.01,
            },
        ],
    }
    related = _out(
        [
            {
                "number": 5,
                "kind": "issue",
                "relation": "related",
                "relation_recorded": "none",
                "confidence": 0.12,
            },
            {
                "number": 6,
                "kind": "issue",
                "relation": "related",
                "relation_recorded": "none",
                "confidence": 0.06,
            },
        ]
    )
    # off by default: untouched
    monkeypatch.setattr(settings, "tracker_dupcheck_judge_enabled", False)
    out = await hd_answer.judge_uncertain(_Src(), inp, run, related, fit={"coef": {}})
    assert out.verdict == "related" and calls == [] and llm.calls == []
    # on: one text read for the top_k issues, one call, promotion
    monkeypatch.setattr(settings, "tracker_dupcheck_judge_enabled", True)
    monkeypatch.setattr(settings, "tracker_dupcheck_judge_top_k", 5)
    monkeypatch.setattr(settings, "tracker_dupcheck_judge_body_chars", 900)
    out = await hd_answer.judge_uncertain(_Src(), inp, run, related, fit={"coef": {}})
    assert (
        calls == [("text", [5, 6, 7, 8], 900)] and len(llm.calls) == 1
    )  # served first, then under the line
    assert (
        out.verdict == "duplicate_of"
        and out.items[0]["number"] == 5
        and out.judge["promoted"] == [5]
    )
    assert (
        "[#5] t5\nb5" in llm.calls[0]["user"]
        and "DRAFT:\ndraft title\ndraft body" in llm.calls[0]["user"]
    )
    # never on a duplicate_of, a none, or without a fit
    for o, fit in (
        (_out([], "none"), {"coef": {}}),
        (_out([{"number": 5}], "duplicate_of"), {"coef": {}}),
        (related, None),
    ):
        before = len(llm.calls)
        await hd_answer.judge_uncertain(_Src(), inp, run, o, fit=fit)
        assert len(llm.calls) == before


def test_apply_serves_an_unserved_candidate_the_judge_confirms_and_keeps_top_k() -> None:
    served = [
        {
            "number": 117440,
            "kind": "issue",
            "relation": "related",
            "relation_recorded": "none",
            "confidence": 0.067,
            "evidence": ["found by: lexical, error_code"],
        },
    ]
    under = [
        {
            "number": 117420,
            "kind": "issue",
            "relation": "none",
            "relation_recorded": "none",
            "confidence": 0.046,
            "evidence": ["found by: lexical, vector"],
        },
        {
            "number": 117426,
            "kind": "issue",
            "relation": "none",
            "relation_recorded": "related_to",
            "confidence": 0.03,
        },
        {
            "number": 117441,
            "kind": "issue",
            "relation": "none",
            "relation_recorded": "none",
            "confidence": 0.02,
        },
    ]
    j = dj.Judgement(
        labels={117440: "related", 117420: "duplicate", 117426: "duplicate", 117441: "different"},
        reasons={117420: "same lazy-load defect"},
        ran=True,
        model="m",
    )
    out = dj.apply(_out(served), j, unserved=under, top_k=1)
    assert out.verdict == "duplicate_of"
    assert [it["number"] for it in out.items] == [117420]  # the confirmed duplicate, top_k = 1
    top = out.items[0]
    assert top["relation"] == "duplicate" and top["confidence"] == 0.046
    assert top["evidence"] == [
        "found by: lexical, vector",
        "judge: same defect — same lazy-load defect",
    ]
    assert top["judge"] == {"label": "duplicate", "reason": "same lazy-load defect"}
    assert out.judge["promoted"] == [117420] and out.judge["inserted"] == [117420]
    # a recorded related_to under the line is never served as a duplicate; `different` is never served
    assert under[1].get("relation") == "none" and "judge" not in under[1]
    out2 = dj.apply(_out(list(served)), j, unserved=under, top_k=5)
    assert [it["number"] for it in out2.items] == [117420, 117440]


@pytest.mark.asyncio
async def test_on_a_duplicate_verdict_the_judge_reads_only_strong_unserved_rows(
    monkeypatch,
) -> None:
    """The fit already answered duplicate_of, so the judge stays
    out — unless a row under the related line came from a fix in flight or
    the vector leg's top rows; then it
    reads it beside the served duplicate and serves it if confirmed."""
    from server.settings import settings
    from server.tracker.app import dupcheck_answer as hd_answer

    calls: list[Any] = []

    class _Src:
        async def embed_items_by_number(self, org, ns, *, kind, numbers, body_chars, stamp=""):
            calls.append(list(numbers))
            return [{"number": n, "title": f"t{n}", "body": f"b{n}"} for n in numbers]

    llm = _LLM(
        json.dumps(
            {
                "verdicts": [
                    {"number": 118168, "label": "duplicate", "reason": "x"},
                    {"number": 120550, "label": "duplicate", "reason": "same defect"},
                ]
            }
        )
    )
    monkeypatch.setattr("server.llm.client.get_llm_client_for_model", lambda model: llm)
    monkeypatch.setattr(settings, "tracker_dupcheck_judge_enabled", True)
    inp = SimpleNamespace(org="o", namespace="qa", title="draft", body="body", top_k=5)
    served = [
        {
            "number": 118168,
            "kind": "issue",
            "relation": "duplicate",
            "relation_recorded": "none",
            "confidence": 0.44,
        }
    ]
    weak = {
        "number": 102481,
        "kind": "issue",
        "relation": "none",
        "relation_recorded": "none",
        "confidence": 0.01,
        "why": ["lexical"],
        "features": {},
    }
    strong = {
        "number": 120550,
        "kind": "issue",
        "relation": "none",
        "relation_recorded": "none",
        "confidence": 0.006,
        "why": ["lexical", "fix_path"],
        "features": {"fix_path": 1.0},
    }
    # only weak rows under the line: the judge stays out
    run = {"q": SimpleNamespace(text="draft"), "item": None, "unserved": [weak]}
    out = await hd_answer.judge_uncertain(
        _Src(), inp, run, _out(list(served), "duplicate_of"), fit={"coef": {}}
    )
    assert out.verdict == "duplicate_of" and calls == [] and llm.calls == []
    # a fix-in-flight row under the line: judged beside the served duplicate, then served
    run = {"q": SimpleNamespace(text="draft"), "item": None, "unserved": [weak, strong]}
    out = await hd_answer.judge_uncertain(
        _Src(), inp, run, _out(list(served), "duplicate_of"), fit={"coef": {}}
    )
    assert calls == [[118168, 120550]] and len(llm.calls) == 1
    assert [it["number"] for it in out.items] == [118168, 120550]
    assert out.items[1]["relation"] == "duplicate" and out.judge["inserted"] == [120550]
    # the vector leg's first row counts as strong; its third does not
    vec1 = {
        "number": 77,
        "kind": "issue",
        "relation": "none",
        "relation_recorded": "none",
        "confidence": 0.02,
        "why": ["vector"],
        "features": {"vector_inv_rank": 1.0},
    }
    vec3 = {**vec1, "features": {"vector_inv_rank": 0.33}}
    assert hd_answer._strong_unserved(vec1) and not hd_answer._strong_unserved(vec3)
    assert not hd_answer._strong_unserved(weak)
