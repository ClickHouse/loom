"""The validity fragments the graph and records planes share."""

from __future__ import annotations

from server.memory.domain.temporal_sql import graph_validity_predicate, record_validity_predicate


def test_a_current_graph_read_keeps_open_and_not_yet_closed_edges() -> None:
    assert graph_validity_predicate(valid=False, known=False) == (
        "(valid_to IS NULL OR valid_to >= now())"
    )


def test_a_graph_read_at_v_is_interval_containment() -> None:
    p = graph_validity_predicate(valid=True, known=False, alias="t")
    assert "t.valid_from <= {valid_as_of:DateTime}" in p
    assert "t.valid_to >= {valid_as_of:DateTime}" in p
    assert "recorded_at" not in p


def test_a_graph_read_known_at_k_reopens_closes_recorded_later() -> None:
    p = graph_validity_predicate(valid=False, known=True)
    # With no V the world is read at K.
    assert "valid_from <= {known_as_of:DateTime}" in p
    assert "recorded_at <= {known_as_of:DateTime}" in p
    assert "closed_at > {known_as_of:DateTime}" in p


def test_a_record_holds_over_a_half_open_interval() -> None:
    p = record_validity_predicate("r")
    assert "r.valid_at <= {valid_as_of:DateTime}" in p
    # A record superseded at T and its successor never both hold at T.
    assert "r.valid_to > {valid_as_of:DateTime}" in p and ">=" not in p


def test_a_graph_knowledge_time_is_bound_in_utc_whatever_its_offset() -> None:
    from datetime import UTC, datetime, timedelta, timezone
    from types import SimpleNamespace

    from server.memory.app.facts import _graph_validity

    utc = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    ist = utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
    bound = []
    for k in (utc, ist):
        params: dict = {}
        _graph_validity(SimpleNamespace(known_as_of=k, valid_as_of=k), params)
        bound.append(params)
    assert (
        bound[0]
        == bound[1]
        == {
            "known_as_of": datetime(2026, 1, 1, 6, 0),
            "valid_as_of": utc,
        }
    )
