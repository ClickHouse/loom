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


def test_the_reader_user_can_only_select() -> None:
    """Caller-shaped reads run as loom_reader: no access management, read-only
    profile, and grants of SELECT alone."""
    root = ET.parse(CFG / "nlp_user_settings.xml").getroot()
    reader = root.find("users/loom_reader")
    assert reader is not None
    assert reader.find("access_management") is None
    assert reader.find("named_collection_control") is None
    assert reader.find("password").get("from_env") == "CLICKHOUSE_PASSWORD"
    assert root.findtext(f"profiles/{reader.findtext('profile')}/readonly") == "2"
    assert reader.findtext("quota") == "loom_tenant"
    grants = [q.text for q in reader.findall("grants/query")]
    assert grants and all(g.startswith("GRANT SELECT ON ") for g in grants)
    assert not any(" system." in g for g in grants)  # system.query_log holds every tenant's SQL


@pytest.mark.parametrize(
    ("ch_setting", "field"),
    [
        ("max_execution_time", "ch_caller_shaped_max_execution_s"),
        ("max_memory_usage", "ch_caller_shaped_max_memory_bytes"),
        ("max_rows_to_read", "ch_caller_shaped_max_rows_to_read"),
    ],
)
def test_the_reader_profile_caps_equal_the_caller_shaped_defaults(
    ch_setting: str, field: str
) -> None:
    """No statement may raise the reader's caps past Loom's own ceilings, so the
    two must not drift apart."""
    from server.settings import Settings

    root = ET.parse(CFG / "nlp_user_settings.xml").getroot()
    profile = root.find("profiles/loom_reader")
    assert profile is not None
    default = Settings.model_fields[field].default
    assert float(profile.findtext(f"constraints/{ch_setting}/max") or "nan") == default
    assert float(profile.findtext(ch_setting) or "nan") == default
