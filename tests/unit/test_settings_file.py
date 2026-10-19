"""The YAML settings file: below the dotenv file and the environment, above the
defaults, and read strictly (an unknown name, a secret or another version refuses)."""

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
        ("version: 1\nsettings:\n  memory_ask_recency_wieght: 0.4\n", "memory_ask_recency_wieght"),
        ("version: 1\nsettings:\n  jwt_secret: x\n", "jwt_secret is a secret"),
        ("version: 2\nsettings: {}\n", "version 2 is not supported"),
        ("settings: {}\n", "version None is not supported"),
        ("version: 1\nsetings: {}\n", "unknown top-level keys ['setings']"),
        ("version: 1\nsettings: [a]\n", "`settings` must be a mapping"),
        ("- 1\n", "expected a mapping"),
        ("version: 1\nsettings: {a: [\n", "not readable YAML"),
    ],
)
def test_a_file_the_build_cannot_use_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str, names: str
) -> None:
    with pytest.raises(SettingsFileError) as e:
        _load(monkeypatch, tmp_path, text)
    assert names in str(e.value)


def test_a_wrong_type_names_the_field_never_the_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(ValueError) as e:
        _load(
            monkeypatch,
            tmp_path,
            "version: 1\nsettings:\n  memory_ask_synth_max_tokens: lots-of-them\n",
        )
    assert "memory_ask_synth_max_tokens" in str(e.value) and "lots-of-them" not in str(e.value)


def test_a_named_file_that_does_not_exist_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOOM_SETTINGS_FILE", str(tmp_path / "missing.yaml"))
    with pytest.raises(SettingsFileError, match="does not exist"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_empty_reads_no_file_and_an_absent_default_is_fine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "loom.yaml").write_text("version: 1\nsettings:\n  memory_ask_recency_weight: 0.9\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOOM_MEMORY_ASK_RECENCY_WEIGHT", raising=False)
    monkeypatch.setenv("LOOM_SETTINGS_FILE", "")
    assert Settings(_env_file=None).memory_ask_recency_weight == 0.2  # type: ignore[call-arg]
    monkeypatch.delenv("LOOM_SETTINGS_FILE")
    assert Settings(_env_file=None).memory_ask_recency_weight == 0.9  # type: ignore[call-arg]
    monkeypatch.chdir(ROOT)  # no loom.yaml here
    assert Settings(_env_file=None).memory_ask_recency_weight == 0.2  # type: ignore[call-arg]


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
