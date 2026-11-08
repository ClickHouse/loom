"""The assistant's LibreChat is configured the same in Compose and in the chart: the
chart's ConfigMap is integrations/librechat/assistant.librechat.yaml with the release's
Loom Service in place of the compose service. CH-free."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_CONFIG = ROOT / "integrations" / "librechat" / "assistant.librechat.yaml"
CHART = ROOT / "deploy" / "helm" / "loom" / "templates" / "assistant.yaml"


def _chart_config() -> dict:
    text = CHART.read_text()
    block = text.split("  librechat.yaml: |\n", 1)[1].split("\n---", 1)[0]
    # The template's two substitutions: the Loom Service, and the escaped braces.
    block = (
        block.replace("{{ $loom }}", "loom").replace('{{ "{{" }}', "{{").replace('{{ "}}" }}', "}}")
    )
    return yaml.safe_load(re.sub(r"^    ", "", block, flags=re.M))


def test_the_chart_configures_librechat_as_compose_does() -> None:
    assert _chart_config() == yaml.safe_load(COMPOSE_CONFIG.read_text())


def test_the_agent_has_tools_only_and_each_answer_carries_its_own_token() -> None:
    config = yaml.safe_load(COMPOSE_CONFIG.read_text())
    assert config["endpoints"]["agents"]["capabilities"] == ["tools"]
    assert config["memory"]["disabled"] is True
    headers = config["mcpServers"]["loom"]["headers"]
    # Under headers, not requestHeaders: those are left off the tools/list connection.
    assert headers["Authorization"] == "Bearer {{LIBRECHAT_BODY_PARENTMESSAGEID}}"
    assert "requestHeaders" not in config["mcpServers"]["loom"]


def test_compose_publishes_no_assistant_port_and_keeps_mongo_internal() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    for name in ("librechat", "librechat-mongo"):
        service = compose["services"][name]
        assert service["profiles"] == ["assistant"]
        assert "ports" not in service, name
    assert compose["services"]["librechat-mongo"]["networks"] == ["assistant"]
    assert compose["networks"]["assistant"]["internal"] is True
    # LibreChat's own egress network is shared with no other service.
    for name, service in compose["services"].items():
        if name != "librechat":
            assert "assistant-egress" not in service.get("networks", []), name
