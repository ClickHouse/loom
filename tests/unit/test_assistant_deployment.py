"""The native runtime adds no service or database and keeps retained data external."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
RETIRED_CHAT_SOURCES = (
    "deploy/librechat/assistant.librechat.yaml",
    "integrations/librechat/.env.example",
    "integrations/librechat/.gitignore",
    "integrations/librechat/README.md",
    "integrations/librechat/agent.yaml",
    "integrations/librechat/docker-compose.yml",
    "integrations/librechat/librechat.yaml",
    "integrations/librechat/qualification/cases.json",
    "integrations/librechat/qualification/fixture.json",
    "integrations/librechat/qualification/qualify.py",
)


def test_compose_needs_no_assistant_database_or_sidecar() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert set(compose["services"]) == {"loom", "clickhouse", "extractor"}
    assert compose["services"]["loom"]["networks"] == ["default", "extractor"]
    assert "LOOM_ASSISTANT_ENABLED" in compose["services"]["loom"]["environment"]
    assert set(compose["volumes"]) == {"loom-ch-data", "loom-code-repos"}
    # Removing a declaration must not become a command to remove existing data.
    guide = (ROOT / "docs/operations/assistant.md").read_text()
    assert "Do not use orphan removal or volume deletion" in guide
    assert "persistent-volume-claim retention policy" in guide


def _assert_retired_chat_sources_absent(root: Path) -> None:
    # Retained qualification data is external state, not a bundled runtime.
    for name in RETIRED_CHAT_SOURCES:
        assert not (root / name).exists()


def test_packaged_sources_contain_no_bundled_chat_runtime() -> None:
    from scripts.release.build_stack_bundle import FILES

    _assert_retired_chat_sources_absent(ROOT)
    assert all("librechat" not in name for name in FILES)
    assert "ASSISTANT_AGENT" not in (ROOT / "server/core/paths.py").read_text()


@pytest.mark.parametrize("name", [".state.json", "results.json", "results-example.json"])
def test_retained_chat_qualification_is_not_bundled_source(tmp_path, name) -> None:
    retained = tmp_path / "integrations/librechat/qualification" / name
    retained.parent.mkdir(parents=True)
    retained.write_text("retained synthetic data")
    _assert_retired_chat_sources_absent(tmp_path)
    assert retained.read_text() == "retained synthetic data"


@pytest.mark.parametrize("name", RETIRED_CHAT_SOURCES)
def test_retired_chat_source_is_rejected(tmp_path, name) -> None:
    source = tmp_path / name
    source.parent.mkdir(parents=True)
    source.write_text("synthetic retired source")
    with pytest.raises(AssertionError):
        _assert_retired_chat_sources_absent(tmp_path)


def test_chart_contains_only_native_assistant_enablement() -> None:
    values = yaml.safe_load((ROOT / "deploy/helm/loom/values.yaml").read_text())
    assert values["assistant"] == {"nativeEnabled": False}
    assert not (ROOT / "deploy/helm/loom/templates/assistant.yaml").exists()
    assert not (ROOT / "deploy/helm/loom/templates/assistant-config.yaml").exists()
    helpers = (ROOT / "deploy/helm/loom/templates/_helpers.tpl").read_text()
    assert "LOOM_ASSISTANT_ENABLED" in helpers
    assert "librechat" not in helpers.lower()


@pytest.mark.parametrize(
    "broken", ["", "workload", "network", "enabled", "missing", "off", "legacy", "extra_env"]
)
def test_native_helm_gate_rejects_expanded_runtime_or_network(monkeypatch, broken):
    from scripts.gates import helm_assistant_isolation as gate

    calls = []

    def render(overlay, *sets):
        calls.append((overlay, sets))
        legacy = "assistant.enabled=true" in sets
        external = "extraEnv[0].name=LOOM_ASSISTANT_ENABLED" in sets
        enabled = "assistant.nativeEnabled=true" in sets or (legacy and broken == "legacy")
        flag = "true" if enabled else "false"
        if enabled and broken == "enabled":
            flag = "false"
        if not enabled and broken == "off":
            flag = "true"
        env = [] if broken == "missing" else [{"name": "LOOM_ASSISTANT_ENABLED", "value": flag}]
        if external:
            if broken != "extra_env":
                env.clear()
            env.append({"name": "LOOM_ASSISTANT_ENABLED", "valueFrom": {"configMapKeyRef": {}}})
        web = {
            "kind": "Deployment",
            "metadata": {"name": "loom", "labels": {"app.kubernetes.io/component": "web"}},
            "spec": {"template": {"spec": {"containers": [{"name": "loom", "env": env}]}}},
        }
        policy = {"kind": "NetworkPolicy", "metadata": {"name": "loom"}, "spec": {}}
        if enabled and broken == "network":
            policy["spec"]["ingress"] = [{}]
        rows = [web, policy]
        if enabled and broken == "workload":
            rows.append({"kind": "Deployment", "metadata": {"name": "extra"}})
        return rows

    monkeypatch.setattr(gate, "render", render)
    if broken:
        with pytest.raises(AssertionError):
            gate.main()
    else:
        gate.main()
        for overlay in ("values-kind.yaml", "values-eks.yaml"):
            assert any(name == overlay and "tls.secretName=ci" in args for name, args in calls)
