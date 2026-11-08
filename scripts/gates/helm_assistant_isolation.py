#!/usr/bin/env python3
"""Render the chart with the assistant on and check what LibreChat and its MongoDB
can be reached by and reach: Loom's ingress admits LibreChat (the agent's tools) when
it admits named peers only, LibreChat admits only Loom's pods, MongoDB admits only
LibreChat and reaches nothing, LibreChat runs one pod at a time, and a changed
configuration restarts it.

    python scripts/gates/helm_assistant_isolation.py

Needs `helm` on PATH and PyYAML.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "loom"
ON = ("assistant.enabled=true", "assistant.existingSecret=ci", "networkPolicy.enabled=true")


def render(overlay: str, *sets: str) -> list[dict[str, Any]]:
    args = ["helm", "template", "loom", str(CHART), "-f", str(CHART / overlay)]
    for s in ("secrets.jwtSecret=" + "j" * 40, *sets):
        args += ["--set", s]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def check(docs: list[dict[str, Any]]) -> None:
    def named(kind: str, name: str) -> dict[str, Any]:
        return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)

    def pods(policy: dict[str, Any]) -> dict[str, str]:
        return policy["spec"]["podSelector"]["matchLabels"]

    loom = named("NetworkPolicy", "loom")
    librechat = named("NetworkPolicy", "loom-librechat")
    mongo = named("NetworkPolicy", "loom-librechat-mongo")
    (rule,) = loom["spec"]["ingress"]
    if "from" in rule:  # named peers only (an overlay's ingressFrom): LibreChat among them
        assert {"podSelector": {"matchLabels": pods(librechat)}} in rule["from"], rule
    (admits,) = librechat["spec"]["ingress"]
    assert admits["from"] == [{"podSelector": {"matchLabels": pods(loom)}}], admits
    (admits,) = mongo["spec"]["ingress"]
    assert admits["from"] == [{"podSelector": {"matchLabels": pods(librechat)}}], admits
    assert mongo["spec"]["policyTypes"] == ["Ingress", "Egress"] and not mongo["spec"].get("egress")
    deployment = named("Deployment", "loom-librechat")
    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def checksum(docs: list[dict[str, Any]]) -> str:
    deployment = next(
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["metadata"]["labels"].get("app.kubernetes.io/component") == "assistant"
    )
    return str(deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"])


def main() -> None:
    for overlay in ("values-kind.yaml", "values-eks.yaml"):
        check(render(overlay, *ON))
    # Another release name is another Loom Service in the configuration: a restart.
    kind = render("values-kind.yaml", *ON)
    renamed = render("values-kind.yaml", *ON, "fullnameOverride=other")
    assert checksum(kind) != checksum(renamed)
    # LibreChat's MCP server is the Loom Service's port, whatever the release sets.
    (config,) = [
        d
        for d in render("values-kind.yaml", *ON, "service.port=80")
        if d["kind"] == "ConfigMap" and "librechat.yaml" in d.get("data", {})
    ]
    mcp = yaml.safe_load(config["data"]["librechat.yaml"])
    assert mcp["mcpServers"]["loom"]["url"] == "http://loom:80/mcp?toolset=assistant", mcp
    assert mcp["mcpSettings"]["allowedAddresses"] == ["loom:80"], mcp
    print("assistant isolation: policies, one pod at a time and the config checksum checked")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        sys.exit(f"assistant isolation: {e}")
