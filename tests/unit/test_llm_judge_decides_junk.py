"""The durability verifier applies the LLM judge's junk rubric."""

from __future__ import annotations

from server.memory.app import durability_verifier as dv
from server.memory.domain.extraction_prompts import DURABILITY_VERIFIER_PROMPT_VERSION
from server.settings import Settings


def test_part_a_is_the_judge_rubric() -> None:
    p = dv.VERIFIER_SYSTEM_PROMPT
    assert "useful to recall in a week" in p
    for junk in (
        "this session's own activity",
        "restates a tool's output or a file's text",
        "transient state",
        "test chatter",
        "vague/garbled fragment",
    ):
        assert junk in p, junk
    # wording that made the judge keep junk is absent
    assert "When unsure, KEEP" not in p and "measured results or numbers" not in p


def test_part_b_folds_and_the_answer_shape_are_unchanged() -> None:
    p = dv.VERIFIER_SYSTEM_PROMPT
    assert '"same_claim"' in p and '"superseded"' in p and '"verdicts"' in p
    v = dv.parse_verdicts(
        '{"verdicts":[{"i":0,"keep":false,"why":"narrates"}],"same_claim":[],"superseded":[]}', 1
    )
    assert v.ok and 0 in v.drop


def test_the_default_verifier_is_one_opus_pass() -> None:
    assert Settings.model_fields["memory_extract_verify_models"].default == "claude-opus-5-5"
    assert DURABILITY_VERIFIER_PROMPT_VERSION.startswith("durability-verifier-v")
