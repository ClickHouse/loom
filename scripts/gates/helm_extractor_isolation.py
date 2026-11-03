#!/usr/bin/env python3
"""Render the chart with the extractor on and check what its pods can be
reached by and reach: no Service, policy or workload that selects Loom's pods
selects them, their own policy admits only Loom's pods and allows no egress, and
a new extractor token restarts both sides.

    python scripts/gates/helm_extractor_isolation.py

Needs `helm` on PATH and PyYAML.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "loom"


def render(overlay: str, *sets: str) -> list[dict[str, Any]]:
    args = ["helm", "template", "loom", str(CHART), "-f", str(CHART / overlay)]
    for s in ("secrets.jwtSecret=" + "j" * 40, "extractor.enabled=true", *sets):
        args += ["--set", s]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def _selects(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in selector.items())


def _selector(doc: dict[str, Any]) -> dict[str, str]:
    spec = doc.get("spec", {})
    if doc["kind"] == "Service":
        return spec.get("selector") or {}
    if doc["kind"] in ("NetworkPolicy", "PodDisruptionBudget"):
        return (spec.get("podSelector") or spec.get("selector") or {}).get("matchLabels", {})
    return (spec.get("selector") or {}).get("matchLabels", {})


def check(docs: list[dict[str, Any]]) -> None:
    def named(kind: str, suffix: str) -> dict[str, Any]:
        return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"].endswith(suffix))

    deployment = named("Deployment", "-extractor")
    pod = deployment["spec"]["template"]
    labels = pod["metadata"]["labels"]
    selecting = [
        f"{d['kind']}/{d['metadata']['name']}"
        for d in docs
        if d["kind"] in ("Service", "NetworkPolicy", "PodDisruptionBudget", "Deployment")
        and not d["metadata"]["name"].endswith("-extractor")
        and _selector(d)
        and _selects(_selector(d), labels)
    ]
    assert not selecting, f"select the extractor's pods: {selecting}"
    policy = named("NetworkPolicy", "-extractor")["spec"]
    assert policy["policyTypes"] == ["Ingress", "Egress"] and not policy.get("egress")
    (rule,) = policy["ingress"]
    web = named("Deployment", "loom")["spec"]["template"]["metadata"]["labels"]
    assert all(_selects(p["podSelector"]["matchLabels"], web) for p in rule["from"])
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False
    (container,) = spec["containers"]
    env = {e["name"]: e for e in container["env"]}
    assert env["LOOM_EXTRACTOR_TOKEN"]["valueFrom"]["secretKeyRef"]["key"] == (
        "LOOM_DOCUMENT_EXTRACTOR_TOKEN"
    )
    assert "envFrom" not in container, "the extractor reads one key of the Secret, not all"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def annotations(docs: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    return {
        d["metadata"]["name"]: d["spec"]["template"]["metadata"].get("annotations", {})
        for d in docs
        if d["kind"] == "Deployment"
    }


def main() -> None:
    for overlay in ("values-kind.yaml", "values-eks.yaml"):
        token = "secrets.extractorToken=" + "t" * 24
        on = ("networkPolicy.enabled=true", "workers.enabled=true", token)
        docs = render(overlay, *on)
        check(docs)
        if overlay == "values-kind.yaml":  # the EKS overlay's Secret is the operator's
            rotated = render(overlay, *on[:2], "secrets.extractorToken=" + "u" * 24)
            before, after = annotations(docs), annotations(rotated)
            changed = [name for name in before if before[name] != after[name]]
            assert sorted(changed) == sorted(before), f"a new token restarts only {changed}"
    print("extractor isolation: selectors, policy, token key and restarts checked")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, subprocess.CalledProcessError) as e:
        sys.exit(f"extractor isolation: {e}")
