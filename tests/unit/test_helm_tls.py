"""The chart's TLS on Loom's port: the mount, settings and HTTPS probes of the serving
pods, and the refusals of the names the block renders (`scripts/gates/helm_tls.py`)."""

from __future__ import annotations

import shutil

import pytest

from scripts.gates import helm_tls


@pytest.mark.skipif(not shutil.which("helm"), reason="needs helm (the CI helm job runs it)")
def test_the_serving_pods_mount_serve_and_probe_the_pair() -> None:
    helm_tls.main()
