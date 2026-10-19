"""The YAML settings file LOOM_SETTINGS_FILE names: below the dotenv file and the
environment, above the defaults. A secret, a duplicate key, another version or a value
of the wrong type or outside its bounds refuses; a name no setting has is logged and
ignored (N-1 and N share the file in a rollout)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.codegen import settings_schema
from server.settings import Settings
from server.settings._env import is_secret_field
from server.settings._file import SettingsFileError

ROOT = Path(__file__).resolve().parents[2]


def _load(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, *, env_file: str = ""
) -> Settings:
    path = tmp_path / "loom.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(path))
    return Settings(_env_file=env_file or None)  # type: ignore[call-arg]


def test_the_file_sets_a_value_and_the_dotenv_file_and_environment_win(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (
        "LOOM_MEMORY_ASK_RECENCY_WEIGHT",
        "LOOM_MEMORY_ASK_SYNTH_BUDGET_RATIO",
        "LOOM_CH_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("LOOM_MEMORY_ASK_SYNTH_BUDGET_RATIO=0.5\nLOOM_CH_HOST=from-dotenv\n")
    monkeypatch.setenv("LOOM_CH_HOST", "from-env")
    s = _load(
        monkeypatch,
        tmp_path,
        "version: 1\nsettings:\n  memory_ask_recency_weight: 0.4\n"
        "  memory_ask_synth_budget_ratio: 0.9\n  ch_host: from-file\n",
        env_file=str(dotenv),
    )
    assert s.memory_ask_recency_weight == 0.4  # the file over the default
    assert s.memory_ask_synth_budget_ratio == 0.5  # the dotenv file over the file
    assert s.ch_host == "from-env"  # the environment over both


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("version: 1\nsettings:\n  jwt_secret: x\n", "jwt_secret is a secret"),
        ("version: 2\nsettings: {}\n", "version 2 is not supported"),
        ("settings: {}\n", "version None is not supported"),
        ("version: 1\nsetings: {}\n", "unknown top-level keys ['setings']"),
        ("version: 1\nsettings: [a]\n", "`settings` must be a mapping"),
        ("version: 1\nsettings: false\n", "`settings` must be a mapping"),
        ("version: 1\nsettings: []\n", "`settings` must be a mapping"),
        ("version: 1\nsettings: 0\n", "`settings` must be a mapping"),
        ("version: 1\nsettings: ''\n", "`settings` must be a mapping"),
        ("- 1\n", "expected a mapping"),
        ("version: 1\nsettings: {a: [\n", "not readable YAML"),
        ("version: true\nsettings: {}\n", "version True is not supported"),
        ("version: 1\nsettings:\n  ? [a]\n  : 1\n", "a key must be a name"),
        (
            "version: 1\nsettings:\n  memory_ask_recency_weight: 0.4\n"
            "  memory_ask_recency_weight: 0.5\n",
            "'memory_ask_recency_weight' is given twice",
        ),
    ],
)
def test_a_file_the_build_cannot_use_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, names: str
) -> None:
    with pytest.raises(SettingsFileError) as e:
        _load(monkeypatch, tmp_path, text)
    assert names in str(e.value)


@pytest.mark.parametrize(
    "text", ["version: 1\n", "version: 1\nsettings:\n", "version: 1\nsettings: {}\n"]
)
def test_a_file_with_no_settings_sets_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str
) -> None:
    assert _load(monkeypatch, tmp_path, text).loaded_from("settings_file") == frozenset()


@pytest.mark.parametrize("shadowed", [False, True])
def test_a_wrong_type_names_the_field_and_the_rule_never_the_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shadowed: bool
) -> None:
    """Also when the environment sets the field: removing the variable later must not
    be what stops a start."""
    if shadowed:
        monkeypatch.setenv("LOOM_MEMORY_ASK_SYNTH_MAX_TOKENS", "900")
    with pytest.raises(SettingsFileError) as e:
        _load(
            monkeypatch,
            tmp_path,
            "version: 1\nsettings:\n  memory_ask_synth_max_tokens: lots-of-them\n",
        )
    assert "memory_ask_synth_max_tokens" in str(e.value) and "lots-of-them" not in str(e.value)
    assert "valid integer" in str(e.value)


def test_an_unquoted_off_says_what_the_setting_takes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(SettingsFileError) as e:
        _load(monkeypatch, tmp_path, "version: 1\nsettings:\n  memory_search_key_axis: off\n")
    assert "'auto', 'on' or 'off'" in str(e.value)


def test_a_named_file_that_does_not_exist_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(tmp_path / "missing.yaml"))
    with pytest.raises(SettingsFileError, match="does not exist"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_only_a_named_file_is_read(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A `loom.yaml` where the CLI happens to run is not the server's.
    (tmp_path / "loom.yaml").write_text("version: 1\nsettings:\n  memory_ask_recency_weight: 0.9\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOOM_MEMORY_ASK_RECENCY_WEIGHT", raising=False)
    monkeypatch.delenv("LOOM_SETTINGS_FILE", raising=False)
    assert Settings(_env_file=None).memory_ask_recency_weight == 0.2  # type: ignore[call-arg]
    monkeypatch.setenv("LOOM_SETTINGS_FILE", "")
    assert Settings(_env_file=None).memory_ask_recency_weight == 0.2  # type: ignore[call-arg]
    monkeypatch.setenv("LOOM_SETTINGS_FILE", "loom.yaml")
    assert Settings(_env_file=None).memory_ask_recency_weight == 0.9  # type: ignore[call-arg]


def test_a_name_no_setting_has_is_logged_and_the_rest_apply(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("LOOM_MEMORY_ASK_RECENCY_WEIGHT", raising=False)
    s = _load(
        monkeypatch,
        tmp_path,
        "version: 1\nsettings:\n  a_setting_of_a_newer_build: 1\n  memory_ask_recency_weight: 0.4\n",
    )
    assert s.memory_ask_recency_weight == 0.4
    assert "no setting is named a_setting_of_a_newer_build" in caplog.text


def test_the_example_file_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(ROOT / "loom.example.yaml"))
    Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_schema_is_current() -> None:
    assert settings_schema.TARGET.is_current(settings_schema.render()), (
        f"stale: run {settings_schema.COMMAND}"
    )


def test_the_schema_holds_every_setting_but_the_secrets() -> None:
    schema = json.loads((ROOT / "schemas" / "loom-settings.schema.json").read_text())
    props = schema["properties"]["settings"]["properties"]
    assert set(props) == {f for f in Settings.model_fields if not is_secret_field(f)}
    assert schema["properties"]["settings"]["additionalProperties"] is False
    assert props["memory_ask_recency_weight"]["description"].startswith("memory.ask only")


def test_a_yaml_merge_key_reads_as_yaml_does(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    s = _load(
        monkeypatch,
        tmp_path,
        "version: 1\nsettings:\n  <<: {memory_ask_recency_weight: 0.3, memory_ask_synth_budget_ratio: 0.4}\n"
        "  memory_ask_recency_weight: 0.5\n",
    )
    assert (s.memory_ask_recency_weight, s.memory_ask_synth_budget_ratio) == (0.5, 0.4)


@pytest.mark.parametrize(
    ("text", "weight"),
    [
        # An alias: the mapping's own key overrides the merged one.
        (
            "version: 1\nsettings:\n  x_base: &b {memory_ask_recency_weight: 0.3}\n"
            "  <<: *b\n  memory_ask_recency_weight: 0.5\n",
            0.5,
        ),
        # A list of sources: the first wins, and a key both share is not "given twice".
        (
            "version: 1\nsettings:\n  x_a: &a {memory_ask_recency_weight: 0.3}\n"
            "  x_b: &b {memory_ask_recency_weight: 0.4}\n  <<: [*a, *b]\n",
            0.3,
        ),
        # A merged anchor with its own merge and override.
        (
            "version: 1\nsettings:\n  x_base: &b\n    <<: {memory_ask_recency_weight: 0.3}\n"
            "    memory_ask_recency_weight: 0.4\n  <<: *b\n",
            0.4,
        ),
    ],
)
def test_merges_read_as_yaml_reads_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, weight: float
) -> None:
    assert _load(monkeypatch, tmp_path, text).memory_ask_recency_weight == weight


def test_a_bare_value_key_is_an_unknown_name_not_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert (
        _load(monkeypatch, tmp_path, "version: 1\nsettings:\n  =: 1\n").loaded_from("settings_file")
        == frozenset()
    )
