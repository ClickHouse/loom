"""LongMemEval robustness variants: evidence position and context scale as
pure transforms; evidence is never dropped."""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("lme_variants", ROOT / "bench" / "lme_variants.py")
lv = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(lv)


def _item(n: int = 9, evidence=("s3", "s7"), qid="q1") -> dict:
    return {
        "question_id": qid,
        "haystack_sessions": [[{"role": "user", "content": f"turn {i}"}] for i in range(n)],
        "haystack_dates": [f"2024/01/{i + 1:02d} (Mon) 10:00" for i in range(n)],
        "haystack_session_ids": [f"s{i}" for i in range(n)],
        "answer_session_ids": list(evidence),
    }


def test_unmodified_item_is_returned_with_empty_label():
    it = _item()
    out = lv.apply_variant(it)
    assert out["variant"] == "" and out["haystack_session_ids"] == it["haystack_session_ids"]


def test_position_moves_evidence_as_a_block_and_keeps_dates_aligned():
    it = _item()
    for pos, where in (("early", 0), ("late", 7), ("middle", 3)):
        out = lv.apply_variant(it, position=pos)
        ids = out["haystack_session_ids"]
        assert ids[where : where + 2] == ["s3", "s7"], (pos, ids)
        assert len(ids) == 9 and sorted(ids) == sorted(it["haystack_session_ids"])
        # dates and sessions travel with their ids
        i3 = ids.index("s3")
        assert out["haystack_dates"][i3] == it["haystack_dates"][3]
        assert out["haystack_sessions"][i3] == it["haystack_sessions"][3]
        assert out["variant"] == f"position={pos}"
        assert lv.evidence_third(out) == pos


def test_scale_down_drops_only_non_evidence_sessions():
    it = _item(n=12, evidence=("s1", "s10"))
    out = lv.apply_variant(it, scale=0.5, rng=random.Random(1))
    ids = out["haystack_session_ids"]
    assert len(ids) == 6 and "s1" in ids and "s10" in ids
    assert ids.index("s1") < ids.index("s10")  # relative order kept
    assert out["variant"] == "scale=0.5"


def test_scale_keeps_every_survivor_in_its_original_order():
    it = _item(n=12, evidence=("s5",))
    down = lv.apply_variant(it, scale=0.5, rng=random.Random(1))["haystack_session_ids"]
    assert down == sorted(down, key=lambda s: int(s[1:]))  # not evidence-first
    assert down[0] != "s5"
    b = _item(qid="b", evidence=("s0",))
    up = lv.apply_variant(it, scale=1.5, pool=lv.distractor_pool([it, b]), rng=random.Random(0))
    ids = up["haystack_session_ids"]
    assert ids[:12] == it["haystack_session_ids"]  # originals first, in place
    assert all(x.startswith("pad:b:") for x in ids[12:])


def test_scale_up_without_donors_is_not_labelled_scaled():
    it = _item()
    out = lv.apply_variant(it, scale=2.0, pool=lv.distractor_pool([it]))
    assert out["variant"] == "" and out["haystack_session_ids"] == it["haystack_session_ids"]


def test_scale_up_pads_with_other_items_prefixed_sessions():
    a, b = _item(qid="a"), _item(qid="b", evidence=("s2",))
    pool = lv.distractor_pool([a, b])
    out = lv.apply_variant(a, scale=2.0, pool=pool, rng=random.Random(0))
    ids = out["haystack_session_ids"]
    # b has 9 sessions, one of them evidence → 8 donors; the pool is exhausted
    # before the 2× target, and the shortfall is reported, never fabricated.
    padded = [x for x in ids if str(x).startswith("pad:")]
    assert len(ids) == 17 and len(padded) == 8
    assert all(x.startswith("pad:b:") for x in padded)
    assert "pad:b:s2" not in ids  # a donor's evidence is never used as padding
    assert set(a["haystack_session_ids"]) <= set(ids)


def test_scale_and_position_compose():
    a, b = _item(qid="a"), _item(qid="b")
    out = lv.apply_variant(
        a, position="early", scale=1.5, pool=lv.distractor_pool([a, b]), rng=random.Random(3)
    )
    ids = out["haystack_session_ids"]
    assert ids[:2] == ["s3", "s7"] and len(ids) == 14
    assert out["variant"] == "scale=1.5,position=early"


def test_harness_exposes_the_flags_and_the_column():
    src = (ROOT / "bench" / "longmemeval.py").read_text()
    assert "--evidence-position" in src and "--context-scale" in src
    assert 'variant=str(item.get("variant", "")' in src
    # The header list names the column (the `repeat_*` columns follow it, so
    # it is not the last one).
    assert '"variant",\n' in src and "r.variant," in src
