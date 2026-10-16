"""The issue forms parse, and the fields `loom report --open` prefills exist."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from server.cli.doctor import BUG_FORM

FORMS = Path(__file__).resolve().parents[2] / ".github" / "ISSUE_TEMPLATE"
FIELD_TYPES = {"markdown", "textarea", "input", "dropdown", "checkboxes"}


def _forms() -> list[Path]:
    return sorted(p for p in FORMS.glob("*.y*ml") if p.name != "config.yml")


@pytest.mark.parametrize("path", _forms(), ids=lambda p: p.name)
def test_form_is_valid(path: Path) -> None:
    form = yaml.safe_load(path.read_text())
    assert form["name"] and form["description"] and form["body"]
    ids = [f["id"] for f in form["body"] if "id" in f]
    assert len(ids) == len(set(ids)), f"duplicate field id in {path.name}"
    assert all(re.fullmatch(r"[a-z][a-z0-9-]*", i) for i in ids), ids
    for field in form["body"]:
        assert field["type"] in FIELD_TYPES
        assert field["type"] != "markdown" or "id" not in field
        if field["type"] != "markdown":
            assert field["attributes"]["label"]
        if field["type"] == "dropdown":
            assert all(isinstance(o, str) for o in field["attributes"]["options"])


def test_report_prefills_an_existing_field() -> None:
    form = yaml.safe_load((FORMS / BUG_FORM).read_text())
    ids = {f.get("id") for f in form["body"]}
    assert "environment" in ids


def test_blank_issues_off_and_security_linked() -> None:
    config = yaml.safe_load((FORMS / "config.yml").read_text())
    assert config["blank_issues_enabled"] is False
    assert any("security" in link["url"] for link in config["contact_links"])
