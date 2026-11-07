#!/usr/bin/env python3
"""Render the chart and check that no pod running the Loom image can reach the
instance metadata service, with `networkPolicy.enabled` on or off: the pods run
the compiler tiers of operator-listed repositories, and that code must not take
the node's cloud role. Off, the policy is egress-only with one rule that names
every pod and every address but `egressExcept`; `guardMetadata: false` or
`egressExcept: []` renders none.

    python scripts/gates/helm_metadata_egress.py

Needs `helm` on PATH and PyYAML.
"""

from __future__ import annotations

import ipaddress
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "loom"
METADATA = ("169.254.169.254", "fd00:ec2::254", "fd20:ce::254")
REQUIRED = (
    "secrets.jwtSecret=" + "j" * 40,
    "publicBaseUrl=https://loom.example.com",
    "clickhouse.host=ch",
)
# Every pod that runs the Loom image with EVERYTHING on: a template that stops
# rendering one, or stops rendering its image, would drop it from the check.
LOOM_PODS = {
    "Deployment/loom",
    "Deployment/loom-workers",
    "Job/loom-publish",
    "Job/loom-reseal",
    "Job/loom-rollback-check",
    "CronJob/loom-analytics-export",
    "Pod/loom-test-health",
}
# Every optional object the policies cover.
EVERYTHING = (
    "workers.enabled=true",
    "analyticsExport.enabled=true",
    "analyticsExport.bucket=ci",
    "analyticsExport.nameKeySecret.name=ci",
    "extractor.enabled=true",
    "secrets.extractorToken=" + "t" * 24,
    "mustMatchChange.publish=ci",
)


def render(*args: str) -> list[dict[str, Any]]:
    cmd = ["helm", "template", "loom", str(CHART), *sets(*REQUIRED)]
    done = subprocess.run([*cmd, *args], capture_output=True, text=True)
    assert done.returncode == 0, f"helm template {' '.join(args)}: {done.stderr}"
    return [d for d in yaml.safe_load_all(done.stdout) if d]


def sets(*values: str) -> tuple[str, ...]:
    return tuple(arg for v in values for arg in ("--set", v))


def loom_pods(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every pod template that runs the Loom image, by object name."""
    image = next(
        c["image"]
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "loom"
        for c in d["spec"]["template"]["spec"]["containers"]
    )
    out = {}
    for d in docs:
        spec = d.get("spec", {})
        if d["kind"] == "Pod":
            pod = d
        elif d["kind"] == "CronJob":
            pod = spec["jobTemplate"]["spec"]["template"]
        else:
            pod = spec.get("template")
        if not pod:
            continue
        containers = pod["spec"].get("containers", []) + pod["spec"].get("initContainers", [])
        if any(c.get("image") == image for c in containers):
            out[f"{d['kind']}/{d['metadata']['name']}"] = pod
    return out


def selects(policy: dict[str, Any], labels: dict[str, str]) -> bool:
    assert "matchExpressions" not in policy["podSelector"], "read matchLabels only"
    match = policy["podSelector"].get("matchLabels", {})
    return all(labels.get(k) == v for k, v in match.items())


def reaches(peer: dict[str, Any], address: str) -> bool:
    block = peer.get("ipBlock")
    if block is None:
        return False  # a pod or namespace selector matches pods, never the node's metadata service
    ip = ipaddress.ip_address(address)
    if ip not in ipaddress.ip_network(block["cidr"]):
        return False
    return not any(
        ip in net
        for net in map(ipaddress.ip_network, block.get("except", []))
        if net.version == ip.version
    )


def check_metadata_denied(docs: list[dict[str, Any]], what: str) -> None:
    policies = [d["spec"] for d in docs if d["kind"] == "NetworkPolicy"]
    pods = loom_pods(docs)
    assert set(pods) >= LOOM_PODS, f"{what}: found only {sorted(pods)}"
    for name, pod in pods.items():
        assert not pod["spec"].get("hostNetwork"), f"{what}: {name} is outside every policy"
        labels = pod["metadata"].get("labels", {})
        mine = [p for p in policies if selects(p, labels) and "Egress" in p["policyTypes"]]
        assert mine, f"{what}: no egress policy selects {name}"
        for policy in mine:
            for rule in policy.get("egress", []):
                assert rule.get("to"), f"{what}: an egress rule of {name} has no peers"
                for address in METADATA:
                    reached = any(reaches(p, address) for p in rule["to"])
                    assert not reached, f"{what}: {name} reaches {address}"


def web_policies(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    labels = loom_pods(docs)["Deployment/loom"]["metadata"]["labels"]
    return [d["spec"] for d in docs if d["kind"] == "NetworkPolicy" and selects(d["spec"], labels)]


def main() -> None:
    kind, eks = ("-f", str(CHART / "values-kind.yaml")), ("-f", str(CHART / "values-eks.yaml"))
    for what, args in (
        ("defaults", sets(*EVERYTHING)),
        ("kind overlay", (*kind, *sets(*EVERYTHING))),
        ("eks overlay", (*eks, *sets(*EVERYTHING))),
        ("eks overlay, policy off", (*eks, *sets("networkPolicy.enabled=false", *EVERYTHING))),
        ("policy on", sets("networkPolicy.enabled=true", *EVERYTHING)),
    ):
        check_metadata_denied(render(*args), what)
    # Off, the one policy changes nothing else: egress only, one portless rule
    # naming every pod and every address but the metadata service.
    (policy,) = web_policies(render())
    assert policy["policyTypes"] == ["Egress"], policy
    assert policy["egress"] == [
        {
            "to": [
                {"namespaceSelector": {}},
                {"ipBlock": {"cidr": "0.0.0.0/0", "except": ["169.254.169.254/32"]}},
                {"ipBlock": {"cidr": "::/0", "except": ["fd00:ec2::254/128", "fd20:ce::254/128"]}},
            ]
        }
    ], policy["egress"]
    assert not web_policies(render(*sets("networkPolicy.guardMetadata=false")))
    # guardMetadata: false turns off only the off-mode policy; the full one still renders
    (full,) = web_policies(
        render(*sets("networkPolicy.enabled=true", "networkPolicy.guardMetadata=false"))
    )
    assert "Ingress" in full["policyTypes"], full
    # a release upgraded with --reuse-values has no such key: the guard stays on
    assert web_policies(render(*sets("networkPolicy.guardMetadata=null"))) == [policy]
    assert not web_policies(render("--set-json", "networkPolicy.egressExcept=[]"))
    # one address family only: the other family's range carries no except list
    for only, v4, v6 in (
        ('["169.254.169.254/32"]', ["169.254.169.254/32"], None),
        ('["fd00:ec2::254/128"]', None, ["fd00:ec2::254/128"]),
    ):
        (one,) = web_policies(render("--set-json", f"networkPolicy.egressExcept={only}"))
        blocks = [p["ipBlock"] for p in one["egress"][0]["to"] if "ipBlock" in p]
        assert [b.get("except") for b in blocks] == [v4, v6], blocks
    print("metadata egress: denied with the policy on and off; other egress open when off")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        sys.exit(f"metadata egress: {e}")
