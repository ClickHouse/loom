#!/usr/bin/env python3
"""Render the chart and check that no Loom pod can reach the instance metadata
service, whatever `networkPolicy.enabled` says: the pods run the compiler tiers
of operator-listed repositories, and that code must not take the node's cloud
role. With the policy off, every other egress stays open; `egressExcept: []`
renders no policy, as before.

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
METADATA = ("169.254.169.254", "fd00:ec2::254")


def render(*args: str) -> list[dict[str, Any]]:
    cmd = ["helm", "template", "loom", str(CHART), "--set", "secrets.jwtSecret=" + "j" * 40]
    cmd += ["--set", "publicBaseUrl=https://loom.example.com", "--set", "clickhouse.host=ch"]
    out = subprocess.run([*cmd, *args], check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def loom_policies(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The NetworkPolicies that select the release's web pods."""
    web = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "loom")
    labels = web["spec"]["template"]["metadata"]["labels"]
    return [
        d["spec"]
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and all(labels.get(k) == v for k, v in d["spec"]["podSelector"]["matchLabels"].items())
    ]


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
    policies = [p for p in loom_policies(docs) if "Egress" in p["policyTypes"]]
    assert policies, f"{what}: no egress policy selects the Loom pods"
    for policy in policies:
        for rule in policy.get("egress", []):
            assert rule.get("to"), f"{what}: an egress rule with no peers reaches everything"
            for address in METADATA:
                assert not any(reaches(p, address) for p in rule["to"]), f"{what}: {address}"


def main() -> None:
    for what, args in (
        ("defaults", ()),
        ("kind overlay", ("-f", str(CHART / "values-kind.yaml"))),
        ("eks overlay", ("-f", str(CHART / "values-eks.yaml"))),
        ("policy on", ("--set", "networkPolicy.enabled=true")),
    ):
        check_metadata_denied(render(*args), what)
    # Off, the policy keeps ingress and every other egress open: one rule, no ports,
    # pods anywhere in the cluster (engines differ on matching pod IPs by range).
    (policy,) = loom_policies(render())
    assert policy["policyTypes"] == ["Egress"], policy
    (rule,) = policy["egress"]
    assert "ports" not in rule and {"namespaceSelector": {}} in rule["to"], rule
    assert not loom_policies(render("--set-json", "networkPolicy.egressExcept=[]"))
    print("metadata egress: denied with the policy on and off; other egress open when off")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, subprocess.CalledProcessError) as e:
        sys.exit(f"metadata egress: {e}")
