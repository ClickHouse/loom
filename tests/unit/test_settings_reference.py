"""docs/reference/configuration.md is generated from the settings mixins and must be current."""

from __future__ import annotations

from pathlib import Path

from scripts.codegen import settings_reference as gen
from server.settings import Settings
from server.settings._env import is_secret_field

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = ROOT / "docs" / "reference" / "configuration.md"


def test_configuration_reference_is_current() -> None:
    assert REFERENCE.read_text(encoding="utf-8") == gen.render(), (
        "docs/reference/configuration.md is stale: run python scripts/codegen/settings_reference.py"
    )


def test_every_setting_has_a_row() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    missing = [f for f in Settings.model_fields if f"`{gen._env_name(f)}`" not in text]
    assert not missing, missing


def test_the_reference_hides_exactly_the_fields_repr_masks() -> None:
    """One secret rule: a default is hidden in the reference iff repr masks the field."""
    for field in Settings.model_fields:
        hidden = gen._default(field) in ("*(set)*", "*(unset)*")
        assert hidden == is_secret_field(field), field


def test_a_token_budget_is_not_a_secret() -> None:
    assert gen._default("code_ask_token_budget") == "`4000`"
    assert is_secret_field("code_github_token")
