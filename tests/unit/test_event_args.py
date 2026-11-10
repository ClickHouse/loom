"""agent_events.args projection: identifiers verbatim, free
text digested, everything else dropped, identity never duplicated, bounded,
never raises."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from server.observability.event_args import (
    IDENTIFIER_KEYS,
    IDENTITY_KEYS,
    TEXT_KEYS,
    project_args,
)


def _sha16(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def test_identifiers_are_kept_verbatim_and_identity_is_not_duplicated():
    out = project_args(
        {
            "org": "acme",
            "namespace": "team",
            "agent": "bot",
            "user_id": "u",
            "session_id": "s",
            "run_id": "r",
            "memory_key": "prefs.editor",
            "memory_type": "preference",
            "top_k": 5,
            "pinned": True,
        }
    )
    assert out["memory_key"] == "prefs.editor"
    assert out["memory_type"] == "preference"
    assert out["top_k"] == "5"
    assert out["pinned"] == "true"
    assert not (set(out) & IDENTITY_KEYS), out
    assert "_sha256" in out


def test_free_text_is_digested_never_stored():
    value = "The user prefers vim keybindings and dark mode."
    out = project_args({"memory_key": "k", "value": value, "query": "vim"})
    assert out["value"] == f"sha256:{_sha16(value)}/{len(value)}"
    assert out["query"] == f"sha256:{_sha16('vim')}/3"
    joined = json.dumps(out)
    assert "prefers vim" not in joined
    assert "dark mode" not in joined


def test_same_arguments_same_digest_regardless_of_key_order():
    a = project_args({"memory_key": "k", "value": "v", "org": "o"})
    b = project_args({"org": "o", "value": "v", "memory_key": "k"})
    assert a == b
    assert a["_sha256"] == b["_sha256"]
    # identity fields do not participate in the whole-call digest
    c = project_args({"memory_key": "k", "value": "v", "org": "other"})
    assert c["_sha256"] == a["_sha256"]


def test_unknown_keys_and_nested_structures_are_dropped():
    out = project_args(
        {
            "memory_key": "k",
            "metadata": {"a": 1},
            "some_future_field": "x",
            "namespace_scope": {"mode": "all"},
        }
    )
    assert set(out) == {"_sha256", "memory_key"}


def test_lists_of_scalars_join_and_truncate():
    out = project_args({"tags": ["a", "b", "c"], "memory_ids": list(range(25))})
    assert out["tags"] == "a,b,c"
    assert out["memory_ids"].endswith(",+5")
    assert out["memory_ids"].startswith("0,1,2,")


def test_long_identifier_is_clipped_and_digest_carries_text_length():
    long_key = "k" * 1000
    out = project_args({"memory_key": long_key, "content": long_key})
    assert len(out["memory_key"]) == 200
    assert out["memory_key"].endswith("…")
    assert out["content"].endswith("/1000")


def test_dates_render_iso_and_messages_digest_as_json():
    when = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
    msgs = [{"role": "user", "content": "secret plans"}]
    out = project_args({"valid_at": when, "messages": msgs})
    assert out["valid_at"] == when.isoformat()
    canon = json.dumps(msgs, sort_keys=True, separators=(",", ":"))
    assert out["messages"] == f"sha256:{_sha16(canon)}/{len(canon)}"
    assert "secret" not in json.dumps(out)


def test_garbage_input_never_raises():
    assert project_args(None) == {}
    assert project_args("not a dict") == {}
    assert project_args({}) == {}

    class Weird:
        def __str__(self):
            raise RuntimeError("boom")

    out = project_args({"memory_key": Weird(), "value": Weird()})
    assert isinstance(out, dict)


def test_bounded_entry_count():
    raw = {k: "x" for k in sorted(IDENTIFIER_KEYS)}
    raw.update({k: "y" for k in sorted(TEXT_KEYS)})
    out = project_args(raw)
    assert len(out) <= 32


def test_agent_event_row_carries_kind_and_args():
    from server.observability.events import AgentEvent

    e = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        operation="memory.set",
        request_id="r",
        latency_ms=1,
        success=True,
        kind="request",
        args={"memory_key": "k"},
    )
    row = e.to_row()
    assert row["kind"] == "request"
    assert row["args"] == {"memory_key": "k"}
    # a worker row defaults to no args and must declare itself
    w = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="-",
        user_id="-",
        session_id="-",
        operation="worker.x",
        request_id="",
        latency_ms=1,
        success=True,
        kind="worker",
    )
    assert w.to_row()["args"] == {}
    assert w.to_row()["kind"] == "worker"


def test_retrieval_outcome_fields_project_as_out_identifiers():
    """The search outcome (abstain, its code, fallback legs,
    calibrated risk) lands on agent_events so the wild abstain rate is a
    GROUP BY, not a log grep."""
    from server.observability.event_args import project_result

    out = project_result(
        {
            "results": [{"memory_key": "m1"}],
            "abstain": True,
            "abstain_code": "below_threshold",
            "fallback_rounds_used": 1,
            "retrieval_risk": "abstain",
            "retrieval_confidence": 0.1234,
            "route_agreement": 0.5,
            "summary_hits": 2,
            "abstain_reason": "free text never stored",
        }
    )
    assert out["out_abstain"] == "true"
    assert out["out_abstain_code"] == "below_threshold"
    assert out["out_fallback_rounds_used"] == "1"
    assert out["out_retrieval_risk"] == "abstain"
    assert out["out_retrieval_confidence"] == "0.1234"
    assert out["out_summary_hits"] == "2"
    assert "out_abstain_reason" not in out
    assert "out_results" not in out


def test_ask_consistency_verdict_is_projected_onto_the_ledger() -> None:
    """The answer-vs-citation verdict rides agent_events.args as
    out_consistency / out_consistency_score; the note (free text) does not."""
    from server.observability.event_args import project_result

    out = project_result(
        {
            "consistency": "contradicted",
            "consistency_score": 0.12,
            "consistency_note": "contradicts [m1]: blue",
            "answer": "long text",
        }
    )
    assert out["out_consistency"] == "contradicted"
    assert out["out_consistency_score"] == "0.12"
    assert "out_consistency_note" not in out and "out_answer" not in out


def test_every_memory_ask_identifier_key_reaches_the_row() -> None:
    """The result budget must hold every identifier key memory.ask returns; a
    shared cap below the key count would silently drop `out_source`,
    `out_summary_hits` and `out_timeline_rows`."""
    from server.memory.api.models_ask import MemoryAskOutput
    from server.observability.event_args import (
        _MAX_RESULT_ENTRIES,
        IDENTIFIER_KEYS,
        IDENTITY_KEYS,
        project_result,
    )

    keys = [
        k for k in MemoryAskOutput.model_fields if k in IDENTIFIER_KEYS and k not in IDENTITY_KEYS
    ]
    assert len(keys) <= _MAX_RESULT_ENTRIES - 8, "leave headroom for the next decision's key"
    out = MemoryAskOutput(answer="a", source="synthesized", route_reason="weak_top_hit")
    projected = project_result(out)
    assert (
        projected["out_source"] == "synthesized" and projected["out_route_reason"] == "weak_top_hit"
    )
    assert "out_timeline_rows" in projected and "out_summary_hits" in projected


def test_a_sectioned_search_records_its_hits_across_sections() -> None:
    from server.memory.api.models_loom_search import SearchOutput
    from server.observability.event_args import project_result

    answered = SearchOutput(
        memory={"results": [{"memory_key": "a"}, {"memory_key": "b"}]},
        knowledge={"results": [{"source_uri": "doc.md"}]},
        conversations={"error": "timeout", "results": []},
    )
    assert project_result(answered)["out_section_hits"] == "3"
    assert project_result(SearchOutput(memory={"results": []}))["out_section_hits"] == "0"
    # An abstaining memory section keeps its hits as evidence, not as an answer; the
    # profile section answers nearly any query by meaning, so it is not counted.
    abstained = SearchOutput(
        memory={"abstain": True, "results": [{"memory_key": "a"}]},
        profile={"results": [{"label": "role"}]},
        tracker={"verdict": "duplicate", "results": [{"number": 7}]},
    )
    # The duplicate-check verdict answers whether a defect is filed, not a recall.
    assert project_result(abstained)["out_section_hits"] == "0"
    # A result with no sections records nothing.
    assert "out_section_hits" not in project_result({"results": [1, 2]})


def test_an_assistant_op_records_how_many_rows_it_found() -> None:
    from server.observability.event_args import project_result

    hits = {"results": [{"memory_key": "a"}, {"memory_key": "b"}]}
    assert project_result(hits, op_name="memory.search")["out_hits"] == "2"
    assert project_result({"entries": []}, op_name="memory.list")["out_hits"] == "0"
    # An abstention found nothing, whatever weak hits it keeps.
    abstained = {"abstain": True, "results": [{"memory_key": "a"}]}
    assert project_result(abstained, op_name="memory.ask")["out_hits"] == "0"
    # Other ops, and a call with no op name, carry none.
    assert "out_hits" not in project_result(hits, op_name="memory.bulk_get")
    assert "out_hits" not in project_result(hits)
    # A get's miss is its found flag, kept as it was.
    miss = project_result({"found": False, "tags": []}, op_name="memory.get")
    assert miss["out_found"] == "false" and "out_hits" not in miss


def test_an_outcome_records_whether_its_label_landed() -> None:
    from server.memory.api.models_temporal import MemoryOutcomeOutput
    from server.observability.event_args import project_result

    landed = MemoryOutcomeOutput(recorded=True, evidence_count_after=1)
    missed = MemoryOutcomeOutput(recorded=False, evidence_count_after=0)
    assert project_result(landed)["out_recorded"] == "true"
    assert project_result(missed)["out_recorded"] == "false"
