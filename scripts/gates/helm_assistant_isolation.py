#!/usr/bin/env python3
"""Verify native assistant enablement adds no services, stores or ingress peers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "loom"
ON = ("assistant.enabled=true", "env.LOOM_ASSISTANT_PROVIDER=fake", "networkPolicy.enabled=true")


def render(overlay: str, *sets: str) -> list[dict[str, Any]]:
    args = ["helm", "template", "loom", str(CHART), "-f", str(CHART / overlay)]
    for value in ("secrets.jwtSecret=" + "j" * 40, *sets):
        args += ["--set", value]
    out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def main() -> None:
    for overlay in ("values-kind.yaml", "values-eks.yaml"):
        off = render(overlay, "networkPolicy.enabled=true")
        on = render(overlay, *ON)

        def identity(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
            return {(d["kind"], d["metadata"]["name"]) for d in rows}

        assert identity(off) == identity(on), "enablement added a workload or store"

        def policies(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [d for d in rows if d["kind"] == "NetworkPolicy"]

        assert policies(off) == policies(on), "enablement widened network access"
        for doc in on:
            if doc["kind"] == "ConfigMap":
                data = doc.get("data", {})
                if "LOOM_ASSISTANT_ENABLED" in data:
                    assert data["LOOM_ASSISTANT_ENABLED"] == "true"
            assert "librechat" not in yaml.safe_dump(doc).lower()
            assert "mongodb" not in yaml.safe_dump(doc).lower()
        # Native tool dispatch has no plain-HTTP connection to a service port.
        render(
            overlay,
            *ON,
            "tls.secretName=ci",
            "ingress.annotations.alb\\.ingress\\.kubernetes\\.io/backend-protocol=HTTPS",
        )
    print("native assistant: no extra workload, store or network access; TLS rendered")


if __name__ == "__main__":
    main()
