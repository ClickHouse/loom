"""memory.list caps every value shape at the preview size.

A cap that only cut string values would let 100 JSON-object memories come
back as megabytes per page. The Memories view refetches the page every 8 s
and shows 140 characters of each value, so anything past the preview is waste."""

import json

from server.memory.app.query import _truncate_value_for_list

CAP = 2048


def test_short_values_pass_through_unchanged() -> None:
    assert _truncate_value_for_list("hello", CAP) == "hello"
    assert _truncate_value_for_list(json.dumps({"a": 1}), CAP) == {"a": 1}
    assert _truncate_value_for_list(json.dumps([1, 2, 3]), CAP) == [1, 2, 3]
    assert _truncate_value_for_list(json.dumps(True), CAP) is True
    assert _truncate_value_for_list(None, CAP) is None


def test_long_string_is_cut_with_an_ellipsis() -> None:
    out = _truncate_value_for_list("x" * (CAP + 10), CAP)
    assert isinstance(out, str)
    assert len(out) == CAP + 1 and out.endswith("…")


def test_large_object_comes_back_as_a_cut_json_preview() -> None:
    big = {"alert": "y" * (CAP * 20), "severity": "high"}
    raw = json.dumps(big)
    out = _truncate_value_for_list(raw, CAP)
    assert isinstance(out, str), "an oversize object must not ship whole"
    assert len(out) == CAP + 1 and out.endswith("…")
    assert out.startswith('{"alert": "yyy')


def test_large_list_and_already_decoded_object_are_capped_too() -> None:
    out = _truncate_value_for_list(json.dumps(["z" * 100] * 100), CAP)
    assert isinstance(out, str) and len(out) == CAP + 1
    # a row whose value arrived already parsed (no raw text to reuse)
    out2 = _truncate_value_for_list({"k": "w" * (CAP * 2)}, CAP)
    assert isinstance(out2, str) and len(out2) == CAP + 1


def test_page_of_large_objects_stays_under_the_budget() -> None:
    rows = [json.dumps({"i": i, "body": "b" * 100_000}) for i in range(100)]
    total = sum(len(json.dumps(_truncate_value_for_list(r, CAP))) for r in rows)
    assert total < 100 * (CAP + 64)
