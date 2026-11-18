"""Render the cell probe contract without server or pytest dependencies.

    python -m scripts.gates.helm_cell_probe

Needs Helm on PATH and PyYAML."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from scripts.gates.helm_metadata_egress import CHART, EVERYTHING, REQUIRED, loom_pods, sets
from scripts.gates.helm_postgres import CONFIGURED

PORT_NAME = "cell-probe"
ENV_NAME = "LOOM_CELL_PROBE_PORT"
ENDPOINT_LABEL = "loom.clickhouse.com/cell-probe"
ENABLED = 8777
INGRESS = (
    "ingress.enabled=true",
    "trustedProxies=10.0.0.0/8",
    "ingress.hosts[0].host=loom.example.com",
    "ingress.hosts[0].paths[0].path=/",
    "ingress.hosts[0].paths[0].pathType=Prefix",
)


def _template(*args: str, release: str = "loom") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["helm", "template", release, str(CHART), *sets(*REQUIRED), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _render(*args: str, release: str = "loom") -> list[dict[str, Any]]:
    result = _template(*args, release=release)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _object(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    matches = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name, matches)
    return matches[0]


def _env(container: dict[str, Any], value: str) -> None:
    entries = [entry for entry in container.get("env", []) if entry["name"] == ENV_NAME]
    assert entries == [{"name": ENV_NAME, "value": value}], entries


def _probe_service(docs: list[dict[str, Any]]) -> dict[str, Any]:
    services = [
        d
        for d in docs
        if d["kind"] == "Service" and any(p["name"] == PORT_NAME for p in d["spec"]["ports"])
    ]
    assert len(services) == 1, services
    return services[0]


def _check_enabled(docs: list[dict[str, Any]], port: int, *, workers: bool) -> None:
    service = _probe_service(docs)
    assert service["metadata"]["name"] == "loom-cell-probe"
    spec = service["spec"]
    assert spec["type"] == "ClusterIP"
    assert spec["publishNotReadyAddresses"] is True
    assert spec["ports"] == [
        {"name": PORT_NAME, "port": port, "targetPort": PORT_NAME, "protocol": "TCP"}
    ]
    assert not {"externalIPs", "externalName", "loadBalancerIP", "loadBalancerClass"} & spec.keys()
    common = _object(docs, "Service", "loom")["spec"]["selector"]
    assert spec["selector"] == {
        **{k: v for k, v in common.items() if k != "app.kubernetes.io/component"},
        ENDPOINT_LABEL: "true",
    }
    wanted = {"Deployment/loom"}
    if workers:
        wanted.add("Deployment/loom-workers")
    pods = loom_pods(docs)
    assert wanted <= pods.keys()
    selected = {
        name
        for name, pod in pods.items()
        if all((pod["metadata"]["labels"].get(k) == v for k, v in spec["selector"].items()))
    }
    assert selected == wanted, selected
    for name, pod in pods.items():
        for container in pod["spec"]["containers"]:
            probe_ports = [p for p in container.get("ports", []) if p["name"] == PORT_NAME]
            if name in wanted:
                assert pod["metadata"]["labels"][ENDPOINT_LABEL] == "true"
                assert probe_ports == [
                    {"name": PORT_NAME, "containerPort": port, "protocol": "TCP"}
                ]
                _env(container, str(port))
                assert container["readinessProbe"]["httpGet"]["port"] == "http"
            else:
                assert not probe_ports, (name, probe_ports)
                assert ENDPOINT_LABEL not in pod["metadata"]["labels"], name
    for suffix in ("extractor", "librechat", "librechat-mongo"):
        for doc in docs:
            if (
                doc["kind"] in ("Deployment", "StatefulSet")
                and doc["metadata"]["name"] == f"loom-{suffix}"
            ):
                pod = doc["spec"]["template"]
                assert ENDPOINT_LABEL not in pod["metadata"]["labels"]
                assert not all(
                    (pod["metadata"]["labels"].get(k) == v for k, v in spec["selector"].items())
                )


def check_probe_is_off_by_default_even_with_other_env_sources(workers: bool) -> None:
    docs = _render(
        *sets(f"workers.enabled={str(workers).lower()}"),
        "--set-json",
        'envFrom=[{"configMapRef":{"name":"operator-settings"}}]',
    )
    assert not any(
        d["kind"] == "Service" and d["metadata"]["name"] == "loom-cell-probe" for d in docs
    )
    for name, pod in loom_pods(docs).items():
        assert ENDPOINT_LABEL not in pod["metadata"]["labels"], name
        for container in pod["spec"]["containers"]:
            assert not any(p["name"] == PORT_NAME for p in container.get("ports", []))
            if name.startswith("Deployment/"):
                _env(container, "0")
                assert {"configMapRef": {"name": "operator-settings"}} in container["envFrom"]


def check_probe_selects_every_listener_workload_and_no_hook_or_test_pod(workers: bool) -> None:
    docs = _render(
        *sets(
            *EVERYTHING,
            "assistant.enabled=true",
            "assistant.existingSecret=ci",
            f"workers.enabled={str(workers).lower()}",
            f"cellProbe.port={ENABLED}",
        )
    )
    _check_enabled(docs, ENABLED, workers=workers)


def check_probe_changes_no_traffic_route_or_network_policy(
    overlay: tuple[str, ...], policy: bool
) -> None:
    args = tuple(arg for name in overlay for arg in ("-f", str(CHART / name)))
    args += sets(*EVERYTHING, *INGRESS, f"networkPolicy.enabled={str(policy).lower()}")
    before, after = (_render(*args), _render(*args, *sets(f"cellProbe.port={ENABLED}")))
    changed = _render(*args, *sets(f"cellProbe.port={ENABLED + 1}"))
    _check_enabled(after, ENABLED, workers=True)
    _check_enabled(changed, ENABLED + 1, workers=True)
    for rendered in (after, changed):
        for kind in ("NetworkPolicy", "Ingress"):
            assert [d for d in before if d["kind"] == kind] == [
                d for d in rendered if d["kind"] == kind
            ]
        assert _object(before, "Service", "loom") == _object(rendered, "Service", "loom")
    for name in ("loom", "loom-workers"):
        original = _object(before, "Deployment", name)["spec"]["selector"]
        for rendered, enabled in ((before, False), (after, True), (changed, True)):
            deployment = _object(rendered, "Deployment", name)["spec"]
            selector = deployment["selector"]
            assert selector == original, (name, selector, original)
            assert ENDPOINT_LABEL not in selector.get("matchLabels", {})
            assert all(
                expression["key"] != ENDPOINT_LABEL
                for expression in selector.get("matchExpressions", [])
            )
            labels = deployment["template"]["metadata"]["labels"]
            if enabled:
                assert labels[ENDPOINT_LABEL] == "true"
            else:
                assert ENDPOINT_LABEL not in labels


def check_numeric_ports_reach_listener_and_service(port: int) -> None:
    _check_enabled(_render(*sets(f"cellProbe.port={port}")), port, workers=False)


def check_values_file_and_json_numbers_are_accepted(tmp_path: Path, port: int) -> None:
    values = tmp_path / "probe.yaml"
    values.write_text(f"cellProbe:\n  port: {port}\n")
    for args in (("-f", str(values)), ("--set-json", f"cellProbe.port={port}")):
        docs = _render(*args)
        if port:
            _check_enabled(docs, port, workers=False)
        else:
            _env(loom_pods(docs)["Deployment/loom"]["spec"]["containers"][0], "0")
            assert not any(d["metadata"]["name"] == "loom-cell-probe" for d in docs)


def check_an_absent_port_after_value_coalescing_is_off(value: Any) -> None:
    docs = _render("--set-json", f"cellProbe={json.dumps(value)}")
    _env(loom_pods(docs)["Deployment/loom"]["spec"]["containers"][0], "0")
    assert not any(d["metadata"]["name"] == "loom-cell-probe" for d in docs)


def check_the_explicit_port_takes_precedence_over_env_from(port: int) -> None:
    docs = _render(
        *sets(
            f"cellProbe.port={port}",
            "workers.enabled=true",
            "secrets.existingSecret=operator-secret",
        ),
        "--set-json",
        'envFrom=[{"secretRef":{"name":"operator-settings"}}]',
    )
    for name in ("Deployment/loom", "Deployment/loom-workers"):
        container = loom_pods(docs)[name]["spec"]["containers"][0]
        _env(container, str(port))
        assert container["envFrom"] == [
            {"secretRef": {"name": "operator-secret"}},
            {"secretRef": {"name": "operator-settings"}},
        ]


def check_every_configured_pod_has_one_explicit_port_beside_external_env_from(port: int) -> None:
    docs = _render(
        *sets(*EVERYTHING, f"cellProbe.port={port}", "secrets.existingSecret=operator-secret"),
        "--set-json",
        'envFrom=[{"configMapRef":{"name":"operator-settings"}}]',
    )
    pods = loom_pods(docs)
    health = "Pod/loom-test-health"
    assert set(pods) == CONFIGURED | {health}
    for name in CONFIGURED:
        for container in pods[name]["spec"]["containers"]:
            _env(container, str(port))
            assert container["envFrom"] == [
                {"secretRef": {"name": "operator-secret"}},
                {"configMapRef": {"name": "operator-settings"}},
            ]
    for container in pods[health]["spec"]["containers"]:
        assert not any(entry["name"] == ENV_NAME for entry in container.get("env", []))
    if port:
        _check_enabled(docs, port, workers=True)
    else:
        assert not any(
            d["kind"] == "Service" and d["metadata"]["name"] == "loom-cell-probe" for d in docs
        )
        for name, pod in pods.items():
            assert ENDPOINT_LABEL not in pod["metadata"]["labels"], name
            for container in pod["spec"]["containers"]:
                assert not any(p["name"] == PORT_NAME for p in container.get("ports", []))


def check_probe_service_keeps_its_suffix_with_a_long_fullname() -> None:
    full = "l" * 63
    docs = _render(*sets(f"fullnameOverride={full}", f"cellProbe.port={ENABLED}"))
    service = _probe_service(docs)
    assert service["metadata"]["name"] == f"{'l' * 52}-cell-probe"
    assert re.fullmatch("[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", service["metadata"]["name"])
    web = _object(docs, "Deployment", full)["spec"]["template"]
    assert all(
        (web["metadata"]["labels"].get(k) == v for k, v in service["spec"]["selector"].items())
    )


def check_bad_probe_ports_are_refused_by_name(value: Any) -> None:
    result = _template("--set-json", f"cellProbe.port={json.dumps(value)}")
    assert result.returncode != 0, f"accepted cellProbe.port={value!r}"
    assert "cellProbe.port must be" in result.stderr, result.stderr


def check_set_string_ports_are_refused(value: str) -> None:
    result = _template("--set-string", f"cellProbe.port={value}")
    assert result.returncode != 0, f"accepted string port {value!r}"
    assert "cellProbe.port must be" in result.stderr, result.stderr


def check_bad_probe_blocks_are_refused_by_name(value: Any) -> None:
    result = _template("--set-json", f"cellProbe={json.dumps(value)}")
    assert result.returncode != 0, f"accepted cellProbe={value!r}"
    assert "cellProbe must be a map" in result.stderr, result.stderr


def check_probe_port_cannot_be_set_twice(port: int, source: str) -> None:
    if source == "extraEnv":
        args = sets(f"extraEnv[0].name={ENV_NAME}", "extraEnv[0].value=0")
    else:
        name = "cell_probe_port" if source == "settingsFile" else ENV_NAME
        args = sets(f"{source}.{name}=0")
    result = _template(*sets(f"cellProbe.port={port}"), *args)
    assert result.returncode != 0, (source, port)
    assert "through cellProbe.port" in result.stderr and f"not {source}" in result.stderr, (
        result.stderr
    )


def check_user_pod_labels_cannot_override_probe_endpoint_membership(value: str) -> None:
    result = _template(
        *sets(f"cellProbe.port={ENABLED}"),
        "--set-json",
        f'podLabels={{"{ENDPOINT_LABEL}":"{value}"}}',
    )
    assert result.returncode != 0, "accepted a conflicting probe endpoint label"
    assert "podLabels" in result.stderr and ENDPOINT_LABEL in result.stderr, result.stderr


def check_the_main_service_and_ingress_use_the_web_http_port(policy: bool) -> None:
    docs = _render(
        *sets("workers.enabled=true", *INGRESS, f"networkPolicy.enabled={str(policy).lower()}")
    )
    service = _object(docs, "Service", "loom")["spec"]
    assert service["selector"]["app.kubernetes.io/component"] == "web"
    assert service["ports"] == [
        {"name": "http", "port": 7777, "targetPort": "http", "protocol": "TCP"}
    ]
    assert not service.get("publishNotReadyAddresses", False)
    for rule in _object(docs, "Ingress", "loom")["spec"]["rules"]:
        for path in rule["http"]["paths"]:
            assert path["backend"]["service"] == {"name": "loom", "port": {"name": "http"}}


def check_other_settings_and_pod_labels_still_render() -> None:
    docs = _render(*sets("env.LOOM_CELL=example", "podLabels.operator=example"))
    pod = loom_pods(docs)["Deployment/loom"]
    assert pod["metadata"]["labels"]["operator"] == "example"
    assert {"name": "LOOM_CELL", "value": "example"} in pod["spec"]["containers"][0]["env"]


def check_existing_embedding_and_root_names_keep_their_config_route_refusal(
    name: str, through: str, source: str
) -> None:
    field = name.removeprefix("LOOM_").lower() if source == "settingsFile" else name
    if source == "extraEnv":
        args = sets(f"extraEnv[0].name={name}", "extraEnv[0].value=x")
    else:
        args = sets(f"{source}.{field}=x")
    result = _template(*args)
    assert result.returncode != 0, (name, source)
    assert f"set {field} through {through}" in result.stderr, result.stderr
    assert f"not {source}" in result.stderr, result.stderr


def main() -> None:
    for workers in (False, True):
        check_probe_is_off_by_default_even_with_other_env_sources(workers)
        check_probe_selects_every_listener_workload_and_no_hook_or_test_pod(workers)
    for overlay in (
        (),
        ("values-kind.yaml",),
        ("values-eks.yaml",),
        ("values-eks.yaml", "values-clickhouse-cloud.yaml"),
    ):
        for policy in (False, True):
            check_probe_changes_no_traffic_route_or_network_policy(overlay, policy)
    for port in (1, 7776, 7778, 65535):
        check_numeric_ports_reach_listener_and_service(port)
    with tempfile.TemporaryDirectory(prefix="loom-helm-cell-probe-") as directory:
        for port in (0, ENABLED):
            check_values_file_and_json_numbers_are_accepted(Path(directory), port)
    for absent_block in (None, {"port": None}):
        check_an_absent_port_after_value_coalescing_is_off(absent_block)
    for port in (0, ENABLED):
        check_the_explicit_port_takes_precedence_over_env_from(port)
        check_every_configured_pod_has_one_explicit_port_beside_external_env_from(port)
        for source in ("env", "extraEnv", "secrets.extra", "settingsFile"):
            check_probe_port_cannot_be_set_twice(port, source)
    check_probe_service_keeps_its_suffix_with_a_long_fullname()
    invalid_ports: tuple[Any, ...] = (-1, 65536, 7777, 8777.5, True, False, "8777", "0", "", [], {})
    for invalid_port in invalid_ports:
        check_bad_probe_ports_are_refused_by_name(invalid_port)
    for text in ("0", "8777"):
        check_set_string_ports_are_refused(text)
    invalid_blocks: tuple[Any, ...] = (False, True, "8777", 8777, [])
    for invalid_block in invalid_blocks:
        check_bad_probe_blocks_are_refused_by_name(invalid_block)
    for label in ("false", "true"):
        check_user_pod_labels_cannot_override_probe_endpoint_membership(label)
    for policy in (False, True):
        check_the_main_service_and_ingress_use_the_web_http_port(policy)
    check_other_settings_and_pod_labels_still_render()
    for name, through in (
        ("LOOM_EMBEDDING_PROVIDER", "embedding.*"),
        ("LOOM_EMBEDDING_MODEL", "embedding.*"),
        ("LOOM_EMBEDDING_DIM", "embedding.*"),
        ("LOOM_EMBEDDING_BASE_URL", "embedding.*"),
        ("LOOM_EMBEDDING_SEND_DIMENSIONS", "embedding.*"),
        ("LOOM_JWT_SECRET", "secrets.jwtSecret"),
        ("LOOM_JWT_SECRET_PREVIOUS", "secrets.jwtSecretPrevious"),
    ):
        for source in ("env", "extraEnv", "secrets.extra", "settingsFile"):
            check_existing_embedding_and_root_names_keep_their_config_route_refusal(
                name, through, source
            )
    print("Helm cell probe contract passed")


if __name__ == "__main__":
    main()
