"""Every LLM call's output cap is one entry of LLM_MAX_TOKENS in server/settings/catalog.py,
read through `llm_max_tokens`, and a deployment changes one while Loom runs."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from server.core import settings_overrides as so
from server.settings import Settings, settings
from server.settings._live import coerce
from server.settings.budgets import llm_max_tokens
from server.settings.catalog import LLM_MAX_TOKENS

ROOT = Path(__file__).resolve().parents[2]


def _calls() -> tuple[list[str], set[str]]:
    """(the literal max_tokens at a call site, the keys `llm_max_tokens` is called with)."""
    literals, keys = [], set()
    for path in sorted((ROOT / "server").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        constants = {
            t.id
            for st in tree.body
            if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant)
            for t in st.targets
            if isinstance(t, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                literal = isinstance(kw.value, ast.Constant) or (
                    isinstance(kw.value, ast.Name) and kw.value.id in constants
                )
                if kw.arg == "max_tokens" and literal:
                    literals.append(f"{path.relative_to(ROOT)}:{node.lineno}")
            if ast.unparse(node.func) == "llm_max_tokens" and node.args:
                arg = node.args[0]
                assert isinstance(arg, ast.Constant), f"{path}:{node.lineno}: a literal key"
                keys.add(arg.value)
    return literals, keys


def test_no_call_site_caps_its_output_with_a_literal() -> None:
    literals, _ = _calls()
    assert literals == [], "read the cap with llm_max_tokens(<call>) from the catalog table"


def test_every_key_called_is_in_the_table_and_every_entry_is_called() -> None:
    _, keys = _calls()
    assert sorted(keys - set(LLM_MAX_TOKENS)) == [], "calls with no table entry"
    assert sorted(set(LLM_MAX_TOKENS) - keys) == [], "entries no call reads"


def test_a_deployment_override_changes_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "llm_max_tokens_overrides", '{"hyde": 64}')
    assert llm_max_tokens("hyde") == 64
    assert llm_max_tokens("ask_decompose") == LLM_MAX_TOKENS["ask_decompose"]
    monkeypatch.setattr(settings, "llm_max_tokens_overrides", "")
    assert llm_max_tokens("hyde") == LLM_MAX_TOKENS["hyde"]


@pytest.mark.parametrize("raw", ['{"hyde": -7}', '{"hyde": 0}', '{"hyde": "64"}', "[1]"])
def test_an_unusable_count_is_refused_at_startup_and_by_settings_set(raw: str) -> None:
    """The refusal names the call, never the count."""
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, llm_max_tokens_overrides=raw)  # type: ignore[call-arg]
    assert "-7" not in str(caught.value)
    with pytest.raises(ValidationError):
        coerce(Settings, "llm_max_tokens_overrides", raw)


def test_a_call_this_build_lacks_is_logged_and_ignored(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Builds N-1 and N read one settings file: a call one of them lacks must not stop it."""
    raw = '{"hyde": 64, "a_later_call": 100}'
    Settings(_env_file=None, llm_max_tokens_overrides=raw)  # type: ignore[call-arg]
    monkeypatch.setattr(settings, "llm_max_tokens_overrides", raw)
    with caplog.at_level(logging.WARNING):
        assert llm_max_tokens("hyde") == 64
    assert "a_later_call" in caplog.text


def test_a_stored_count_this_build_refuses_is_skipped_when_adopted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        so.adopt({so.KIND: {("llm_max_tokens_overrides",): '{"hyde": 0}'}})
    assert settings.llm_max_tokens_overrides == ""
    assert llm_max_tokens("hyde") == LLM_MAX_TOKENS["hyde"]
    assert any("ignored" in r.message for r in caplog.records)


def test_an_unknown_call_is_an_error_not_a_default() -> None:
    with pytest.raises(KeyError):
        llm_max_tokens("no_such_call")
