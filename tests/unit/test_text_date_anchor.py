"""The day a fact's text names is the day it holds."""

from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.dates import align_anchor_to_text_date


def _f(value, valid_at=None, anchor=None, conf=0.0):
    return ExtractedFact(
        memory_key="k",
        value=value,
        valid_at=valid_at,
        temporal_anchor=anchor,
        temporal_confidence=conf,
    )


def test_a_restatement_dated_by_its_session_takes_the_day_its_text_names() -> None:
    f = _f(
        "User recently attended a music festival in Brooklyn (around 2023-03-01).",
        valid_at="2023-04-15T00:00:00",
        anchor="2023-04-15T00:00:00",
        conf=0.5,
    )
    out = align_anchor_to_text_date([f])[0]
    assert out.valid_at == "2023-03-01T00:00:00" and out.temporal_anchor == "2023-03-01T00:00:00"


def test_agreeing_pinned_and_noteless_facts_pass_through() -> None:
    facts = [
        _f("User fixed the flat (on 2023-03-15).", valid_at="2023-03-16T00:00:00"),
        _f("User moved (around 2023-01-05).", valid_at="2023-05-01T00:00:00", conf=1.0),
        _f("User likes tea.", valid_at="2023-05-01T00:00:00"),
        _f("User ran a 5K (three weeks ago, around 2023-06-10).", valid_at="2023-07-01T00:00:00"),
    ]
    out = align_anchor_to_text_date(facts)
    assert [f.valid_at[:10] for f in out] == [
        "2023-03-16",
        "2023-05-01",
        "2023-05-01",
        "2023-06-10",
    ]
    assert align_anchor_to_text_date([]) == []


def test_a_restatement_keeps_the_first_event_date(monkeypatch) -> None:
    from datetime import datetime
    from types import SimpleNamespace

    from server.memory.app.bulk_verdicts import _keep_first_event_date

    row = {
        "memory_key": "user_music_festival",
        "valid_at": "2023-04-15T00:00:00",
        "temporal_anchor": datetime(2023, 4, 15),
        "temporal_confidence": 0.5,
    }
    erow = {"memory_key": "user_music_festival", "valid_at": "2023-04-15T00:00:00"}
    peers = [SimpleNamespace(memory_id="p1", valid_at=datetime(2023, 3, 1))]
    assert _keep_first_event_date(row, [erow], [0], 0, peers)
    assert row["valid_at"] == "2023-03-01T00:00:00" and row["temporal_anchor"] == datetime(
        2023, 3, 1
    )
    assert erow["valid_at"] == "2023-03-01T00:00:00"
    # the insert path carries datetimes: the type is preserved, never a string
    drow = {
        "memory_key": "k",
        "valid_at": datetime(2023, 4, 15),
        "temporal_anchor": datetime(2023, 4, 15),
        "temporal_confidence": 0.0,
    }
    derow = {"memory_key": "k", "valid_at": datetime(2023, 4, 15)}
    assert _keep_first_event_date(drow, [derow], [0], 0, peers)
    assert drow["valid_at"] == datetime(2023, 3, 1) and isinstance(drow["valid_at"], datetime)
    assert derow["valid_at"] == datetime(2023, 3, 1) and isinstance(derow["valid_at"], datetime)
    pinned = {"memory_key": "k", "valid_at": "2023-04-15T00:00:00", "temporal_confidence": 1.0}
    assert (
        not _keep_first_event_date(pinned, [], [], 0, peers)
        and pinned["valid_at"] == "2023-04-15T00:00:00"
    )
    near = {"memory_key": "k", "valid_at": "2023-03-02T00:00:00", "temporal_confidence": 0.0}
    assert not _keep_first_event_date(near, [], [], 0, peers)
    assert not _keep_first_event_date(
        {"memory_key": "k", "valid_at": "2023-04-15T00:00:00"},
        [],
        [],
        0,
        [SimpleNamespace(memory_id="p", valid_at=None)],
    )
