#!/usr/bin/env python3
"""Render the chart and check TLS on Loom's port: with `tls.secretName` set, the web
and workers pods mount the Secret's certificate and key whole (no subPath, so a
renewed pair reaches them), serve them through LOOM_TLS_CERT_FILE, LOOM_TLS_KEY_FILE
and LOOM_TLS_MIN_VERSION, and probe over HTTPS; the Service marks its port `appProtocol: https`
and the helm test calls it over HTTPS. Without it, no pod carries any of that. The
names the block renders are refused anywhere else, and so is the assistant, which calls
the port over plain HTTP.

    python -m scripts.gates.helm_tls

Needs `helm` on PATH and PyYAML.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from scripts.gates.helm_metadata_egress import EVERYTHING, REQUIRED, loom_pods, render, sets

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "loom"
EKS = ("-f", str(CHART / "values-eks.yaml"))
# The pods that serve the web port, and so the pair.
SERVING = {"Deployment/loom", "Deployment/loom-workers"}
RENDERED = ("LOOM_TLS_CERT_FILE", "LOOM_TLS_KEY_FILE", "LOOM_TLS_MIN_VERSION")
# The EKS overlay's ALB, which must connect over HTTPS once the port serves only that.
ALB_HTTPS = "ingress.annotations.alb\\.ingress\\.kubernetes\\.io/backend-protocol=HTTPS"
PROBES = ("startupProbe", "livenessProbe", "readinessProbe")


def loom_container(pod: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in pod["spec"]["containers"] if c["name"] == "loom")


def service(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "loom")


def health_test(docs: list[dict[str, Any]]) -> str:
    pod = next(
        d for d in docs if d["kind"] == "Pod" and d["metadata"]["name"] == "loom-test-health"
    )
    return str(pod["spec"]["containers"][0]["command"][-1])


def check_served(docs: list[dict[str, Any]], what: str, min_version: str) -> None:
    pods = loom_pods(docs)
    assert set(pods) >= SERVING, f"{what}: found only {sorted(pods)}"
    for name in SERVING:
        container = loom_container(pods[name])
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env["LOOM_TLS_CERT_FILE"] == "/etc/loom/tls/tls.crt", (what, name)
        assert env["LOOM_TLS_KEY_FILE"] == "/etc/loom/tls/tls.key", (what, name)
        assert env["LOOM_TLS_MIN_VERSION"] == min_version, (what, name)
        (mount,) = [m for m in container["volumeMounts"] if m["name"] == "tls"]
        assert mount == {"name": "tls", "mountPath": "/etc/loom/tls", "readOnly": True}, mount
        volumes = {v["name"]: v for v in pods[name]["spec"]["volumes"]}
        assert volumes["tls"]["secret"] == {
            "secretName": "loom-tls",
            "defaultMode": 0o440,
            "items": [{"key": "tls.crt", "path": "tls.crt"}, {"key": "tls.key", "path": "tls.key"}],
        }, (what, name, volumes["tls"])
        for probe in PROBES:
            assert container[probe]["httpGet"].get("scheme") == "HTTPS", (what, name, probe)
    for name in set(pods) - SERVING:
        check_plain_pod(pods[name], f"{what}: {name}")
    assert service(docs)["spec"]["ports"][0].get("appProtocol") == "https", what
    assert 'base = "https://' in health_test(docs), what


def check_plain_pod(pod: dict[str, Any], what: str) -> None:
    for container in pod["spec"]["containers"]:
        names = {e["name"] for e in container.get("env", [])}
        assert not names & set(RENDERED), f"{what} has {names & set(RENDERED)}"
        for probe in PROBES:
            assert "scheme" not in container.get(probe, {}).get("httpGet", {}), (what, probe)
    assert "tls" not in {v["name"] for v in pod["spec"].get("volumes", [])}, what


def check_plain(docs: list[dict[str, Any]], what: str) -> None:
    for name, pod in loom_pods(docs).items():
        check_plain_pod(pod, f"{what}: {name}")
    assert "appProtocol" not in service(docs)["spec"]["ports"][0], what
    assert 'base = "http://' in health_test(docs), what


def refused(want: str, *args: str) -> None:
    cmd = ["helm", "template", "loom", str(CHART), *EKS, *sets(*REQUIRED)]
    done = subprocess.run([*cmd, *args], capture_output=True, text=True)
    assert done.returncode != 0, f"the chart accepted {args}"
    assert want in done.stderr, f"unexpected refusal for {args}: {done.stderr}"


def main() -> None:
    on = ("tls.secretName=loom-tls", ALB_HTTPS, *EVERYTHING)
    check_served(render(*EKS, *sets(*on)), "eks", "1.2")
    check_served(render(*EKS, *sets("tls.minVersion=1.3", *on)), "TLS 1.3", "1.3")
    check_plain(render(*EKS, *sets(*EVERYTHING)), "eks")
    # A release upgraded with --reuse-values from a chart without the block has no key.
    check_plain(render(*EKS, *sets("tls=null", *EVERYTHING)), "no tls key")
    for name in RENDERED:
        want = f"set {name} through tls.*"
        refused(f"{want}, not env", *sets(f"env.{name}=x"))
        refused(f"{want}, not secrets.extra", *sets(f"secrets.extra.{name}=x"))
        refused(f"{want}, not extraEnv", *sets(f"extraEnv[0].name={name}", "extraEnv[0].value=x"))
        field = name.removeprefix("LOOM_").lower()
        refused(f"set {field} through tls.*, not settingsFile", *sets(f"settingsFile.{field}=x"))
    refused(
        'tls.minVersion must be "1.2" or "1.3"', *sets("tls.secretName=x", "tls.minVersion=1.1")
    )
    refused("tls.secretName must be the name", *sets("tls.secretName=1"))
    alb = 'set ingress.annotations."alb.ingress.kubernetes.io/{}" to HTTPS beside tls.secretName'
    refused(alb.format("backend-protocol"), *sets("tls.secretName=x"))
    health = "ingress.annotations.alb\\.ingress\\.kubernetes\\.io/healthcheck-protocol=HTTP"
    refused(alb.format("healthcheck-protocol"), *sets("tls.secretName=x", ALB_HTTPS, health))
    # The controller reads the Service's annotation over the Ingress's.
    service = "service.annotations.alb\\.ingress\\.kubernetes\\.io/backend-protocol="
    refused(alb.format("backend-protocol"), *sets("tls.secretName=x", ALB_HTTPS, service + "HTTP"))
    only_service = sets("tls.secretName=loom-tls", service + "HTTPS", *EVERYTHING)
    check_served(render(*EKS, *only_service), "on the Service only", "1.2")
    assistant = ("assistant.enabled=true", "assistant.existingSecret=ci")
    refused(
        "assistant.enabled calls Loom's port over plain HTTP", *sets("tls.secretName=x", *assistant)
    )
    # A release upgraded with --reuse-values from a chart without the block, then given a
    # Secret, serves the default minimum.
    reused = sets("tls.minVersion=null", "tls.secretName=loom-tls", ALB_HTTPS, *EVERYTHING)
    check_served(render(*EKS, *reused), "reused values", "1.2")
    print("tls: the serving pods mount, serve and probe the pair; refusals hold")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        sys.exit(f"tls: {e}")
