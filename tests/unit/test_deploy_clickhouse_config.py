"""The ClickHouse config files the dev/CI stack mounts from deploy/: each one
parses, and none grants or carries more than Loom needs."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

CFG = Path(__file__).resolve().parents[2] / "deploy" / "clickhouse-config"


@pytest.mark.parametrize("path", sorted(CFG.glob("*.xml*")), ids=lambda p: p.name)
def test_every_config_file_parses(path: Path) -> None:
    assert ET.parse(path).getroot().tag == "clickhouse"


def test_the_loom_user_resolves_named_collections_without_reading_secrets() -> None:
    loom = ET.parse(CFG / "nlp_user_settings.xml").getroot().find("users/loom")
    assert loom is not None
    assert loom.findtext("named_collection_control") == "1"
    assert loom.find("show_named_collections_secrets") is None


def test_the_named_collections_template_carries_no_key() -> None:
    """As shipped the template defines no collection; its example reads the
    key from the environment, so no copy of it ever holds a key literal."""
    path = CFG / "named_collections.xml.template"
    assert ET.parse(path).getroot().find("named_collections") is None
    text = path.read_text(encoding="utf-8")
    assert '<api_key from_env="OPENAI_API_KEY"/>' in text
    assert "<api_key>" not in text and "REPLACE_WITH" not in text
