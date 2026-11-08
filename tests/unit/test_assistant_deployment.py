"""The assistant's LibreChat is configured the same in Compose and in the chart: the
chart's ConfigMap is deploy/librechat/assistant.librechat.yaml with the release's
Loom Service in place of the compose service. CH-free."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_CONFIG = ROOT / "deploy" / "librechat" / "assistant.librechat.yaml"
CHART = ROOT / "deploy" / "helm" / "loom" / "templates" / "assistant-config.yaml"


def _chart_config() -> dict:
    text = CHART.read_text()
    block = text.split("  librechat.yaml: |\n", 1)[1].split("\n{{- end }}", 1)[0]
    # The template's substitutions: the Loom Service and its port, and the escaped braces.
    block = (
        block.replace("{{ $loom }}:{{ $.Values.service.port }}", "loom:7777")
        .replace('{{ "{{" }}', "{{")
        .replace('{{ "}}" }}', "}}")
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
    assert compose["services"]["librechat-mongo"]["networks"] == ["assistant-db"]
    assert compose["networks"]["assistant"]["internal"] is True
    assert compose["networks"]["assistant-db"]["internal"] is True
    # LibreChat's database and egress networks are shared with no other service, so
    # the loom container reaches neither MongoDB nor LibreChat's way out.
    for name, service in compose["services"].items():
        joined = service.get("networks", [])
        if name not in ("librechat", "librechat-mongo"):
            assert "assistant-db" not in joined, name
        if name != "librechat":
            assert "assistant-egress" not in joined, name


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm (the CI helm job runs it)")
def test_the_charts_assistant_is_reached_only_from_loom_and_reaches_loom() -> None:
    from scripts.gates import helm_assistant_isolation

    helm_assistant_isolation.main()


def test_librechats_compose_variables_are_names_loom_does_not_read() -> None:
    # The loom service loads all of .env: a LibreChat secret or key named as Loom names
    # one (ANTHROPIC_API_KEY, LOOM_*) would become Loom's own.
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    values = compose["services"]["librechat"]["environment"].values()
    named = {m for v in values for m in re.findall(r"\$\{([A-Z0-9_]+)", str(v))}
    assert named and all(n.startswith("LIBRECHAT_") for n in named), named


def test_the_guide_keeps_the_bundled_clickhouse_when_it_adds_the_assistant() -> None:
    # A --profile flag takes the place of COMPOSE_PROFILES: each command that names the
    # assistant's profile names the bundled ClickHouse's beside it.
    guide = (ROOT / "docs" / "features" / "integrations" / "librechat.md").read_text()
    commands = guide.replace("\\\n", " ").split("\n")
    flagged = [c for c in commands if "--profile assistant" in c]
    assert flagged and all("--profile bundled" in c for c in flagged), flagged
    # An installed stack's COMPOSE_PROFILES, as install.sh writes it, is extended.
    assert "COMPOSE_PROFILES=\\1,assistant/" in guide


@pytest.mark.skipif(not shutil.which("sed"), reason="needs sed")
@pytest.mark.parametrize(
    ("before", "after"),
    [("bundled", "bundled,assistant"), ("", "assistant"), ("a,b", "a,b,assistant")],
)
def test_the_guides_sed_adds_the_assistant_to_any_profiles(
    tmp_path: Path, before: str, after: str
) -> None:
    # The line as install.sh's set_env writes it: unquoted, no comment after it.
    guide = (ROOT / "docs" / "features" / "integrations" / "librechat.md").read_text()
    (expression,) = re.findall(r"sed -i\.bak '([^']+)' \.env", guide)
    env = tmp_path / ".env"
    env.write_text(f"A=1\nCOMPOSE_PROFILES={before}\n")
    subprocess.run(["sed", "-i.bak", expression, str(env)], check=True)
    assert f"COMPOSE_PROFILES={after}\n" in env.read_text()
