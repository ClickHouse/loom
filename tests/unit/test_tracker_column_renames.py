"""A tracker source's `columns` config maps contract columns to its own names."""

from __future__ import annotations

import json

import pytest

from server.tracker.domain import sources


def _with_source(monkeypatch: pytest.MonkeyPatch, columns: object) -> None:
    cfg = {"sources": {"t": {"host": "h.example", "database": "db", "columns": columns}}}
    monkeypatch.setattr(sources.settings, "tracker_sources_json", json.dumps(cfg))


def test_renames_parse_into_the_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_source(monkeypatch, {"github_issues": {"triage_component": "component_guess"}})
    assert sources.column_renames("t") == {"github_issues": {"triage_component": "component_guess"}}


def test_a_rename_that_is_not_an_identifier_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_source(monkeypatch, {"github_issues": {"triage_component": "x; DROP TABLE y"}})
    assert sources.column_renames("t") == {}


def test_no_columns_key_means_no_renames(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_source(monkeypatch, None)
    assert sources.column_renames("t") == {}
