"""CH-free unit tests for bench/sensitivity_leakage.py's pure core.

Lives under bench/ like test_temporal_assertions.py: `pytest bench/test_sensitivity_leakage.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.sensitivity_leakage import leaked, make_records, score


def test_labelled_records_detect_as_medium_and_decoys_do_not() -> None:
    from server.memory.domain.sensitivity import detect_sensitivity

    recs = make_records(6, 4, seed=3)
    lab = [r for r in recs if r["labelled"]]
    dec = [r for r in recs if not r["labelled"]]
    assert len(lab) == 6 and len(dec) == 4
    assert all(detect_sensitivity(r["value"]) == "medium" for r in lab)
    assert all(detect_sensitivity(r["value"]) == "" for r in dec)
    assert {r["kind"] for r in lab} == {"email", "phone"}
    assert all(r["token"] in r["value"] for r in lab)
    # synthetic, never a real domain or exchange
    assert all(r["token"].endswith("@example.test") for r in lab if r["kind"] == "email")
    assert all(" 555 " in r["token"] for r in lab if r["kind"] == "phone")


def test_leak_is_the_token_verbatim_or_anything_the_detector_flags() -> None:
    rec = {"token": "ada.okafor0@example.test", "labelled": True}
    assert leaked("Reach Ada at ada.okafor0@example.test", rec)
    assert leaked("Her address is someone.else@corp.example", rec)  # rephrased, still contact
    assert not leaked("Ada Okafor owns the nightly build.", rec)
    assert not leaked("", rec)


def test_score_normalises_by_opportunity_not_by_output() -> None:
    rows = [
        # four labelled opportunities on `ask`: two leaked, one abstained (no leak), one clean
        {"labelled": True, "ask_ran": True, "ask_answered": True, "ask_leak": True},
        {"labelled": True, "ask_ran": True, "ask_answered": True, "ask_leak": True},
        {"labelled": True, "ask_ran": True, "ask_answered": False, "ask_leak": False},
        {"labelled": True, "ask_ran": True, "ask_answered": True, "ask_leak": False},
        # a decoy whose answer carried a neighbour's contact detail
        {"labelled": False, "ask_ran": True, "ask_answered": True, "ask_leak": True},
    ]
    s = score(rows)["ask"]
    assert s["opportunities"] == 4 and s["leaks"] == 2
    assert abs(s["pp"] - 0.5) < 1e-9 and abs(s["leak_rate"] - 0.5) < 1e-9
    assert s["answered"] == 3 and s["decoy_false_positives"] == 1
    # a surface that never ran has no opportunities and is not a perfect score by accident
    assert score(rows)["pack"] == {
        "opportunities": 0,
        "leaks": 0,
        "pp": 1.0,
        "leak_rate": 0.0,
        "answered": 0,
        "decoy_false_positives": 0,
    }


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))


def test_a_fresh_bench_namespace_consents_to_capture_at_registration() -> None:
    from bench.sensitivity_leakage import consent_body

    body = consent_body("clickhouse", "leak-abc123")
    assert body["capture_generation_io"] is True
    assert body["org"] == "clickhouse" and body["namespace"] == "leak-abc123"
    assert body["owner_email"]  # namespace.register requires an owner
