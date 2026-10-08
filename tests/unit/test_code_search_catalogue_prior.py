"""A settings catalogue is demoted in the fusion signals for
the question kinds the setting names; off at 1.0; never for other kinds."""

from __future__ import annotations

from unittest.mock import patch

from server.code.app.search.priors import (
    _CATALOGUE_FILE_RE,
    _apply_fusion_signals,
    _catalogue_penalty,
)
from server.code.domain.units import CodeUnit
from server.settings import settings


def _u(path: str, score: float) -> CodeUnit:
    return CodeUnit(unit_id=path, path=path, kind="function", score=score, body="x")


def test_catalogue_regex_names_the_catalogues_only() -> None:
    assert _CATALOGUE_FILE_RE.search("src/Core/Settings.cpp")
    assert _CATALOGUE_FILE_RE.search("src/Storages/MergeTree/MergeTreeSettings.cpp")
    assert _CATALOGUE_FILE_RE.search("src/Core/SettingsChangesHistory.cpp")
    assert not _CATALOGUE_FILE_RE.search("src/Access/SettingsProfilesCache.cpp")
    assert not _CATALOGUE_FILE_RE.search("src/Common/MemoryTracker.cpp")


def test_penalty_reorders_only_when_set() -> None:
    ranked = [_u("src/Core/Settings.cpp", 1.0), _u("src/Common/MemoryTracker.cpp", 0.9)]
    with patch.object(settings, "code_fusion_signals", True):
        same = _apply_fusion_signals(list(ranked), "where is the memory limit enforced")
        assert [u.path for u in same][0] == "src/Core/Settings.cpp"
        demoted = _apply_fusion_signals(
            list(ranked),
            "where is the memory limit enforced",
            catalogue_penalty=0.35,
        )
        assert [u.path for u in demoted][0] == "src/Common/MemoryTracker.cpp"


def test_penalty_follows_kind_and_setting() -> None:
    with patch.multiple(
        settings,
        code_search_catalogue_penalty=0.35,
        code_search_catalogue_kinds="responsibility",
    ):
        work = {"question_kind": "responsibility"}
        assert (
            _catalogue_penalty(work, prose=True) == 0.35 and work["catalogue_penalty_milli"] == 350
        )
        assert _catalogue_penalty({"question_kind": "incident"}, prose=True) == 1.0
        assert _catalogue_penalty({"question_kind": "responsibility"}, prose=False) == 1.0
    with patch.object(settings, "code_search_catalogue_penalty", 1.0):
        assert _catalogue_penalty({"question_kind": "responsibility"}, prose=True) == 1.0
