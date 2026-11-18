"""The final table row in a bounded request still carries its source contract."""

import pytest

from scripts.i18n import translate as t
from scripts.i18n.context import SourceContext

PATH = "docs/reference/operations/calibration.md"
HEADER = "| Field | Type | Meaning |\n| --- | --- | --- |\n"
ROW = '| `source` | `str` | "namespace" / "cluster" / "scalar" |\n'


def _planned(newline):
    requests = t.english_requests((HEADER + ROW * 600).replace("\n", newline))
    assert len(requests) == 3
    if newline == "\n":
        assert [len(p.encode()) for p in requests] == [16344, 16355, 2146]
    return requests


def _change_last(text):
    prefix, suffix = text.rsplit('"namespace"', 1)
    return prefix + '"命名空间"' + suffix


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["LF", "CRLF"])
@pytest.mark.parametrize("index", [0, 1, 2], ids=["first", "middle", "final"])
def test_planned_request_final_row_and_neighbor_keep_wire_literals(newline, index):
    requests = _planned(newline)
    english = "\n".join(requests)
    start = sum(len(p) + 1 for p in requests[:index])
    source = requests[index]
    scope = SourceContext(PATH, english, start, start + len(source))
    assert english[scope.start : scope.end] == source
    assert t.differences(source, source, source_context=scope) == []
    assert any(
        "protected source strings" in p
        for p in t.differences(source, _change_last(source), source_context=scope)
    )
    previous, final = source.rsplit('"namespace"', 1)
    neighbor = _change_last(previous) + '"namespace"' + final
    assert any(
        "protected source strings" in p
        for p in t.differences(source, neighbor, source_context=scope)
    )


def test_split_row_refuses_the_bad_reply_and_asks_for_repair(monkeypatch):
    requests = _planned("\n")
    source = requests[0]
    english = "\n".join(requests)
    bad = _change_last(source)
    scope = SourceContext(PATH, english, 0, len(source))
    replies = iter([bad, source])
    sent = []

    def reply(client, model, system, messages):
        sent.append(list(messages))
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": next(replies)}]}

    monkeypatch.setattr(t, "call", reply)
    assert (
        t.translate_text(
            source, t.LANGUAGES[0], None, t.Spend(t.DEFAULT_MODEL, 1), source_context=scope
        )
        == source
    )
    assert len(sent) == 2
    assert "protected source strings" in sent[1][-1]["content"]
    assembled = "\n".join([bad, *requests[1:]])
    assert any("('cell', 281, 2)" in p for p in t.differences(english, assembled, source_path=PATH))
