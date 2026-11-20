"""An update calculates its target without changing the running configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from server.code.domain.index import repos
from server.core import cell
from server.settings import Settings, settings
from server.settings._file import SettingsFileError


def _config(**values: object) -> Settings:
    return Settings(_env_file=None, **(settings.model_dump() | values))


def test_the_overlay_wins_over_old_environment_without_installing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from server.cli.cell_update_config import read_target

    current = _config(code_org="before", tracker_org="tracker")
    monkeypatch.setenv("LOOM_CODE_ORG", "environment")
    # A candidate validates supplied values, without rereading another settings file.
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(tmp_path / "absent.yaml"))
    path = tmp_path / "target.yaml"
    path.write_text("version: 1\nsettings:\n  code_org: after\n")
    before_env, before_settings = dict(os.environ), settings.model_dump()
    target = read_target(path, current)
    assert target.code_org == "after"
    assert target.tracker_org == "tracker"
    assert current.code_org == "before"
    assert dict(os.environ) == before_env
    assert settings.model_dump() == before_settings


@pytest.mark.parametrize(
    "body,field",
    [
        ("  code_org: one\n  code_org: two\n", "code_org"),
        ("  jwt_secret: do-not-copy-this-value\n", "jwt_secret"),
        ("  memory_ask_deep_evidence_max_memories: -1\n", "memory_ask_deep_evidence_max_memories"),
    ],
)
def test_invalid_overlay_fields_refuse_without_echoing_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str, field: str
) -> None:
    from server.cli.cell_update_config import read_target

    current = _config()
    monkeypatch.setenv("LOOM_MEMORY_ASK_DEEP_EVIDENCE_MAX_MEMORIES", "5")
    path = tmp_path / "target.yaml"
    path.write_text("version: 1\nsettings:\n" + body)
    with pytest.raises(SettingsFileError) as exc:
        read_target(path, current)
    if field == "memory_ask_deep_evidence_max_memories":
        assert field in str(exc.value)
    else:
        assert "invalid target settings file" in str(exc.value)
    assert "do-not-copy-this-value" not in str(exc.value)


def test_full_model_rules_apply_and_unknown_settings_keep_the_normal_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from server.cli.cell_update_config import read_target

    current = _config()
    path = tmp_path / "target.yaml"
    path.write_text("version: 1\nsettings:\n  future_cell_field: ignored\n  tracker_org: next\n")
    assert read_target(path, current).tracker_org == "next"
    assert "future_cell_field" in caplog.text
    path.write_text(
        "version: 1\nsettings:\n  inference_token_audience: own-audience\n  public_base_url: ''\n"
    )
    with pytest.raises(SettingsFileError, match="invalid target configuration"):
        read_target(path, current)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", {"scalar"}),
        ("not json", {"scalar"}),
        ("[]", {"scalar"}),
        ('[{"namespace":"code-only"}]', {"scalar"}),
        (
            '[{"namespace":"first","org":"one","repo":"a/b"},'
            '{"namespace":"first","org":"duplicate"},'
            '{"namespace":"bad.name","org":"skipped"},'
            '{"namespace":"second","org":"two"}]',
            {"one", "two"},
        ),
    ],
)
def test_explicit_code_parser_keeps_process_semantics_without_registry_or_gauge_changes(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: set[str]
) -> None:
    # The parser retains its existing defensive fallback for a corrupt runtime value.
    config = _config(code_org="scalar", code_repo="a/b", code_namespace="code").model_copy(
        update={"code_repos_json": raw}
    )
    for key in ("code_org", "code_repo", "code_namespace", "code_repos_json"):
        monkeypatch.setattr(settings, key, getattr(config, key))
    monkeypatch.setattr(repos, "_config_errors", {})
    monkeypatch.setattr(repos, "_config_errors_logged", set())
    monkeypatch.setattr(repos, "_config_evaluated", False)
    process_result = repos.operator_orgs()
    errors = dict(repos._config_errors)
    logged = set(repos._config_errors_logged)

    def no_registry() -> object:
        raise AssertionError("candidate read a member-managed registry")

    monkeypatch.setattr(repos, "registry_repos", no_registry)
    assert repos.operator_orgs_for(config) == process_result == expected
    assert repos._config_errors == errors
    assert repos._config_errors_logged == logged


def test_explicit_fingerprint_is_identical_to_the_process_and_uses_candidate_orgs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = _config(code_org="before", tracker_org="tracker")
    for name in Settings.model_fields:
        monkeypatch.setattr(settings, name, getattr(current, name))
    monkeypatch.setattr(cell, "_code_orgs", lambda: {"extra"})
    fields = cell.fingerprint_for(current, "deployment", {"extra"})
    assert json.dumps(fields, sort_keys=True) == json.dumps(
        cell.fingerprint("deployment"), sort_keys=True
    )
    target = _config(code_org="after", tracker_org="tracker")
    changed = cell.fingerprint_for(target, "deployment", {"target-repo"})
    assert "after" in changed["configured_orgs"]
    assert "target-repo" in changed["configured_orgs"]
    assert "before" not in changed["configured_orgs"]
    assert cell.fingerprint("deployment") == fields


@pytest.mark.parametrize(
    "template",
    [
        "version: 1\nsettings:\n  openai_base_url: {marker}\n",
        "version: 1\nsettings:\n  tracker_org: !!int {marker}\n",
        "version: 1\nsettings:\n  tracker_org: !!float {marker}\n",
        "version: 1\nsettings:\n  tracker_org: !!bool {marker}\n",
        "version: 1\nsettings:\n  tracker_org: !!timestamp {marker}\n",
        "version: {marker}\nsettings: {{}}\n",
        "version: 1\nsettings:\n  jwt_secret: [\n  {marker}\n",
        "version: 1\nsettings:\n  memory_ask_deep_evidence_max_memories: {marker}\n",
        "version: 1\nsettings:\n  {marker}: first\n  {marker}: second\n",
        "version: 1\nsettings:\n  tracker_org: 'next'\n{marker}: extra\n",
    ],
)
def test_invalid_target_errors_never_echo_supplied_values(tmp_path: Path, template: str) -> None:
    from server.cli.cell_update_config import read_target

    marker = "private-overlay-regression-marker"
    path = tmp_path / "target.yaml"
    path.write_text(template.format(marker=marker))
    with pytest.raises(SettingsFileError) as error:
        read_target(path, _config())
    assert marker not in str(error.value)
    assert error.value.__suppress_context__
    assert "target" in str(error.value)


def test_malformed_target_encoding_is_a_safe_configuration_refusal(tmp_path: Path) -> None:
    from server.cli.cell_update_config import read_target

    path = tmp_path / "target.yaml"
    path.write_bytes(b"version: 1\nsettings:\n  tracker_org: \xff\n")
    with pytest.raises(SettingsFileError, match="invalid target settings file"):
        read_target(path, _config())
